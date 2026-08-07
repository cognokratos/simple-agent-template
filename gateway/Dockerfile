# syntax=docker/dockerfile:1.7

FROM rust:1.88-bookworm AS builder
WORKDIR /app

COPY Cargo.toml ./
RUN mkdir -p src && printf 'fn main() {}\n' > src/main.rs
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    cargo build --release && rm -f src/main.rs

COPY src ./src
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    touch src/main.rs \
    && cargo build --release \
    && cp /app/target/release/alerts-auth-gateway /tmp/alerts-auth-gateway

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /tmp/alerts-auth-gateway /usr/local/bin/alerts-auth-gateway
USER 65532:65532
EXPOSE 8081
ENTRYPOINT ["/usr/local/bin/alerts-auth-gateway"]
