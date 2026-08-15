# Paper-Oxidizer v2 -- Rust server + Vite web, one small runtime image.
#
# Same properties as v1's image: holds NO document data (text/verdicts in Neon,
# pixels in R2 behind signed URLs), so it can be rebuilt or moved freely.

# ---- web build ----
FROM node:24-slim AS web
WORKDIR /w
COPY web/package.json web/package-lock.json* ./
RUN npm install --no-fund --no-audit
COPY web/ .
RUN npm run build

# ---- rust build ----
FROM rust:1-slim AS rust
WORKDIR /s
# Dependency layer first so code edits do not recompile the world.
COPY Cargo.toml ./
COPY server/Cargo.toml server/
RUN mkdir -p server/src && echo "fn main(){}" > server/src/main.rs \
    && cargo build --release && rm -rf server/src
COPY server/src server/src
# touch so cargo sees the real sources as newer than the stub build
RUN touch server/src/main.rs && cargo build --release

# ---- runtime ----
FROM debian:bookworm-slim
# NO apt here, deliberately. The VPS's Cloudflare Gateway matches egress on
# SNI, and plain-HTTP apt (deb.debian.org:80) carries none -- the block-all
# rule drops it and the build dies. Same failure class as Postgres negotiated
# TLS. The rust build stage already has the CA bundle installed (cargo needs
# HTTPS), so copy it: zero network in this stage, and TLS (Neon via compiled-in
# webpki-roots, R2 via system roots) still verifies.
COPY --from=rust /etc/ssl/certs /etc/ssl/certs
WORKDIR /app
COPY --from=rust /s/target/release/paper-oxidizer-server /app/
COPY --from=web /w/dist /app/web/dist

ENV HOST=0.0.0.0 \
    PORT=8779 \
    WEB_DIST=/app/web/dist

EXPOSE 8779

# /dev/tcp is a BASH builtin -- /bin/sh on Debian is dash, where this probe
# would fail on every run and restart-loop a healthy container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["/bin/bash", "-c", \
       "exec 3<>/dev/tcp/127.0.0.1/8779 && printf 'GET /healthz HTTP/1.0\\r\\n\\r\\n' >&3 && grep -q '200' <&3"]

CMD ["/app/paper-oxidizer-server"]
