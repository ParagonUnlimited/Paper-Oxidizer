// Paper-Oxidizer v2 — login gate, then the review surface.

import './review.css';
import { esc, mount } from './review';

const app = document.getElementById('app')!;

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

async function boot(): Promise<void> {
  const me = await whoami();
  if (me) mount(app, esc(me.reviewer));
  else loginView();
}

boot();
