// Paper-Oxidizer v2 — M1 shell.
//
// Login against the Rust server, then a placeholder shell proving the
// authenticated round trip (whoami + queue stub). The full four-pane review
// UI ports over in M2 on top of this scaffolding.

const app = document.getElementById('app')!;

// Every dynamic value that reaches innerHTML goes through this. Document keys
// and tags are real-world strings; the M2 port renders hundreds of them.
const esc = (s: unknown): string =>
  String(s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

type Whoami = { reviewer: string };

async function whoami(): Promise<Whoami | null> {
  const r = await fetch('/whoami');
  return r.ok ? r.json() : null;
}

function loginView(error = ''): void {
  app.innerHTML = `
    <div class="login-wrap"><div class="login-card">
      <h1>OCR review</h1>
      <label>Name</label><input id="user" autocapitalize="off" autofocus>
      <label>Password</label><input id="pw" type="password">
      <button class="primary" id="go">Sign in</button>
      ${error ? `<div class="login-err">${error}</div>` : ''}
    </div></div>`;
  const submit = async () => {
    const user = (document.getElementById('user') as HTMLInputElement).value;
    const pw = (document.getElementById('pw') as HTMLInputElement).value;
    const r = await fetch('/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user, pw }),
    });
    if (r.ok) boot();
    else loginView('Wrong name or password.');
  };
  document.getElementById('go')!.addEventListener('click', submit);
  app.addEventListener('keydown', e => {
    if ((e as KeyboardEvent).key === 'Enter') submit();
  }, { once: true });
}

async function shell(me: Whoami): Promise<void> {
  const q = await (await fetch('/api/queue')).json();
  app.innerHTML = `
    <div style="padding:24px;max-width:640px;margin:0 auto">
      <h1 style="font:590 17px/1.3 var(--font)">Paper-Oxidizer v2</h1>
      <p style="color:var(--dim)">Signed in as <b style="color:var(--fg2)">${esc(me.reviewer)}</b>
        · <a href="/logout" style="color:var(--accent-h)">log out</a></p>
      <p>Corpus: <b>${esc(q.total)}</b> documents, <b>${esc(q.reviewed)}</b> carrying a verdict.</p>
      <p class="tag">M1 skeleton — the four-pane review UI arrives in M2</p>
    </div>`;
}

async function boot(): Promise<void> {
  const me = await whoami();
  if (me) shell(me);
  else loginView();
}

boot();
