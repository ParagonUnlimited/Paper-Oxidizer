//! Paper-Oxidizer v2 — Rust review server.
//!
//! M1 skeleton: fail-closed config, direct-TLS Neon pool, argon2 login with
//! the v1 cookie scheme, R2 presigned image redirects, /healthz, and static
//! serving of the Vite build. The full API port (queue/doc/save/verdict/tags)
//! lands in M2 on top of these routes.

mod auth;
mod config;
mod db;
mod r2;

use auth::Auth;
use axum::extract::{Query, State};
use axum::http::{header, HeaderMap, StatusCode};
use axum::response::{IntoResponse, Redirect, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use serde::Deserialize;
use serde_json::json;
use std::sync::Arc;
use tower_http::services::{ServeDir, ServeFile};

struct App {
    auth: Auth,
    pool: deadpool_postgres::Pool,
    r2: r2::R2Client,
    loopback: bool,
}

type S = Arc<App>;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Both rustls crypto providers exist in the dependency graph (the AWS SDK
    // brings aws-lc-rs; our Postgres TLS uses ring), so rustls cannot pick one
    // automatically and panics at first use. Choose ring explicitly, once,
    // before anything opens a TLS connection.
    rustls::crypto::ring::default_provider()
        .install_default()
        .expect("no other rustls CryptoProvider is installed before this line");

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    // Any config error prints its REFUSING TO START reason and exits non-zero
    // -- a crash in the deploy log, never a silently weakened service.
    let cfg = match config::load() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("{e:#}");
            std::process::exit(1);
        }
    };

    let app = Arc::new(App {
        auth: Auth::new(&cfg.users, &cfg.session_secret, cfg.loopback),
        pool: db::pool(&cfg.neon_url)?,
        r2: r2::R2Client::new(&cfg.r2),
        loopback: cfg.loopback,
    });

    // Prove the database path at startup rather than on Jeff's first click.
    // Same fail-closed logic: an unreachable Neon is a crash with the real
    // error, not a queue that 500s.
    {
        let client = app.pool.get().await?;
        let n: i64 = client
            .query_one("select count(*) from document", &[])
            .await?
            .get(0);
        tracing::info!(documents = n, "Neon reachable (direct TLS)");
    }

    let static_dir = ServeDir::new(&cfg.web_dist)
        .fallback(ServeFile::new(format!("{}/index.html", cfg.web_dist)));

    let router = Router::new()
        .route("/healthz", get(healthz))
        .route("/login", post(login))
        .route("/logout", get(logout))
        .route("/whoami", get(whoami))
        .route("/page.img", get(page_img))
        .route("/api/queue", get(api_queue))
        .fallback_service(static_dir)
        .with_state(app.clone());

    let addr = format!("{}:{}", cfg.host, cfg.port);
    tracing::info!(%addr, "listening");
    let listener = tokio::net::TcpListener::bind(&addr).await?;
    axum::serve(listener, router).await?;
    Ok(())
}

async fn healthz() -> impl IntoResponse {
    Json(json!({"ok": true}))
}

fn who(app: &App, headers: &HeaderMap) -> Option<String> {
    let cookie = headers.get(header::COOKIE).and_then(|v| v.to_str().ok());
    app.auth.reviewer(cookie)
}

fn unauthorized() -> Response {
    (StatusCode::UNAUTHORIZED, Json(json!({"error": "login required"}))).into_response()
}

#[derive(Deserialize)]
struct LoginForm {
    user: String,
    pw: String,
}

async fn login(State(app): State<S>, Json(f): Json<LoginForm>) -> Response {
    let name = f.user.trim().to_lowercase();
    if !app.auth.verify_password(&name, &f.pw) {
        return (StatusCode::UNAUTHORIZED,
                Json(json!({"error": "wrong name or password"}))).into_response();
    }
    let cookie = format!(
        "rev={}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000{}",
        app.auth.make_cookie(&name),
        // Coolify's proxy terminates TLS in front; the cookie must never ride
        // a plaintext hop. Left off on loopback so local dev works over http.
        if app.loopback { "" } else { "; Secure" }
    );
    ([(header::SET_COOKIE, cookie)], Json(json!({"ok": true, "reviewer": name})))
        .into_response()
}

async fn logout() -> Response {
    ([(header::SET_COOKIE, "rev=; Path=/; Max-Age=0")],
     Redirect::to("/")).into_response()
}

async fn whoami(State(app): State<S>, headers: HeaderMap) -> Response {
    match who(&app, &headers) {
        Some(r) => Json(json!({"reviewer": r})).into_response(),
        None => unauthorized(),
    }
}

#[derive(Deserialize)]
struct PageImg {
    id: i64,
}

async fn page_img(
    State(app): State<S>,
    headers: HeaderMap,
    Query(q): Query<PageImg>,
) -> Response {
    if who(&app, &headers).is_none() {
        return unauthorized();
    }
    match app.r2.page_url(q.id).await {
        Ok(url) => Redirect::temporary(&url).into_response(),
        Err(e) => (StatusCode::BAD_GATEWAY,
                   Json(json!({"error": e.to_string()}))).into_response(),
    }
}

/// M1 stub: proves auth + pool + JSON end to end. M2 replaces the body with
/// the full v1 queue port (scoring, tiers, states, tags, notes).
async fn api_queue(State(app): State<S>, headers: HeaderMap) -> Response {
    if who(&app, &headers).is_none() {
        return unauthorized();
    }
    let client = match app.pool.get().await {
        Ok(c) => c,
        Err(e) => return (StatusCode::INTERNAL_SERVER_ERROR,
                          Json(json!({"error": e.to_string()}))).into_response(),
    };
    match client.query_one(
        "select count(*),
                count(*) filter (where meta->'ocr_review' is not null)
         from document", &[]).await
    {
        Ok(row) => {
            let total: i64 = row.get(0);
            let reviewed: i64 = row.get(1);
            Json(json!({"total": total, "reviewed": reviewed,
                        "note": "M1 stub — full queue lands in M2"})).into_response()
        }
        Err(e) => (StatusCode::INTERNAL_SERVER_ERROR,
                   Json(json!({"error": e.to_string()}))).into_response(),
    }
}
