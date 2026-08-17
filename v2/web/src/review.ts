// The four-pane review surface — a TypeScript port of v1's proven vanilla JS,
// plus v2's page-level approval. Every dynamic value that reaches innerHTML
// goes through esc().

export const esc = (s: unknown): string =>
  String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!));

// ---- types (the API shapes, verbatim from the Rust server) ----------------
interface QueueDoc {
  id: number; key: string; pdf: string | null;
  pages: number; bad: number; words: number; tbad: number; twords: number;
  maxRep: number; dupRows: number;
  verdict: string | null; peers: Record<string, string>;
  tags: string[]; noted: number; edited: number;
  rate: number; allRate: number; thin: boolean; repeats: boolean;
  flagged: boolean; conf: 'low' | 'medium' | 'high'; state: string;
  pagesApproved: number;
}
interface Span { s: number; e: number; c: number }
interface TableT {
  id: string; html: string; saved: string | null;
  suspect: string[]; bad: number; words: number;
}
interface Other { by: string; text: string; note: string; when: string }
interface Approval { by: string; status: string; when?: string }
interface Page {
  pageId: number; docPage: number; text: string; spans: Span[];
  src?: string;                      // "mistral" or adjust:reocr:v2 etc.
  corrected: string | null; note: string; others: Other[];
  tables: TableT[]; bad: number; words: number; approvals: Approval[];
}
interface Doc { id: number; pdf: string | null; pages: Page[] }

// ---- state -----------------------------------------------------------------
let Q: QueueDoc[] = [];
let D: Doc | null = null;
let i = 0;
let cur = 0;
let dirty = false;
let ME = '';

const $ = (x: string) => document.getElementById(x)!;

async function api<T>(path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, body === undefined ? undefined : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

// ---- filters + counts ------------------------------------------------------
const FILTERS: Record<string, [string, (d: QueueDoc) => boolean]> = {
  all:       ['All',        () => true],
  flagged:   ['Flagged',    d => d.flagged],
  loops:     ['⟳ Loops',    d => d.repeats],
  low:       ['Low',        d => d.conf === 'low'],
  medium:    ['Med',        d => d.conf === 'medium'],
  high:      ['High',       d => d.conf === 'high'],
  unrev:     ['Unreviewed', d => d.state === 'unreviewed'],
  submitted: ['Submitted',  d => d.state === 'submitted'],
  final:     ['Final ✓',    d => d.state === 'approved'],
  held:      ['Held',       d => d.state === 'hold'],
  mine:      ['Mine',       d => !!d.verdict],
  noted:     ['✎ Noted',    d => d.noted > 0],
};
let FILTER = 'flagged';
let SEARCH = '';

function visible(): Array<[QueueDoc, number]> {
  const f = FILTERS[FILTER][1];
  const s = SEARCH.toLowerCase();
  return Q.map((d, n) => [d, n] as [QueueDoc, number]).filter(([d]) =>
    f(d) && (!s || (d.key || '').toLowerCase().includes(s) ||
             (d.tags || []).some(t => t.toLowerCase().includes(s))));
}

function drawFilters(): void {
  $('filters').innerHTML = Object.entries(FILTERS).map(([k, [label]]) => {
    const n = Q.filter(FILTERS[k][1]).length;
    return `<span class="chip ${k === FILTER ? 'on' : ''}" data-f="${k}"
      title="${n} document(s)">${esc(label)}</span>`;
  }).join('');
  $('filters').querySelectorAll('.chip').forEach(el =>
    el.addEventListener('click', () => {
      FILTER = (el as HTMLElement).dataset.f!;
      drawFilters(); drawList();
    }));
}

// The pipeline readout: effective states across both reviewers, hold trumps.
// The pending count is the union of "submitted" + "held" + "approved" —
// anything that left the queue — because those documents are the others'
// workload already, and the to-review count should not include them.
function drawCounts(): void {
  const c = { r: 0, s: 0, f: 0, h: 0, p: 0 };
  for (const d of Q) {
    if (d.state === 'approved') c.f++;
    else if (d.state === 'hold') c.h++;
    else if (d.state === 'submitted') c.s++;
    else if (d.flagged) c.r++;
  }
  c.p = c.r + c.s + c.f + c.h;
  $('counts').innerHTML =
    `<span class=cr><b>${c.r}</b> to review</span>` +
    `<span class=cs><b>${c.s}</b> submitted</span>` +
    `<span class=cf><b>${c.f}</b> final</span>` +
    `<span class=ch><b>${c.h}</b> held</span>`;
}

function effState(d: QueueDoc): string {
  const vs = new Set([d.verdict, ...Object.values(d.peers || {})].filter(Boolean));
  return vs.has('hold') ? 'hold' : vs.has('approved') ? 'approved'
       : vs.has('submitted') ? 'submitted' : 'unreviewed';
}

// TWO PEOPLE work this queue at once. Each browser loaded Q once at login and
// only ever mutated it from its own clicks, so one reviewer's submissions were
// invisible to the other until a hard reload -- counts drifted apart within
// minutes of real two-person use. The queue is re-pulled on window focus,
// every 45s, and after each of our own verdicts; the open document pane and
// any in-progress edits are deliberately untouched (only the list, counts,
// tags and states refresh). `cur` is re-anchored by document id because a
// refresh can reorder rows.
let refreshing = false;
async function refreshQueue(): Promise<void> {
  if (refreshing) return;
  refreshing = true;
  try {
    const fresh = await api<QueueDoc[]>('/api/queue');
    const curId = Q[cur]?.id;
    Q = fresh;
    if (curId != null) {
      const n = Q.findIndex(d => d.id === curId);
      if (n >= 0) cur = n;
    }
    drawCounts(); drawFilters(); drawList(); drawTags();
  } catch { /* transient network failure -- next tick will retry */ }
  finally { refreshing = false; }
}

const VICON: Record<string, string> = { approved: '✓ ', hold: '⏸ ', submitted: '↑ ' };

function drawList(): void {
  const rows = visible();
  $('list').innerHTML = rows.map(([d, n]) =>
    `<div class="q ${n === cur ? 'on' : ''} ${d.state === 'approved' ? 'done' : ''} ${
       d.state === 'hold' ? 'held' : ''} ${d.state === 'submitted' ? 'subm' : ''}"
       data-n="${n}">
     <span class=k>${VICON[d.verdict || ''] || ''}${d.repeats ? '⟳ ' : ''}${
       d.thin ? '· ' : ''}${esc(d.key)}${
       Object.keys(d.peers || {}).length ? ` <span class=pk>${
         Object.entries(d.peers).map(([w, v]) =>
           `${esc(w)}:${(VICON[v] || '?').trim()}`).join(' ')}</span>` : ''}${
       (d.tags || []).map(t => `<span class=tag>${esc(t)}</span>`).join('')}</span>
     <span class=m>${d.repeats
       ? `<b style="color:var(--warn)">${d.maxRep} identical rows in a row</b> · ` : ''}${
       d.rate}% text${d.twords ? ` · ${d.allRate}% w/tables` : ''} · ${
       d.bad + d.tbad}/${d.words + d.twords} words · ${
       d.pagesApproved}/${d.pages}p ✓${
       d.noted ? ' · ✎' : ''}${d.thin ? ' · thin, rate unreliable' : ''}</span></div>`
  ).join('') ||
  '<div class=q style="color:var(--dim2)">nothing matches this filter</div>';
  $('list').querySelectorAll('.q[data-n]').forEach(el =>
    el.addEventListener('click', () =>
      openDoc(Number((el as HTMLElement).dataset.n))));
}

// ---- scan zoom / pan (ported intact from v1) -------------------------------
let Z = 0, ox = 0, oy = 0, fitS = 1;
let drag: { x: number; y: number } | null = null;

function apply(): void {
  const sc = Z || fitS;
  ($('img') as HTMLImageElement).style.transform =
    `translate(${ox}px,${oy}px) scale(${sc})`;
  $('zl').textContent = Z ? Math.round(sc * 100) + '%' : 'fit';
}
function zfit(): void {
  Z = 0; ox = oy = 0;
  const w = $('imgwrap'), im = $('img') as HTMLImageElement;
  if (im.naturalWidth) {
    fitS = Math.min(w.clientWidth / im.naturalWidth,
                    w.clientHeight / im.naturalHeight);
    ox = (w.clientWidth - im.naturalWidth * fitS) / 2; oy = 0;
  }
  apply();
}
function setZ(ns: number, cx: number, cy: number): void {
  const w = $('imgwrap').getBoundingClientRect();
  const px = (cx - w.left - ox) / (Z || fitS);
  const py = (cy - w.top - oy) / (Z || fitS);
  Z = Math.max(0.1, Math.min(8, ns));
  ox = cx - w.left - px * Z; oy = cy - w.top - py * Z; apply();
}
function zoom(d: number): void {
  const w = $('imgwrap').getBoundingClientRect();
  setZ((Z || fitS) * (d > 0 ? 1.25 : 0.8), w.left + w.width / 2, w.top + w.height / 2);
}

function wireZoom(): void {
  $('imgwrap').addEventListener('wheel', e => {
    e.preventDefault();
    setZ((Z || fitS) * ((e as WheelEvent).deltaY < 0 ? 1.15 : 0.87),
         (e as WheelEvent).clientX, (e as WheelEvent).clientY);
  }, { passive: false });
  $('imgwrap').addEventListener('mousedown', e => {
    drag = { x: (e as MouseEvent).clientX - ox, y: (e as MouseEvent).clientY - oy };
    $('imgwrap').classList.add('drag');
  });
  addEventListener('mousemove', e => {
    if (!drag) return;
    ox = (e as MouseEvent).clientX - drag.x; oy = (e as MouseEvent).clientY - drag.y;
    apply();
  });
  addEventListener('mouseup', () => { drag = null; $('imgwrap').classList.remove('drag'); });
  $('imgwrap').addEventListener('dblclick', zfit);
  $('img').addEventListener('load', () => { if (!Z) zfit(); else apply(); });
  addEventListener('resize', () => { if (!Z) zfit(); });
}

// ---- text rendering: suspect marks, inline tables, diff --------------------
function marks(t: string, sp: Span[]): string {
  if (!sp.length) return esc(t);
  const sorted = sp.slice().sort((a, b) => a.s - b.s);
  let o = '', p = 0;
  for (const s of sorted) {
    if (s.s < p) continue;
    o += esc(t.slice(p, s.s)) +
      `<mark title="confidence ${s.c}">` + esc(t.slice(s.s, s.e)) + '</mark>';
    p = s.e;
  }
  return o + esc(t.slice(p));
}

// Table HTML arrives from Mistral OCR — i.e. derived from scanned DOCUMENT
// CONTENT, which nobody vetted as markup. Before it touches innerHTML it is
// rebuilt through a strict allowlist: table structure tags survive as
// structure, everything else survives only as text. v1 injected this raw;
// v2 does not.
const TABLE_TAGS = new Set(['TABLE', 'THEAD', 'TBODY', 'TFOOT', 'TR', 'TD', 'TH',
                            'CAPTION', 'COL', 'COLGROUP']);
function sanitizeTable(html: string): string {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const walk = (node: Node): string => {
    if (node.nodeType === Node.TEXT_NODE) return esc(node.textContent);
    if (node.nodeType !== Node.ELEMENT_NODE) return '';
    const el = node as Element;
    const inner = Array.from(el.childNodes).map(walk).join('');
    if (!TABLE_TAGS.has(el.tagName)) return inner;   // unwrap unknown tags
    const tag = el.tagName.toLowerCase();
    const span = ['td', 'th'].includes(tag)
      ? ['colspan', 'rowspan'].map(a => {
          const v = el.getAttribute(a);
          return v && /^\d+$/.test(v) ? ` ${a}="${v}"` : '';
        }).join('')
      : '';
    return `<${tag}${span}>${inner}</${tag}>`;
  };
  return Array.from(doc.body.childNodes).map(walk).join('');
}

// The markdown carries only "[tbl-0.html](tbl-0.html)" where a table belongs;
// swap each placeholder for the real table so the read pane shows the whole
// page in order.
function inlineTables(html: string, p: Page): string {
  (p.tables || []).forEach((t, n) => {
    const id = t.id || `tbl-${n}.html`;
    const q = id.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    html = html.replace(new RegExp('\\[' + q + '\\]\\(' + q + '\\)', 'g'),
      `<div class=tw><div class=tl>${esc(id)} — ${t.bad}/${t.words}` +
      ' suspect · edit it in the next pane</div>' +
      sanitizeTable(t.saved != null ? t.saved : t.html) + '</div>');
  });
  return html;
}

function markCells(root: HTMLElement, p: Page): void {
  const bad = new Set<string>();
  (p.tables || []).forEach(t => t.suspect.forEach(s => bad.add(s)));
  root.querySelectorAll('td,th').forEach(c => {
    const v = c.textContent?.trim() || '';
    if (v && bad.has(v)) c.innerHTML = '<mark>' + esc(v) + '</mark>';
  });
}

// word-level LCS diff, ported intact — a page of OCR never makes O(n*m) matter
function diff(a: string, b: string): string {
  const A = a.split(/(\s+)/), B = b.split(/(\s+)/);
  const m = A.length, n = B.length;
  const L: Int32Array[] = Array.from({ length: m + 1 }, () => new Int32Array(n + 1));
  for (let x = m - 1; x >= 0; x--)
    for (let y = n - 1; y >= 0; y--)
      L[x][y] = A[x] === B[y] ? L[x + 1][y + 1] + 1 : Math.max(L[x + 1][y], L[x][y + 1]);
  let x = 0, y = 0, o = '';
  while (x < m && y < n) {
    if (A[x] === B[y]) { o += esc(A[x]); x++; y++; }
    else if (L[x + 1][y] >= L[x][y + 1]) { o += '<del>' + esc(A[x]) + '</del>'; x++; }
    else { o += '<ins>' + esc(B[y]) + '</ins>'; y++; }
  }
  return o + '<del>' + esc(A.slice(x).join('')) + '</del>'
           + '<ins>' + esc(B.slice(y).join('')) + '</ins>';
}

// ---- editable tables --------------------------------------------------------
function drawTables(p: Page): void {
  const host = $('tbl');
  host.innerHTML = '';
  let tb = 0, tw = 0;
  (p.tables || []).forEach((t, n) => {
    tb += t.bad; tw += t.words;
    const h = document.createElement('div');
    h.className = 'th';
    h.innerHTML = `<span>table ${esc(t.id || n)} — editable</span>
                   <span>${t.bad}/${t.words} suspect</span>`;
    const box = document.createElement('div');
    box.innerHTML = sanitizeTable(t.saved != null ? t.saved : t.html);
    box.dataset.id = t.id || `tbl-${n}`;
    box.querySelectorAll('td,th').forEach(c => {
      c.setAttribute('contenteditable', 'true');
      const v = c.textContent?.trim() || '';
      if (v && t.suspect.includes(v)) c.innerHTML = '<mark>' + esc(v) + '</mark>';
    });
    box.addEventListener('input', mark);
    host.appendChild(h); host.appendChild(box);
  });
  $('tc').textContent = (p.tables || []).length
    ? `${p.tables.length} table(s) · ${tb}/${tw} suspect` : '';
}

// Strip highlight wrappers before storing — a save never persists markup the
// review tool added for display.
function readTables(): Array<{ id: string; format: string; content: string }> {
  return Array.from($('tbl').children)
    .filter(el => (el as HTMLElement).dataset?.id)
    .map(el => {
      const c = el.cloneNode(true) as HTMLElement;
      c.querySelectorAll('mark').forEach(m => m.replaceWith(m.textContent || ''));
      c.querySelectorAll('[contenteditable]').forEach(x =>
        x.removeAttribute('contenteditable'));
      return { id: (el as HTMLElement).dataset.id!, format: 'html', content: c.innerHTML };
    });
}

// ---- dirty tracking ----------------------------------------------------------
//
// IMPORTANT: Submit and Finalize are two different actions with two different
// gates. The next-document-class crash on 2026-08-16 was caused by clicking
// the Finalize button when the intent was to Submit (the same Teal button was
// the largest and brightest). The fix is to make the verbs un-mistakeable,
// gate Submit on "every page has been touched" (the worker needs an
// artifact of intent per page), and gate Finalize on "every page has been
// both submitted and finalized".
//
// Page states derive from these flags:
//   saved      — a human edit row exists for this page
//   submitted  — page_verdict submitted; the worker has run
//   approved   — page_verdict approved; this page is Finalized
// The whole document is Finalizable only when every page is approved.
function reviewedPages(): { saved: number; submitted: number; approved: number } {
  if (!D) return { saved: 0, submitted: 0, approved: 0 };
  let saved = 0, submitted = 0, approved = 0;
  for (const p of D.pages) {
    if (p.corrected != null) saved++;
    if ((p.approvals || []).some(a => a.status === 'submitted')) submitted++;
    if ((p.approvals || []).some(a => a.status === 'approved')) approved++;
  }
  return { saved, submitted, approved };
}

function mark(): void {
  dirty = true;
  $('st').textContent = 'unsaved';
  refreshActionBar();
}
function unmark(): void {
  dirty = false;
  $('st').textContent = 'saved ' + new Date().toLocaleTimeString();
  refreshActionBar();
}

// The action bar is the part Jeff misread. Three distinct verbs, three
// distinct colours, three distinct gates. Sizes are scaled up from the v1
// defaults because the original buttons were too small alongside the 4-pane
// content (the count text was nearly illegible on a 13" screen).
function refreshActionBar(): void {
  const r = reviewedPages();
  const total = D ? D.pages.length : 0;
  const allTouched = r.saved === total && total > 0;
  const allFinal = r.approved === total && total > 0;
  const live = document.querySelector<HTMLButtonElement>('#bok');
  const finalize = document.querySelector<HTMLButtonElement>('#bfin');
  const reject = document.querySelector<HTMLButtonElement>('#brej');
  if (live) {
    live.disabled = !allTouched;
    live.title = !allTouched
      ? `Save at minimum — every page must be edited before Submit (${r.saved}/${total} touched).`
      : 'Hand off to the pipeline for fixes and re-OCR.';
    live.textContent = !allTouched
      ? `Submit ▶ (${r.saved}/${total} touched)`
      : 'Submit ▶';
  }
  if (finalize) {
    finalize.disabled = !allFinal;
    finalize.title = !allFinal
      ? `Mark every page as approved before Finalize (${r.approved}/${total}).`
      : 'Approved by every page — moves to the build queue.';
    finalize.textContent = !allFinal
      ? `Approve Final (${r.approved}/${total})`
      : '✔ Approve Final';
  }
  if (reject) {
    reject.disabled = false;
    reject.title = 'Send back to the reviewer with a reason (popover).';
  }
  const sb = document.querySelector<HTMLElement>('#statusbar');
  if (sb) {
    sb.textContent =
      `reviewed ${r.saved}/${total} · submitted ${r.submitted}/${total} · ` +
      `approved ${r.approved}/${total}`;
  }
}

// ---- render -------------------------------------------------------------------
function pageStatus(p: Page): string {
  const s = (p.approvals || []).map(a => a.status);
  if (s.includes('approved')) return 'approved';
  if (s.includes('submitted')) return 'submitted';
  if (p.corrected != null) return 'touched';
  return 'untouched';
}

function drawPageStrip(): void {
  if (!D) return;
  $('pagestrip').innerHTML = D.pages.map((p, n) => {
    const s = pageStatus(p);
    return `<span class="pgdot ${n === i ? 'cur' : ''} ${s}"
            data-n="${n}" data-st="${s}"
            title="page ${p.docPage} — ${s}">${p.docPage}</span>`;
  }).join('');
  $('pagestrip').querySelectorAll('.pgdot').forEach(el => {
    el.addEventListener('click', () => {
      if (dirty && !confirm('Discard unsaved edits?')) return;
      i = Number((el as HTMLElement).dataset.n);
      render();
    });
    el.addEventListener('dblclick', e => {
      // dblclick on a page dot toggles that page through the verdict cycle:
      // untouched (or touched — saved but not yet submitted) -> submitted,
      // submitted -> approved, approved -> submitted again (rolled back so
      // a page can be re-finalized). Reading from D.pages[n] not D.pages[i]
      // — the bug that gated every dot against the currently-shown page.
      e.preventDefault();
      const n = Number((el as HTMLElement).dataset.n);
      i = n;                                  // also navigate, like the click
      const ps = pageStatus(D!.pages[n]);
      if (ps === 'submitted') { approvePage(); return; }
      if (ps === 'approved')  { submitPage(); return; }
      submitPage();                           // untouched OR touched
    });
  });
}

function render(): void {
  if (!D || !D.pages.length) return;
  const p = D.pages[i];
  $('pn').textContent = `${i + 1}/${D.pages.length}`;
  $('fn').textContent = D.pdf || '(no pdf)';
  $('bc').textContent = `${p.bad}/${p.words}`;
  // Provenance: after the adjustment worker runs, the machine text under
  // review is the worker's output, and the reviewer must be able to SEE that
  // (adjust:reocr:v2 has fresh suspect marks; adjust:geometry:vN has none —
  // its words were reordered, not re-scored, so read it with your eyes).
  $('srclabel').textContent =
    !p.src || p.src === 'mistral' ? 'Mistral — suspect words marked'
      : `${p.src} — adjusted by the pipeline`;
  ($('img') as HTMLImageElement).src = `/page.img?id=${p.pageId}`;
  const orig = $('orig');
  orig.innerHTML = inlineTables(marks(p.text, p.spans), p);
  markCells(orig, p);
  ($('ed') as HTMLTextAreaElement).value = p.corrected != null ? p.corrected : p.text;
  const dmv = ($('dm') as HTMLSelectElement).value;
  $('df').innerHTML = dmv === 'other'
    ? ((p.others || []).length ? diff(p.text, p.others[0].text)
       : '<i style="color:var(--dim)">no other reviewer on this page</i>')
    : diff(p.text, ($('ed') as HTMLTextAreaElement).value);
  drawTables(p);
  ($('note') as HTMLTextAreaElement).value = p.note || '';
  $('noteh').textContent = `note — page ${i + 1} of ${D.pages.length}: what is wrong?`;
  $('others').innerHTML = (p.others || []).map(o => {
    const changed = o.text && o.text !== p.text;
    return `<div class=ob><h4>${esc(o.by)} — ${esc(o.when)}</h4>` +
      (o.note ? `<div class=on>${esc(o.note)}</div>`
              : '<div class=on style="color:var(--dim)">(no note)</div>') +
      `<div class=oc>${changed ? 'edited the text' : 'no text change'}</div></div>`;
  }).join('') ||
  '<div class=ob style="color:var(--dim2)">no other reviewer on this page</div>';
  const bpage = document.getElementById('bpage');
if (bpage) bpage.textContent = pageStatus(p) === 'approved'
  ? '✓ page approved' : '✓ Approve page';
  drawPageStrip();
  // Render the source content (OCR text in document order with tables
  // interleaved, formatted as block-multiline paragraphs) above the editable
  // textarea. Read pane uses the same ordering, so the editor and the
  // reader see the page identically.
  drawEditableSource(p);
  unmark();
  refreshActionBar();
  $('st').textContent = p.corrected != null
    ? ('saved' + (p.note ? ' · noted' : '')) : '';
}

async function openDoc(n: number): Promise<void> {
  if (dirty && !confirm('Discard unsaved edits?')) return;
  cur = n; i = 0;
  D = await api<Doc>(`/api/doc?id=${Q[n].id}`);
  drawList(); drawTags(); render();
}

function pg(d: number): void {
  if (!D) return;
  if (dirty && !confirm('Discard unsaved edits?')) return;
  i = Math.max(0, Math.min(D.pages.length - 1, i + d)); render();
}

// ---- writes -------------------------------------------------------------------
async function save(): Promise<void> {
  if (!D) return;
  const p = D.pages[i];
  const t = ($('ed') as HTMLTextAreaElement).value;
  const tb = readTables();
  const nt = ($('note') as HTMLTextAreaElement).value;
  const j = await api<{ ok?: boolean; error?: string }>('/api/save',
    { pageId: p.pageId, text: t, tables: tb, note: nt });
  if (j.error) { $('st').textContent = 'SAVE FAILED: ' + j.error; return; }
  p.corrected = t; p.note = nt;
  (p.tables || []).forEach((x, n) => { if (tb[n]) x.saved = tb[n].content; });
  unmark();
  $('st').textContent = 'saved ' + new Date().toLocaleTimeString();
}

// ALWAYS saves first — a verdict must never discard the edits or note that
// justify it. 0.2.2: Submit is gated on every page having a saved edit;
// Approve Final is gated on every page being approved.
async function setVerdict(v: string | null): Promise<void> {
  if (!D) return;
  if (dirty) await save();
  if (dirty) return;
  if (v === 'submitted') {
    const r = reviewedPages();
    if (r.saved !== D.pages.length) {
      $('st').textContent =
        `Save every page first (${r.saved}/${D.pages.length}).`;
      return;
    }
  }
  const j = await api<{ ok?: boolean; error?: string }>('/api/verdict',
    { id: D.id, verdict: v });
  if (j.error) { $('st').textContent = 'FAILED: ' + j.error; return; }
  Q[cur].verdict = v;
  Q[cur].state = effState(Q[cur]);
  drawCounts(); drawFilters(); drawList();
  refreshActionBar();
  const nx = visible().find(([d, n]) => n !== cur && d.state === 'unreviewed');
  if (nx) openDoc(nx[1]);
  else $('st').textContent = 'no unreviewed left in this filter';
  refreshQueue();
}

// Per-page Apply: marks ONE page submitted (the worker runs on it). Saves
// first for the same reason verdicts do.
async function submitPage(): Promise<void> {
  if (!D) return;
  if (dirty) await save();
  if (dirty) return;
  const p = D.pages[i];
  const already = (p.approvals || []).some(a => a.status === 'submitted');
  const j = await api<{ ok?: boolean; error?: string }>('/api/page_verdict',
    { pageId: p.pageId, status: already ? null : 'submitted' });
  if (j.error) { $('st').textContent = 'FAILED: ' + j.error; return; }
  if (already) {
    p.approvals = (p.approvals || []).filter(a =>
      !(a.by === ME && a.status === 'submitted'));
  } else {
    p.approvals = [...(p.approvals || []),
      {by: ME, status: 'submitted', when: new Date().toISOString()}];
  }
  drawPageStrip();
  refreshActionBar();
}
// Per-page Approve: marks one page Final so the document-wide Finalize can fire.
async function approvePage(): Promise<void> {
  if (!D) return;
  if (dirty) await save();
  if (dirty) return;
  const p = D.pages[i];
  const already = (p.approvals || []).some(a => a.status === 'approved');
  const j = await api<{ ok?: boolean; error?: string }>('/api/page_verdict',
    { pageId: p.pageId, status: already ? null : 'approved' });
  if (j.error) { $('st').textContent = 'FAILED: ' + j.error; return; }
  if (already) {
    p.approvals = (p.approvals || []).filter(a =>
      !(a.by === ME && a.status === 'approved'));
  } else {
    p.approvals = [...(p.approvals || []),
      {by: ME, status: 'approved', when: new Date().toISOString()}];
  }
  drawPageStrip();
  refreshActionBar();
}

// ---- tags -----------------------------------------------------------------------
// REVISION marks (v2, v3 …) are NOT user tags: the adjustment worker stamps
// them when a submitted document comes back for re-review. They render as a
// non-removable badge — removing one would falsify the document's history.
const REVISION = /^v\d+$/i;
function drawTags(): void {
  const d = Q[cur];
  if (!d) return;
  const rev = (d.tags || []).filter(t => REVISION.test(t));
  const user = (d.tags || []).filter(t => !REVISION.test(t));
  $('tags').innerHTML =
    rev.map(t => `<span class=tag style="background:rgba(16,185,129,.14);color:#6ee7b7;border-color:rgba(16,185,129,.4);cursor:default" title="revision — set by the pipeline">${esc(t)}</span>`).join('') +
    user.map(t =>
    `<span class=tag data-t="${esc(t)}" title="click to remove">${esc(t)} ×</span>`)
    .join('');
  $('tags').querySelectorAll('.tag[data-t]').forEach(el =>
    el.addEventListener('click', () =>
      pushTags((Q[cur].tags || []).filter(t => t !== (el as HTMLElement).dataset.t))));
}
async function pushTags(tags: string[]): Promise<void> {
  const j = await api<{ ok?: boolean; error?: string }>('/api/tags',
    { id: D!.id, tags });
  if (j.error) { $('st').textContent = 'TAGS FAILED: ' + j.error; return; }
  Q[cur].tags = tags; drawTags(); drawList();
}
function addTag(v: string): void {
  ($('tagsel') as HTMLSelectElement).value = '';
  if (!v) return;
  if (v === '__custom') { v = (prompt('tag:') || '').trim(); if (!v) return; }
  const t = [...(Q[cur].tags || [])];
  if (!t.some(x => x.toLowerCase() === v.toLowerCase())) t.push(v);
  pushTags(t);
}

// ---- reject popover (0.2.2) -----------------------------------------------
// Server endpoint: POST /api/reject with body {id, reason, note, tag}.
// The popover markup is the static SHELL block at id=rejectpop.
function openRejectPopover(): void {
  const pop = $('rejectpop');
  const wasHidden = pop.hasAttribute('hidden');
  pop.toggleAttribute('hidden', !wasHidden);
  if (wasHidden) ($('rejreason') as HTMLSelectElement).focus();
}
function closeRejectPopover(): void {
  $('rejectpop').setAttribute('hidden', '');
  ($('rejreason') as HTMLSelectElement).value = '';
  ($('rejnote') as HTMLTextAreaElement).value = '';
  ($('rejtagsel') as HTMLSelectElement).value = '';
}
async function confirmReject(): Promise<void> {
  if (!D) return;
  let reason = ($('rejreason') as HTMLSelectElement).value;
  if (!reason) {
    $('st').textContent = 'pick a reason first';
    ($('rejreason') as HTMLSelectElement).focus();
    return;
  }
  if (reason === '__other') {
    const custom = (prompt('Rejection reason (plain text):') || '').trim();
    if (!custom) return;                       // cancelled
    reason = custom;
  }
  const note = ($('rejnote') as HTMLTextAreaElement).value.trim();
  let tag = ($('rejtagsel') as HTMLSelectElement).value;
  if (tag === '__custom') {
    const t = (prompt('Tag:') || '').trim();
    if (!t) tag = '';
    else tag = t;
  }
  const j = await api<{ ok?: boolean; error?: string }>('/api/reject',
    { id: D.id, reason, note, tag: tag || null });
  if (j.error) { $('st').textContent = 'REJECT FAILED: ' + j.error; return; }
  // Mirror the server-side verdict onto the queue row so counts and the
  // sidebar list reflect the rejection immediately.
  Q[cur].verdict = 'rejected';
  Q[cur].state = effState(Q[cur]);
  drawCounts(); drawFilters(); drawList(); drawTags();
  refreshActionBar();
  closeRejectPopover();
  refreshQueue();                              // pull peers' state too
  $('st').textContent = 'sent back: ' + reason;
}

// The editable pane shows the OCR text + tables in document order as
// block-multiline paragraphs, mirroring the read pane's ordering. Tables
// arrive inlined as sanitized <table> blocks (same sanitizeTable path
// the read pane uses), surrounded by paragraphs split on blank lines.
function drawEditableSource(p: Page): void {
  const host = $('ed-source');
  if (!host) return;
  const html = inlineTables(p.text, p);
  const blocks = html.split(/\n\s*\n/).map(b => b.trim()).filter(b => b);
  if (!blocks.length) {
    host.innerHTML = '<i style="color:var(--dim2)">empty page</i>';
    return;
  }
  host.innerHTML = blocks
    .map(b => `<p class=ebp>${b.replace(/\n/g, '<br>')}</p>`).join('');
}

// ---- shell ------------------------------------------------------------------------
const SHELL = `
<div id=side>
  <div id=counts>loading…</div>
  <div id=filters></div>
  <input id=search placeholder="search documents…" spellcheck=false>
  <div id=list>loading…</div>
  <div id=notewrap>
    <div class=nh id=noteh>note — what is wrong with this page?</div>
    <textarea id=note spellcheck=true placeholder="e.g. table is a repetition loop, ~13 invented rows · merchant name misread · handwriting unreadable, do not embed a guess"></textarea>
    <div class=noterow>
      <span class=nrl>+ tag</span>
      <select id=tagsel>
        <option value="">+ tag</option>
        <option>needs-reocr</option><option>illegible</option>
        <option>reading-order</option><option>bad-geometry</option>
        <option>repetition</option><option>handwriting</option>
        <option value="__custom">custom…</option>
      </select>
    </div>
    <div id=others></div>
  </div>
</div>
<div id=rejectpop hidden>
  <div>Send back to the reviewer with a reason.</div>
  <select id=rejreason>
    <option value="">— required —</option>
    <option>illigible</option><option>needs-reocr</option>
    <option>repetition</option><option>bad-geometry</option>
    <option>reading-order</option><option>handwriting</option>
    <option value="__other">other (write note)…</option>
  </select>
  <textarea id=rejnote placeholder="what is wrong? (the reviewer will see this)"
    spellcheck=true></textarea>
  <div id=rejtagwrap>
    <span>+ tag</span>
    <select id=rejtagsel>
      <option value="">+ tag</option>
      <option>needs-reocr</option><option>illegible</option>
      <option>reading-order</option><option>bad-geometry</option>
      <option>repetition</option><option>handwriting</option>
      <option value="__custom">custom…</option>
    </select>
  </div>
  <div id=rejactions>
    <button id=rejclose>Cancel</button>
    <button id=rejconfirm>↩ Reject</button>
  </div>
</div>
<div id=main>
  <div id=bar>
    <button class=small id=btoglist title="[ key">☰ list</button>
    <button class=small id=btogdiff title="] key">diff</button>
    <button id=bprev>◀ page</button><b id=pn>—</b><button id=bnext>page ▶</button>
    <span id=statusbar>reviewed 0/0 · submitted 0/0 · approved 0/0</span>
    <button id=bsave title="Save this page's edits and notes — required before Submit.">Save page</button>
    <!-- 0.2.2: removed the per-page "Approve page" button. It muddied the
         flow (Jeff clicked "Approve Final" thinking it was the next step after
         Submit). Approval is now driven by the whole-document Finalize bar. -->
    <button class=primary id=bok disabled
      title="Save at minimum — every page must be edited before Submit.">Submit ▶</button>
    <button class=final id=bfin disabled
      title="Mark every page as approved before Finalize.">✔ Approve Final</button>
    <button class=hold id=brej>↩ Reject</button>
    <span id=tags></span>
    <span id=st></span>
  </div>
  <div id=pagestrip></div>
  <div id=panes>
    <div class=pane><div class=ph><span>scan</span><span id=fn></span></div>
      <div class=pb style="padding:0">
        <div id=imgwrap>
          <div id=zbar>
            <button id=bzout>−</button><span id=zl>fit</span>
            <button id=bzin>+</button><button id=bzfit>fit</button>
          </div>
          <img id=img>
        </div>
      </div></div>
    <div class=pane><div class=ph><span id=srclabel>Mistral — suspect words marked</span>
      <span id=bc></span></div><div class=pb><pre id=orig></pre></div></div>
    <div class=pane><div class=ph><span>your correction (editable)</span>
      <span id=tc></span></div>
      <div class=pb style="display:flex;flex-direction:column;padding:0">
        <div id=prev_note>Note this page before editing: the button row
          under the note saves it back to the server.</div>
        <div id=ed-source title="OCR text in document order with tables interleaved — for reference while you edit"></div>
        <textarea id=ed spellcheck=false></textarea>
        <div id=tbl></div>
      </div>
    </div>
    <div class=pane id=p-diff><div class=ph><span>diff</span>
      <select id=dm>
        <option value=edit>your edits vs Mistral</option>
        <option value=other>other reviewer vs Mistral</option></select></div>
      <div class=pb><pre id=df></pre></div></div>
  </div>
</div>`;

function tog(id: string): void {
  const e = $(id);
  e.classList.toggle('hide');
  try {
    localStorage.setItem('h_' + id, e.classList.contains('hide') ? '1' : '');
  } catch { /* private mode */ }
}

export async function mount(root: HTMLElement, me: string): Promise<void> {
  ME = me;
  root.innerHTML = SHELL;

  wireZoom();
  $('btoglist').addEventListener('click', () => tog('side'));
  $('btogdiff').addEventListener('click', () => tog('p-diff'));
  $('bprev').addEventListener('click', () => pg(-1));
  $('bnext').addEventListener('click', () => pg(1));
  $('bsave').addEventListener('click', save);
  $('bok').addEventListener('click', () => setVerdict('submitted'));
  $('bfin').addEventListener('click', () => setVerdict('approved'));
  $('brej').addEventListener('click', openRejectPopover);
  $('rejclose').addEventListener('click', closeRejectPopover);
  $('rejconfirm').addEventListener('click', confirmReject);
  $('bzout').addEventListener('click', () => zoom(-1));
  $('bzin').addEventListener('click', () => zoom(1));
  $('bzfit').addEventListener('click', zfit);
  $('dm').addEventListener('change', render);
  $('tagsel').addEventListener('change', () =>
    addTag(($('tagsel') as HTMLSelectElement).value));
  $('search').addEventListener('input', e => {
    SEARCH = (e.target as HTMLInputElement).value; drawList();
  });
  $('ed').addEventListener('input', () => {
    mark();
    if (D && ($('dm') as HTMLSelectElement).value === 'edit')
      $('df').innerHTML = diff(D.pages[i].text, ($('ed') as HTMLTextAreaElement).value);
  });
  $('note').addEventListener('input', mark);

  ['side', 'p-diff'].forEach(id => {
    try { if (localStorage.getItem('h_' + id)) $(id).classList.add('hide'); }
    catch { /* private mode */ }
  });

  document.addEventListener('keydown', e => {
    const tag = (e.target as HTMLElement).tagName;
    if (tag === 'TEXTAREA' || tag === 'INPUT' ||
        (e.target as HTMLElement).isContentEditable) {
      if (e.key === 's' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); save(); }
      return;
    }
    if (e.key === 'ArrowRight') pg(1);
    if (e.key === 'ArrowLeft') pg(-1);
    if (e.key === 'a') approvePage();
    if (e.key === '[') tog('side');
    if (e.key === ']') tog('p-diff');
  });

  Q = await api<QueueDoc[]>('/api/queue');
  drawCounts(); drawFilters(); drawList();
  // Paint the action-bar gates before any document is opened so Submit /
  // Finalize start disabled on first render — even on the empty-queue
  // boot path that skips openDoc().
  refreshActionBar();
  const first = Q.findIndex(d => d.flagged && d.state === 'unreviewed');
  if (Q.length) openDoc(first >= 0 ? first : 0);

  // Keep two concurrent reviewers looking at the same reality.
  setInterval(refreshQueue, 45_000);
  addEventListener('focus', refreshQueue);
}
