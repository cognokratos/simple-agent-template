# syntax=docker/dockerfile:1.7

# The Rig-based Rust agent (rust-agent branch). Two stages: a full Rust
# toolchain to build, and a distroless runtime with nothing but the binary,
# the behavioural config and the licence texts — no shell, no package manager,
# no Python, no NAT. Health checks and the Makefile's in-container probes use
# the binary's own `probe` subcommand.

# rig-core 0.44 requires Rust >= 1.95; pinned rather than `rust:1` so a
# toolchain release cannot change what is built.
FROM rust:1.97-bookworm AS builder
WORKDIR /app

# Dependencies first, against stub sources, so a source-only change reuses
# the compiled dependency layer.
COPY Cargo.toml Cargo.lock ./
RUN mkdir -p src \
    && printf 'fn main() {}\n' > src/main.rs \
    && printf '' > src/lib.rs
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    cargo build --release --locked && rm -rf src

COPY src ./src
# The target directory is a persistent BuildKit cache, so the stub build's
# artefacts can look newer than the real sources; touching them forces this
# package to rebuild while keeping cached dependencies.
RUN --mount=type=cache,target=/usr/local/cargo/registry,sharing=locked \
    --mount=type=cache,target=/usr/local/cargo/git,sharing=locked \
    --mount=type=cache,target=/app/target,sharing=locked \
    touch src/main.rs src/lib.rs \
    && cargo build --release --locked \
    && cp /app/target/release/tickets-agent /tmp/tickets-agent


FROM gcr.io/distroless/cc-debian12:nonroot AS runtime
WORKDIR /app
COPY --from=builder --chmod=0755 /tmp/tickets-agent /usr/local/bin/tickets-agent
COPY config.yml /app/config.yml
# Licence texts and notices travel with the image.
COPY LICENSE THIRD_PARTY_NOTICES.md /usr/share/doc/tickets-agent/
COPY LICENSES /usr/share/doc/tickets-agent/LICENSES

# Stamped last, so a new commit does not invalidate the layers above.
ARG AGENT_BUILD_COMMIT=unknown
ENV AGENT_BUILD_COMMIT=${AGENT_BUILD_COMMIT} \
    AGENT_CONFIG_PATH=/app/config.yml \
    AGENT_BIND_ADDRESS=0.0.0.0:8000

USER nonroot:nonroot
EXPOSE 8000
ENTRYPOINT ["/usr/local/bin/tickets-agent"]
CMD ["serve"]
