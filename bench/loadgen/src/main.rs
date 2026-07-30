//! Closed-loop load generator for the authz gRPC service.
//!
//! Records every request's latency into an HdrHistogram, so the reported tail
//! percentiles are real quantiles rather than an average with error bars. Emits
//! JSON containing whatever the run measured — no target figures anywhere.

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use hdrhistogram::Histogram;
use tokio::sync::Mutex;

pub mod authz {
    tonic::include_proto!("authz.v1");
}

use authz::authz_client::AuthzClient;
use authz::CheckRequest;

#[derive(serde::Serialize)]
struct Percentiles {
    p50_us: u64,
    p90_us: u64,
    p95_us: u64,
    p99_us: u64,
    p999_us: u64,
    max_us: u64,
    mean_us: f64,
}

#[derive(serde::Serialize)]
struct Report {
    concurrency: usize,
    duration_s: f64,
    requests_ok: u64,
    requests_err: u64,
    denied_rate_limited: u64,
    throughput_rps: f64,
    latency: Percentiles,
    sub_millisecond_share: f64,
    note: String,
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let endpoint = std::env::var("TARGET").unwrap_or_else(|_| "http://127.0.0.1:50051".into());
    let concurrency: usize = std::env::var("CONCURRENCY")
        .unwrap_or_else(|_| "64".into())
        .parse()?;
    let duration: u64 = std::env::var("DURATION")
        .unwrap_or_else(|_| "30".into())
        .parse()?;
    let warmup: u64 = std::env::var("WARMUP")
        .unwrap_or_else(|_| "5".into())
        .parse()?;
    let out = std::env::var("OUT").unwrap_or_else(|_| "results/rust.json".into());

    // Distinct subjects so workers don't all contend on one rate-limit key,
    // which would measure Redis key contention rather than service latency.
    let tokens: Vec<String> = (0..concurrency).map(|i| format!("tok-{i}")).collect();

    println!("warming up {warmup}s at concurrency {concurrency}...");
    run_phase(&endpoint, &tokens, Duration::from_secs(warmup), None).await?;

    println!("measuring {duration}s...");
    let hist = Arc::new(Mutex::new(Histogram::<u64>::new_with_bounds(
        1, 60_000_000, 3,
    )?));
    let started = Instant::now();
    let counters = run_phase(
        &endpoint,
        &tokens,
        Duration::from_secs(duration),
        Some(hist.clone()),
    )
    .await?;
    let elapsed = started.elapsed().as_secs_f64();

    let h = hist.lock().await;
    let total = h.len();
    let sub_ms = if total > 0 {
        h.count_between(1, 999) as f64 / total as f64 * 100.0
    } else {
        0.0
    };

    let report = Report {
        concurrency,
        duration_s: (elapsed * 100.0).round() / 100.0,
        requests_ok: counters.ok.load(Ordering::Relaxed),
        requests_err: counters.err.load(Ordering::Relaxed),
        denied_rate_limited: counters.limited.load(Ordering::Relaxed),
        throughput_rps: ((counters.ok.load(Ordering::Relaxed) as f64 / elapsed) * 10.0).round()
            / 10.0,
        latency: Percentiles {
            p50_us: h.value_at_quantile(0.50),
            p90_us: h.value_at_quantile(0.90),
            p95_us: h.value_at_quantile(0.95),
            p99_us: h.value_at_quantile(0.99),
            p999_us: h.value_at_quantile(0.999),
            max_us: h.max(),
            mean_us: (h.mean() * 100.0).round() / 100.0,
        },
        sub_millisecond_share: (sub_ms * 10.0).round() / 10.0,
        note: "Closed-loop client on the same host as the server; latency is \
               end-to-end gRPC round trip including Redis."
            .into(),
    };

    let json = serde_json::to_string_pretty(&report)?;
    if let Some(dir) = std::path::Path::new(&out).parent() {
        std::fs::create_dir_all(dir)?;
    }
    std::fs::write(&out, &json)?;
    println!("{json}");
    println!("\nwrote {out}");

    Ok(())
}

struct Counters {
    ok: AtomicU64,
    err: AtomicU64,
    limited: AtomicU64,
}

async fn run_phase(
    endpoint: &str,
    tokens: &[String],
    dur: Duration,
    hist: Option<Arc<Mutex<Histogram<u64>>>>,
) -> Result<Arc<Counters>, Box<dyn std::error::Error>> {
    let counters = Arc::new(Counters {
        ok: AtomicU64::new(0),
        err: AtomicU64::new(0),
        limited: AtomicU64::new(0),
    });

    let deadline = Instant::now() + dur;
    let mut handles = Vec::with_capacity(tokens.len());

    for token in tokens {
        // One channel per worker: sharing a single HTTP/2 connection across all
        // workers would serialise on stream limits and measure the client.
        let mut client = AuthzClient::connect(endpoint.to_string()).await?;
        let token = token.clone();
        let counters = counters.clone();
        let hist = hist.clone();

        handles.push(tokio::spawn(async move {
            // Per-worker local histogram, merged once at the end — locking a
            // shared histogram per request would itself distort the numbers.
            let mut local = Histogram::<u64>::new_with_bounds(1, 60_000_000, 3).unwrap();

            while Instant::now() < deadline {
                let req = CheckRequest {
                    token: token.clone(),
                    route: "/internal/session".into(),
                    cost: 1,
                };
                let t0 = Instant::now();
                match client.check(req).await {
                    Ok(resp) => {
                        let us = t0.elapsed().as_micros() as u64;
                        let _ = local.record(us.max(1));
                        counters.ok.fetch_add(1, Ordering::Relaxed);
                        // decision 3 == DECISION_DENY_RATE_LIMITED
                        if resp.into_inner().decision == 3 {
                            counters.limited.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                    Err(_) => {
                        counters.err.fetch_add(1, Ordering::Relaxed);
                    }
                }
            }

            if let Some(h) = hist {
                let mut guard = h.lock().await;
                guard.add(&local).ok();
            }
        }));
    }

    for handle in handles {
        let _ = handle.await;
    }

    Ok(counters)
}
