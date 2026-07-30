//! Session validation and rate limiting over gRPC.
//!
//! One `Check` call resolves a session token to a principal and consumes one
//! unit of that principal's rate budget. Both steps are served from Redis, and
//! the rate decision is evaluated by a Lua script so check-and-increment is
//! atomic — see src/limiter.lua for why that matters.

use std::sync::Arc;
use std::time::Duration;

use redis::aio::ConnectionManager;
use redis::Script;
use tonic::{transport::Server, Request, Response, Status};

pub mod authz {
    tonic::include_proto!("authz.v1");
}

use authz::authz_server::{Authz, AuthzServer};
use authz::{CheckRequest, CheckResponse, Decision, HealthRequest, HealthResponse};

/// Window length and per-window budget. Deliberately compile-time constants:
/// the benchmark needs a fixed, documented policy to measure against.
const WINDOW_MS: u64 = 1_000;
const LIMIT_PER_WINDOW: u32 = 10_000;

struct AuthzService {
    redis: ConnectionManager,
    limiter: Arc<Script>,
}

impl AuthzService {
    /// Resolves an opaque token to a principal via a single Redis GET.
    async fn resolve_subject(&self, token: &str) -> Result<Option<String>, redis::RedisError> {
        let mut conn = self.redis.clone();
        redis::cmd("GET")
            .arg(format!("session:{token}"))
            .query_async(&mut conn)
            .await
    }
}

#[tonic::async_trait]
impl Authz for AuthzService {
    async fn check(
        &self,
        request: Request<CheckRequest>,
    ) -> Result<Response<CheckResponse>, Status> {
        let req = request.into_inner();
        let cost = req.cost.max(1);

        let subject = match self.resolve_subject(&req.token).await {
            Ok(Some(s)) => s,
            Ok(None) => {
                return Ok(Response::new(CheckResponse {
                    decision: Decision::DenyUnauthenticated as i32,
                    subject: String::new(),
                    remaining: 0,
                    reset_after_ms: 0,
                }))
            }
            // A Redis failure is reported as unavailable rather than silently
            // allowing or denying: callers must be able to tell "denied" from
            // "the authorizer is down".
            Err(e) => return Err(Status::unavailable(format!("session store: {e}"))),
        };

        let key = format!("rl:{{{subject}}}:{}", req.route);
        let mut conn = self.redis.clone();

        let (allowed, remaining, ttl_ms): (i64, i64, i64) = self
            .limiter
            .key(&key)
            .arg(WINDOW_MS)
            .arg(LIMIT_PER_WINDOW)
            .arg(cost)
            .invoke_async(&mut conn)
            .await
            .map_err(|e| Status::unavailable(format!("rate limiter: {e}")))?;

        let decision = if allowed == 1 {
            Decision::Allow
        } else {
            Decision::DenyRateLimited
        };

        Ok(Response::new(CheckResponse {
            decision: decision as i32,
            subject: if allowed == 1 { subject } else { String::new() },
            remaining: remaining.max(0) as u32,
            reset_after_ms: ttl_ms.max(0) as u64,
        }))
    }

    async fn health(
        &self,
        _request: Request<HealthRequest>,
    ) -> Result<Response<HealthResponse>, Status> {
        let mut conn = self.redis.clone();
        let pong: Result<String, _> = redis::cmd("PING").query_async(&mut conn).await;
        Ok(Response::new(HealthResponse {
            redis_reachable: pong.is_ok(),
        }))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let redis_url = std::env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into());
    let addr = std::env::var("LISTEN_ADDR")
        .unwrap_or_else(|_| "0.0.0.0:50051".into())
        .parse()?;

    let client = redis::Client::open(redis_url.as_str())?;
    // ConnectionManager multiplexes over one connection and reconnects on drop,
    // so per-request latency excludes connection setup.
    let redis = ConnectionManager::new(client).await?;

    let limiter = Arc::new(Script::new(include_str!("limiter.lua")));

    let svc = AuthzService {
        redis: redis.clone(),
        limiter,
    };

    // Kubernetes `grpc:` probes call grpc.health.v1.Health — the standard
    // protocol, not an application-defined Health RPC. Without this service
    // registered the probe gets UNIMPLEMENTED and the kubelet kills the pod.
    let (mut health_reporter, health_service) = tonic_health::server::health_reporter();
    health_reporter
        .set_serving::<AuthzServer<AuthzService>>()
        .await;

    // Readiness tracks Redis: if the session store is unreachable this instance
    // cannot serve a Check, so it should leave the load-balancing set rather
    // than accept traffic it will only fail.
    tokio::spawn({
        let mut conn = redis;
        let mut reporter = health_reporter.clone();
        async move {
            let mut interval = tokio::time::interval(Duration::from_secs(5));
            loop {
                interval.tick().await;
                let ok = redis::cmd("PING")
                    .query_async::<String>(&mut conn)
                    .await
                    .is_ok();
                if ok {
                    reporter.set_serving::<AuthzServer<AuthzService>>().await;
                } else {
                    reporter
                        .set_not_serving::<AuthzServer<AuthzService>>()
                        .await;
                }
            }
        }
    });

    tracing::info!(%addr, "authz-service listening");

    Server::builder()
        .http2_keepalive_interval(Some(Duration::from_secs(20)))
        .add_service(health_service)
        .add_service(AuthzServer::new(svc))
        .serve_with_shutdown(addr, async {
            let _ = tokio::signal::ctrl_c().await;
            tracing::info!("shutting down");
        })
        .await?;

    Ok(())
}
