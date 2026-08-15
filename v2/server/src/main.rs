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
mod routes;
mod scoring;

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
    // Idempotent DDL + the one-time backfill of page-level approvals from
    // v1's document-level Finals.
    routes::migrate(&app.pool).await?;

    let static_dir = ServeDir::new(&cfg.web_dist)
        .fallback(ServeFile::new(format!("{}/index.html", cfg.web_dist)));

    let router = Router::new()
        .route("/healthz", get(healthz))
        .route("/login", post(login))
        .route("/logout", get(logout))
        .route("/whoami", get(whoami))
        .route("/page.img", get(page_img))
        .route("/api/queue", get(api_queue))
        .route("/api/doc", get(api_doc))
        .route("/api/save", post(api_save))
        .route("/api/verdict", post(api_verdict))
        .route("/api/tags", post(api_tags))
        .route("/api/page_verdict", post(api_page_verdict))
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

fn err500(e: anyhow::Error) -> Response {
    (StatusCode::INTERNAL_SERVER_ERROR,
     Json(json!({"error": e.to_string()}))).into_response()
}

async fn api_queue(State(app): State<S>, headers: HeaderMap) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    match routes::queue(&app.pool, &me).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err500(e),
    }
}

#[derive(Deserialize)]
struct DocQ { id: i64 }

async fn api_doc(State(app): State<S>, headers: HeaderMap,
                 Query(q): Query<DocQ>) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    match routes::doc(&app.pool, q.id, &me).await {
        Ok(v) => Json(v).into_response(),
        Err(e) => err500(e),
    }
}

#[derive(Deserialize)]
struct SaveBody {
    #[serde(rename = "pageId")]
    page_id: i64,
    #[serde(default)]
    text: String,
    #[serde(default)]
    tables: serde_json::Value,
    #[serde(default)]
    note: String,
}

async fn api_save(State(app): State<S>, headers: HeaderMap,
                  Json(b): Json<SaveBody>) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    match routes::save_page(&app.pool, b.page_id, &b.text, &b.tables, &b.note, &me).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err500(e),
    }
}

#[derive(Deserialize)]
struct VerdictBody { id: i64, verdict: Option<String> }

async fn api_verdict(State(app): State<S>, headers: HeaderMap,
                     Json(b): Json<VerdictBody>) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    if !matches!(b.verdict.as_deref(),
                 None | Some("submitted") | Some("approved") | Some("hold")) {
        return (StatusCode::BAD_REQUEST,
                Json(json!({"error": "bad verdict"}))).into_response();
    }
    match routes::verdict(&app.pool, b.id, b.verdict.as_deref(), &me).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err500(e),
    }
}

#[derive(Deserialize)]
struct TagsBody { id: i64, tags: Vec<String> }

async fn api_tags(State(app): State<S>, headers: HeaderMap,
                  Json(b): Json<TagsBody>) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    if b.tags.iter().any(|t| t.len() > 40) {
        return (StatusCode::BAD_REQUEST,
                Json(json!({"error": "bad tags"}))).into_response();
    }
    let _ = me;
    match routes::set_tags(&app.pool, b.id, &b.tags).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err500(e),
    }
}

#[derive(Deserialize)]
struct PageVerdictBody {
    #[serde(rename = "pageId")]
    page_id: i64,
    status: Option<String>,
}

async fn api_page_verdict(State(app): State<S>, headers: HeaderMap,
                          Json(b): Json<PageVerdictBody>) -> Response {
    let Some(me) = who(&app, &headers) else { return unauthorized() };
    if !matches!(b.status.as_deref(), None | Some("approved") | Some("flagged")) {
        return (StatusCode::BAD_REQUEST,
                Json(json!({"error": "bad status"}))).into_response();
    }
    match routes::page_verdict(&app.pool, b.page_id, b.status.as_deref(), &me).await {
        Ok(()) => Json(json!({"ok": true})).into_response(),
        Err(e) => err500(e),
    }
}
