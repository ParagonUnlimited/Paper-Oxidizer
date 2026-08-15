//! Configuration. FAIL CLOSED: every misconfiguration that could widen access
//! or masquerade as a different bug is a refusal to start with the variable
//! named -- the house style proven in v1, where each of these guards maps to a
//! real incident (auth fail-open, blank scans from a half-pasted R2 secret).

use anyhow::{bail, Context, Result};
use std::collections::HashMap;

pub struct Config {
    pub host: String,
    pub port: u16,
    pub loopback: bool,
    pub neon_url: String,
    /// name -> plaintext password from REVIEW_USERS. Hashed with argon2id into
    /// the users table at startup; never used for comparison directly.
    pub users: HashMap<String, String>,
    pub session_secret: Vec<u8>,
    pub r2: R2,
    pub web_dist: String,
}

pub struct R2 {
    pub bucket: String,
    pub endpoint: String,
    pub key_id: String,
    pub secret: String,
    pub prefix: String,
    pub sign_ttl_secs: u64,
}

fn env(k: &str) -> Option<String> {
    std::env::var(k).ok().map(|v| v.trim().to_string()).filter(|v| !v.is_empty())
}

pub fn load() -> Result<Config> {
    let host = env("HOST").unwrap_or_else(|| "127.0.0.1".into());
    let port: u16 = env("PORT").unwrap_or_else(|| "8779".into()).parse()
        .context("PORT is not a number")?;
    let loopback = matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1");

    let neon_url = env("NEON_DATABASE_URL")
        .context("REFUSING TO START: NEON_DATABASE_URL is not set")?;

    // REVIEW_USERS = "name:password,name:password". Same contract as v1 so the
    // Coolify env carries over unchanged.
    let mut users = HashMap::new();
    for pair in env("REVIEW_USERS").unwrap_or_default().split(',') {
        if let Some((name, pw)) = pair.trim().split_once(':') {
            let name = name.trim().to_lowercase();
            if !name.is_empty() && !pw.is_empty() {
                users.insert(name, pw.to_string());
            }
        }
    }
    if users.is_empty() && !loopback {
        bail!("REFUSING TO START: REVIEW_USERS is empty and HOST={host} is not \
               loopback. That combination would serve every document with no \
               authentication. Set REVIEW_USERS='name:password,name:password'.");
    }

    let session_secret = match env("SESSION_SECRET") {
        Some(s) => s.into_bytes(),
        None if users.is_empty() && loopback => b"loopback-solo-mode".to_vec(),
        None => bail!("REFUSING TO START: SESSION_SECRET is required when \
                       REVIEW_USERS is set. Without it the login cookie cannot \
                       be signed safely."),
    };

    // R2 is all-or-nothing. v2 has no local-render fallback -- it always runs
    // where the PDFs are not -- so a partial set must be a loud refusal, not a
    // silent fall-through to blank scans.
    let r2_vars = [
        ("R2_BUCKET", env("R2_BUCKET")),
        ("R2_ENDPOINT", env("R2_ENDPOINT")),
        ("R2_ACCESS_KEY_ID", env("R2_ACCESS_KEY_ID")),
        ("R2_SECRET_ACCESS_KEY", env("R2_SECRET_ACCESS_KEY")),
    ];
    let missing: Vec<&str> = r2_vars.iter()
        .filter(|(_, v)| v.is_none()).map(|(k, _)| *k).collect();
    if !missing.is_empty() {
        bail!("REFUSING TO START: R2 is required and these are missing: {}",
              missing.join(", "));
    }
    let mut it = r2_vars.into_iter().map(|(_, v)| v.unwrap());
    let r2 = R2 {
        bucket: it.next().unwrap(),
        endpoint: it.next().unwrap(),
        key_id: it.next().unwrap(),
        secret: it.next().unwrap(),
        prefix: env("R2_PREFIX").unwrap_or_else(|| "pages".into()),
        sign_ttl_secs: env("R2_SIGN_TTL").unwrap_or_else(|| "3600".into())
            .parse().context("R2_SIGN_TTL is not a number")?,
    };

    Ok(Config {
        host, port, loopback, neon_url, users, session_secret, r2,
        web_dist: env("WEB_DIST").unwrap_or_else(|| "web/dist".into()),
    })
}
