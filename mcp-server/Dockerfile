# syntax=docker/dockerfile:1.7

FROM rust:1-bookworm AS builder
WORKDIR /app

COPY Cargo.toml ./
RUN mkdir src && printf 'fn main() {}\n' > src/main.rs
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    cargo build --release && rm -rf src

COPY src ./src
# The first build compiles a dummy main.rs to cache dependencies. The target
# directory is a persistent BuildKit cache, so its dummy executable can appear
# newer than the copied real source. Touch the real entrypoint inside the
# mounted build step to force Cargo to rebuild this package while retaining
# cached dependency artifacts.
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    touch src/main.rs \
    && cargo build --release \
    && cp /app/target/release/etf-mcp-server /tmp/etf-mcp-server


FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder --chmod=0755 /tmp/etf-mcp-server /usr/local/bin/etf-mcp-server
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/etf-mcp-server"]
