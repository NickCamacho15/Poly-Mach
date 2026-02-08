# Multi-stage build for minimal production image.
# Stage 1: Build the Rust binary.
FROM rust:1.83-slim-bookworm AS builder

RUN apt-get update && apt-get install -y pkg-config libssl-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY Cargo.toml Cargo.lock ./
COPY src/ ./src/

# Build release binary with LTO.
RUN cargo build --release

# Stage 2: Minimal runtime image.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates libssl3 && rm -rf /var/lib/apt/lists/*

# Create non-root user and runtime dirs.
RUN useradd -m botuser && mkdir -p /app/logs /app/data && chown -R botuser:botuser /app

WORKDIR /app
COPY --from=builder /build/target/release/polymarket-us-bot /app/polymarket-us-bot

USER botuser

# The .env file or environment variables must be provided at runtime.
# docker run --env-file /path/to/.env polymarket-us-bot
CMD ["/app/polymarket-us-bot"]
