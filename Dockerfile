# Pinned rather than `latest` so builds are reproducible; bump deliberately.
FROM rust:1.88-slim AS build
WORKDIR /src

RUN apt-get update \
 && apt-get install -y --no-install-recommends protobuf-compiler pkg-config \
 && rm -rf /var/lib/apt/lists/*

COPY Cargo.toml Cargo.lock ./
COPY build.rs ./
COPY proto ./proto
COPY src ./src
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 10001 -m app
USER app
COPY --from=build /src/target/release/authz-service /service
EXPOSE 50051
ENTRYPOINT ["/service"]
