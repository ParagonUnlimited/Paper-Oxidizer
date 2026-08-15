//! Neon connection pool.
//!
//! DIRECT TLS OR NOTHING. Postgres's default handshake opens plaintext TCP,
//! sends an SSLRequest byte-packet, and only then starts TLS -- so the first
//! bytes on the wire are not a ClientHello and carry no SNI. Cloudflare
//! Gateway on the VPS classifies egress by SNI and drops what it cannot name;
//! the symptom is "server closed the connection unexpectedly" against every
//! Neon IP, which reads like Neon being down. v1 fixed this with
//! PGSSLNEGOTIATION=direct. Here it is `SslNegotiation::Direct` plus ALPN
//! "postgresql", per RFC: direct TLS requires the ALPN token.

use anyhow::{Context, Result};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use std::sync::Arc;
use tokio_postgres::config::SslNegotiation;

pub fn pool(neon_url: &str) -> Result<Pool> {
    let mut cfg: tokio_postgres::Config =
        neon_url.parse().context("NEON_DATABASE_URL did not parse")?;
    cfg.ssl_negotiation(SslNegotiation::Direct);
    // The shared connection string carries channel_binding=require, and Neon's
    // POOLER completes SCRAM without channel binding. libpq tolerated that;
    // tokio-postgres enforces `require` strictly and fails with "server did
    // not use channel binding". Prefer keeps channel binding wherever the
    // server actually offers it, without refusing the pooler. TLS itself
    // remains mandatory and certificate-verified either way.
    cfg.channel_binding(tokio_postgres::config::ChannelBinding::Prefer);

    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let mut tls = rustls::ClientConfig::builder()
        .with_root_certificates(roots)
        .with_no_client_auth();
    // Direct TLS to Postgres is only valid with this exact ALPN token.
    tls.alpn_protocols = vec![b"postgresql".to_vec()];

    let connector = tokio_postgres_rustls::MakeRustlsConnect::new(Arc::new(tls).as_ref().clone());
    let mgr = Manager::from_config(
        cfg,
        connector,
        ManagerConfig { recycling_method: RecyclingMethod::Fast },
    );
    Pool::builder(mgr)
        .max_size(8)
        .build()
        .context("could not build Postgres pool")
}
