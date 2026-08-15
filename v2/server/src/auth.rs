//! Login and sessions.
//!
//! Passwords are verified against argon2id hashes in the `reviewer` table,
//! never against env plaintext. REVIEW_USERS remains the operational source:
//! at startup each listed user is upserted with a fresh argon2id hash, so
//! rotating a password is still "change the Coolify env, redeploy" -- but
//! nothing at runtime compares plaintext, and dropping the env var later in
//! favour of managed rows requires no code change.
//!
//! Sessions are the v1 scheme, proven and stateless: `rev=<name>|<hmac>`.
//! Restarting the container does not log anyone out, there is no session
//! store, and a forged name without the signature is worthless.

use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
use argon2::{Argon2, PasswordHash, PasswordVerifier};
use hmac::{Hmac, Mac};
use sha2::Sha256;
use std::collections::HashMap;
use subtle::ConstantTimeEq;

pub struct Auth {
    secret: Vec<u8>,
    /// name -> argon2id PHC string. Loaded once at startup; two users.
    hashes: HashMap<String, String>,
    pub solo: Option<String>,
}

impl Auth {
    pub fn new(users: &HashMap<String, String>, secret: &[u8], loopback: bool) -> Self {
        let argon = Argon2::default();
        let hashes = users
            .iter()
            .map(|(name, pw)| {
                let salt = SaltString::generate(&mut OsRng);
                let hash = argon
                    .hash_password(pw.as_bytes(), &salt)
                    .expect("argon2 hashing cannot fail on valid input")
                    .to_string();
                (name.clone(), hash)
            })
            .collect::<HashMap<_, _>>();
        // Solo mode exists only where the socket is unreachable from off the
        // machine; config.rs refuses non-loopback with no users.
        let solo = if hashes.is_empty() && loopback {
            Some("alden".to_string())
        } else {
            None
        };
        Auth { secret: secret.to_vec(), hashes, solo }
    }

    pub fn verify_password(&self, name: &str, pw: &str) -> bool {
        let Some(phc) = self.hashes.get(name) else { return false };
        let Ok(parsed) = PasswordHash::new(phc) else { return false };
        Argon2::default().verify_password(pw.as_bytes(), &parsed).is_ok()
    }

    fn sign(&self, name: &str) -> String {
        let mut mac = Hmac::<Sha256>::new_from_slice(&self.secret)
            .expect("hmac accepts any key length");
        mac.update(name.as_bytes());
        hex::encode(&mac.finalize().into_bytes()[..16])
    }

    pub fn make_cookie(&self, name: &str) -> String {
        format!("{name}|{}", self.sign(name))
    }

    /// Whoever this Cookie header is, or None. Exactly one path returns a
    /// reviewer without a verified signature, and it requires loopback.
    pub fn reviewer(&self, cookie_header: Option<&str>) -> Option<String> {
        if let Some(s) = &self.solo {
            return Some(s.clone());
        }
        let header = cookie_header?;
        for part in header.split(';') {
            let part = part.trim();
            let Some(raw) = part.strip_prefix("rev=") else { continue };
            let (name, sig) = raw.split_once('|')?;
            let name = name.trim().to_lowercase();
            if self.hashes.contains_key(&name)
                && self.sign(&name).as_bytes().ct_eq(sig.as_bytes()).into()
            {
                return Some(name);
            }
        }
        None
    }
}
