# Review this change for security vulnerabilities.

- **Session:** `b2bfbb74-ec73-4c49-ab9e-031a5c066560`
- **Span:** 2026-08-15T09:56:18.149Z → 2026-08-15T09:56:27.942Z
- **Messages:** 1 user · 4 assistant
- **Source:** raw JSONL kept locally in `_session-recovery-backup/` (not in git — contains credentials). This file is the scrubbed conversation: all prose, tool calls as one-line markers, tool results omitted.

---

## 👤 User

Review this change for security vulnerabilities.

Changed files (you may Read these and any other file in the repo):
  - review/ocr_review_app.py
  - review/workflow_test.py

Unified diff (only + lines are new):

=== DIFF: review/ocr_review_app.py ===
@@ -254,18 +254,40 @@ def bad_rate(conf):
 
 
 def build_queue(cur, reviewer):
-    """Every flagged document, worst first. One query, scored in Python because
-    the scoring rule lives in one place and must match the numbers above."""
+    """EVERY document, flagged first. One query, scored in Python because the
+    scoring rule lives in one place and must match the numbers above.
+
+    This used to return only the gated documents. The list now carries the
+    whole corpus so the review UI can filter -- by confidence tier, verdict,
+    reviewer, loops, notes -- instead of hiding everything the gate did not
+    trip. The gate still decides FLAGGED and the default sort; it no longer
+    decides visibility."""
     cur.execute("""
         select d.id, d.key, o.name, r.confidence, r.blocks,
-               d.meta->'ocr_review' as review
+               d.meta->'ocr_review' as review,
+               d.meta->'tags' as tags
         from ocr_reading r
         join document d on d.id = (r.meta->>'document_id')::bigint
         left join output_file o on o.document_id = d.id
                         and o.build_version = 'recut-v2'
         where r.method = %s""", (MISTRAL,))
+    rows = cur.fetchall()
+
+    # Which documents carry a reviewer note, and how many pages have been
+    # edited. Human rows carry only page_id (their meta is {source, by, note}),
+    # so the document comes from joining back through the Mistral row.
+    cur.execute("""
+        select (m.meta->>'document_id')::bigint as did,
+               count(*) filter (where coalesce(h.meta->>'note','') <> '') as noted,
+               count(*) as edited
+        from ocr_reading h
+        join ocr_reading m on m.page_id = h.page_id and m.method = %s
+        where h.method like %s
+        group by 1""", (MISTRAL, HUMAN + ":%"))
+    activity = {d: (n, e) for d, n, e in cur.fetchall()}
+
     docs = {}
-    for did, key, name, conf, blocks, review in cur.fetchall():
+    for did, key, name, conf, blocks, review, tags in rows:
         # Every reviewer's verdict travels with the document, so each of you can
         # see what the other decided rather than only your own progress.
         # isinstance guard: two older shapes exist in this table -- a bare
@@ -281,6 +303,9 @@ def build_queue(cur, reviewer):
                                   "tbad": 0, "twords": 0,
                                   "maxRep": 0, "dupRows": 0,
                                   "verdict": mine, "peers": peers,
+                                  "tags": tags if isinstance(tags, list) else [],
+                                  "noted": activity.get(did, (0, 0))[0],
+                                  "edited": activity.get(did, (0, 0))[1],
                                   "done": mine in ("approved", "hold")})
         b, t = score(page_words(conf))
         tb, tt = score(table_words(blocks))
@@ -311,12 +336,27 @@ def build_queue(cur, reviewer):
         d["allRate"] = round(100.0 * allb / allw, 2) if allw else 0.0
         d["thin"] = allw < MIN_WORDS
         d["repeats"] = d["maxRep"] >= MAX_REPEAT
-        if d["rate"] > GATE or d["allRate"] > GATE or d["repeats"]:
-            out.append(d)
-    # Repetition first: a fabricated table is worse than a misread word, and 15
-    # of these are invisible to the confidence gate.
-    out.sort(key=lambda x: (x["thin"], not x["repeats"], -x["maxRep"],
-                            -max(x["rate"], x["allRate"])))
+        d["flagged"] = d["rate"] > GATE or d["allRate"] > GATE or d["repeats"]
+        # Confidence tier, for filtering. "low" is exactly the gate -- one
+        # definition of bad, not two. medium/high split the rest at 0.5% so
+        # "high" genuinely means near-zero suspect words.
+        worst = max(d["rate"], d["allRate"])
+        d["conf"] = "low" if d["flagged"] else ("medium" if worst > 0.5 else "high")
+        # Effective document state across BOTH reviewers, for the pipeline
+        # counts. hold trumps approved -- a document one person approved and
+        # another held is NOT safe to embed. Then: any final approval counts,
+        # then any submission, else unreviewed.
+        verdicts = set(v for v in [d["verdict"], *d["peers"].values()] if v)
+        d["state"] = ("hold" if "hold" in verdicts
+                      else "approved" if "approved" in verdicts
+                      else "submitted" if "submitted" in verdicts
+                      else "unreviewed")
+        out.append(d)
+    # Flagged first (repetition before rate -- a fabricated table is worse than
+    # a misread word, and 15 of these are invisible to the confidence gate),
+    # then the clean tail sorted by rate so "worst of the good" is on top.
+    out.sort(key=lambda x: (not x["flagged"], x["thin"], not x["repeats"],
+                            -x["maxRep"], -max(x["rate"], x["allRate"])))
     return out
 
 
@@ -423,15 +463,17 @@ def verdict(cur, did, value, reviewer):
     """Stamp the review verdict. document.meta only -- document.state is
     load-bearing for the pipeline and is not touched.
 
-    THREE STATES, because two were not enough:
-      approved -- reviewed, text is right, safe to embed
-      hold     -- reviewed and NOT safe to embed. The page is unreadable, or
-                  the table is fabricated, or it needs a second OCR pass.
-                  Without this, "I looked at it" and "it is correct" were the
-                  same click, and a page noted as unreadable would still have
-                  a guess stamped into it.
-      (absent) -- not yet reviewed
-    The embed step must select on approved, never on 'has been opened'.
+    FOUR STATES -- the round trip needs a middle stop:
+      submitted -- my edits are done and handed off. Pending application:
+                   someone (or the pipeline) applies the corrections and
+                   re-uploads the artifact tagged v2/v3. Not yet safe to embed.
+      approved  -- FINAL. Reviewed, correct, marked for the next step
+                   (embed -> PDF/A -> QC -> Papra ingestion).
+      hold      -- reviewed and NOT safe to embed. Unreadable, fabricated
+                   table, needs a second OCR pass.
+      (absent)  -- not yet reviewed
+    The embed step must select on approved, never on 'has been opened' and
+    never on submitted -- submitted text has not been checked back yet.
     """
     cur.execute("""update document set meta = coalesce(meta,'{}'::jsonb) ||
                    jsonb_build_object('ocr_review',
@@ -442,6 +484,22 @@ def verdict(cur, did, value, reviewer):
                    where id = %s""", (reviewer, value, did))
 
 
+def set_tags(cur, did, tags):
+    """Replace the document's tags. Tags are SHARED, not per reviewer -- they
+    describe the document (v2, needs-reocr, illegible), not an opinion about
+    it, so last write wins and both reviewers see the same set. Deduplicated
+    and order-preserved so the UI renders what was sent."""
+    seen, clean = set(), []
+    for t in tags:
+        t = t.strip()
+        if t and t.lower() not in seen:
+            seen.add(t.lower())
+            clean.append(t)
+    cur.execute("""update document set meta = coalesce(meta,'{}'::jsonb) ||
+                   jsonb_build_object('tags', %s::jsonb)
+                   where id = %s""", (json.dumps(clean), did))
+
+
 def page_png(pdf_name, doc_page):
     """Local fallback: rasterise straight from the source PDF. Used when R2 is
     not configured, i.e. running on the machine that holds recut/."""
@@ -657,9 +715,16 @@ class Handler(SimpleHTTPRequestHandler):
                               p.get("tables") or [], p.get("note", ""), who)
                 elif path == "/verdict":
                     v = p.get("verdict")
-                    if v not in ("approved", "hold", None):
+                    if v not in ("submitted", "approved", "hold", None):
                         return self._send(400, '{"error":"bad verdict"}')
                     verdict(cur, int(p["id"]), v, who)
+                elif path == "/tags":
+                    tags = p.get("tags")
+                    if (not isinstance(tags, list)
+                            or any(not isinstance(t, str) or len(t) > 40
+                                   for t in tags)):
+                        return self._send(400, '{"error":"bad tags"}')
+                    set_tags(cur, int(p["id"]), tags)
                 else:
                     return self._send(404, '{"error":"not found"}')
                 c.commit()
@@ -671,85 +736,158 @@ class Handler(SimpleHTTPRequestHandler):
 
 HTML = r"""<!doctype html><meta charset="utf-8"><title>OCR review</title>
 <style>
-:root{--bg:#0f1115;--panel:#171a21;--line:#2a2f3a;--fg:#e6e9ef;--dim:#8b93a7;
---bad:#ff6b6b;--ok:#4ade80;--add:#14532d;--del:#5b1d1d}
-*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
-font:13px/1.5 ui-sans-serif,system-ui,sans-serif;height:100vh;display:flex}
-#side{width:270px;flex:0 0 270px;border-right:1px solid var(--line);overflow:auto}
-#side h1{font-size:13px;margin:0;padding:10px 12px;border-bottom:1px solid var(--line);
-position:sticky;top:0;background:var(--panel)}
+/* Linear design tokens -- Design/DESIGN.md. Dark is the native medium: near-
+   black canvas, structure from semi-transparent white borders, one accent. */
+:root{
+  --bg:#08090a;--panel:#0f1011;--lvl3:#191a1b;--hover:#28282c;
+  --fg:#f7f8f8;--fg2:#d0d6e0;--dim:#8a8f98;--dim2:#62666d;
+  --line:rgba(255,255,255,.05);--line2:rgba(255,255,255,.08);
+  --brand:#5e6ad2;--accent:#7170ff;--accent-h:#828fff;
+  --ok:#27a644;--ok2:#10b981;--warn:#fbbf24;--bad:#ff6b6b;
+  --add:#14532d;--del:#5b1d1d}
+*{box-sizing:border-box}
+body{margin:0;background:var(--bg);color:var(--fg);height:100vh;display:flex;
+font:13px/1.5 "Inter Variable",Inter,"SF Pro Display",-apple-system,system-ui,
+"Segoe UI",Roboto,sans-serif;font-feature-settings:"cv01","ss03";
+font-weight:400}
+#side{width:300px;flex:0 0 300px;border-right:1px solid var(--line2);
+display:flex;flex-direction:column;min-height:0;background:var(--panel)}
+#counts{padding:10px 12px;border-bottom:1px solid var(--line);font-size:12px;
+font-weight:510;display:flex;gap:10px;flex-wrap:wrap}
+#counts b{font-weight:590}
+#counts .cr{color:var(--bad)}#counts .cs{color:var(--accent-h)}
+#counts .cf{color:var(--ok2)}#counts .ch{color:var(--warn)}
+#filters{padding:7px 10px;border-bottom:1px solid var(--line);display:flex;
+gap:4px;flex-wrap:wrap}
+.fc{font-size:11px;font-weight:510;padding:2px 8px;border-radius:9999px;
+border:1px solid var(--line2);background:rgba(255,255,255,.02);
+color:var(--dim);cursor:pointer;user-select:none}
+.fc:hover{background:var(--hover);color:var(--fg2)}
+.fc.on{background:var(--brand);border-color:var(--brand);color:#fff}
+#search{margin:7px 10px;padding:5px 9px;background:rgba(255,255,255,.02);
+border:1px solid var(--line2);border-radius:6px;color:var(--fg);
+font:12px/1.4 inherit;outline:0}
+#search:focus{border-color:var(--accent)}
+#search::placeholder{color:var(--dim2)}
+#list{flex:1;overflow:auto;min-height:0}
 .q{padding:7px 12px;border-bottom:1px solid var(--line);cursor:pointer}
-.q:hover{background:var(--panel)}.q.on{background:#1e2530;border-left:3px solid #6aa7ff}
-.q .k{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
-.q .m{color:var(--dim);font-size:11px}.q.done .k{color:var(--ok)}
-#main{flex:1;display:flex;flex-direction:column;min-width:0}
-#bar{padding:8px 12px;border-bottom:1px solid var(--line);display:flex;gap:10px;
-align-items:center;background:var(--panel)}
-button{background:#232935;color:var(--fg);border:1px solid var(--line);
-border-radius:5px;padding:5px 11px;cursor:pointer}button:hover{background:#2c3444}
-button.p{background:#1d4ed8;border-color:#1d4ed8}
-button.h{background:#78350f;border-color:#92400e}
-.q.held .k{color:#fbbf24}
-.pk{color:#7dd3fc;font-size:10px}
-#others{flex:0 0 auto;max-height:26vh;overflow:auto;border-top:1px solid var(--line);
-background:#101720}
+.q:hover{background:var(--hover)}
+.q.on{background:var(--lvl3);border-left:3px solid var(--accent)}
+.q .k{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
+font-weight:510;color:var(--fg2)}
+.q .m{color:var(--dim);font-size:11px}
+.q.done .k{color:var(--ok2)}
+.q.held .k{color:var(--warn)}
+.q.subm .k{color:var(--accent-h)}
+.pk{color:var(--accent-h);font-size:10px}
+.tag{display:inline-block;font-size:10px;font-weight:510;padding:0 6px;
+border-radius:9999px;background:rgba(94,106,210,.18);color:var(--accent-h);
+border:1px solid rgba(113,112,255,.3);margin-left:4px}
+#notewrap{flex:0 0 auto;border-top:1px solid var(--line2);background:var(--panel);
+display:flex;flex-direction:column;max-height:44vh}
+.nh{padding:5px 10px;font-size:11px;font-weight:510;color:var(--dim);
+background:var(--lvl3);border-bottom:1px solid var(--line)}
+#note{height:84px;min-height:84px;padding:8px 10px;flex:0 0 auto;
+font:12px/1.5 inherit;background:var(--panel);color:var(--fg);border:0;
+outline:0;resize:vertical}
+#note::placeholder{color:var(--dim2)}
+#others{flex:0 1 auto;overflow:auto;border-top:1px solid var(--line)}
 .ob{padding:6px 10px;border-bottom:1px solid var(--line)}
-.ob h4{margin:0 0 3px;font-size:11px;color:#7dd3fc;font-weight:600}
-.ob .on{white-space:pre-wrap;font-size:12px}
+.ob h4{margin:0 0 3px;font-size:11px;color:var(--accent-h);font-weight:590}
+.ob .on{white-space:pre-wrap;font-size:12px;color:var(--fg2)}
 .ob .oc{color:var(--dim);font-size:11px;margin-top:3px}
+#main{flex:1;display:flex;flex-direction:column;min-width:0}
+#bar{padding:8px 12px;border-bottom:1px solid var(--line2);display:flex;gap:8px;
+align-items:center;background:var(--panel);flex-wrap:wrap}
+button{background:rgba(255,255,255,.02);color:#e2e4e7;font-weight:510;
+border:1px solid rgb(36,40,44);border-radius:6px;padding:5px 11px;cursor:pointer;
+font-family:inherit;font-feature-settings:inherit}
+button:hover{background:var(--hover)}
+button.p{background:var(--brand);border-color:var(--brand);color:#fff}
+button.p:hover{background:var(--accent)}
+button.f{background:rgba(16,185,129,.14);border-color:rgba(16,185,129,.4);
+color:#6ee7b7}
+button.f:hover{background:rgba(16,185,129,.25)}
+button.h{background:rgba(251,191,36,.1);border-color:rgba(251,191,36,.35);
+color:var(--warn)}
+button.h:hover{background:rgba(251,191,36,.2)}
+#tags{display:flex;gap:4px;align-items:center;flex-wrap:wrap}
+#tags .tag{cursor:pointer;margin-left:0}
+#tags .tag:hover{background:rgba(255,107,107,.18);color:#ffd7d7;
+border-color:rgba(255,107,107,.4)}
+#tagsel{background:rgba(255,255,255,.02);color:var(--dim);font:11px/1.4 inherit;
+border:1px solid var(--line2);border-radius:6px;padding:3px 6px;outline:0}
 #panes{flex:1;display:flex;min-height:0}
-.pane{flex:1;display:flex;flex-direction:column;border-right:1px solid var(--line);
+.pane{flex:1;display:flex;flex-direction:column;border-right:1px solid var(--line2);
 min-width:0}.pane:last-child{border-right:0}
-.ph{padding:5px 10px;font-size:11px;color:var(--dim);background:var(--panel);
-border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
+.ph{padding:5px 10px;font-size:11px;font-weight:510;color:var(--dim);
+background:var(--panel);border-bottom:1px solid var(--line);display:flex;
+justify-content:space-between}
 .pb{flex:1;overflow:auto;padding:10px}
-#imgwrap{position:relative;overflow:hidden;height:100%;background:#0b0d11;
+#imgwrap{position:relative;overflow:hidden;height:100%;background:#010102;
 cursor:grab}
 #imgwrap.drag{cursor:grabbing}
 #img{display:block;background:#fff;transform-origin:0 0;
 image-rendering:-webkit-optimize-contrast}
 #zbar{position:absolute;right:8px;top:8px;z-index:5;display:flex;gap:4px;
-background:rgba(15,17,21,.85);border:1px solid var(--line);border-radius:6px;
+background:rgba(15,16,17,.85);border:1px solid var(--line2);border-radius:6px;
 padding:3px}
 #zbar button{padding:2px 8px;font-size:12px;line-height:1.4}
 #zl{align-self:center;color:var(--dim);font-size:11px;padding:0 4px;min-width:38px;
 text-align:center}
-pre,textarea{margin:0;font:12px/1.55 ui-monospace,Menlo,Consolas,monospace;
-white-space:pre-wrap;word-break:break-word}
-textarea{width:100%;height:auto;min-height:20vh;background:transparent;
+pre,textarea.mono,#ed{margin:0;font:12px/1.55 "Berkeley Mono",ui-monospace,
+"SF Mono",Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
+#ed{width:100%;height:auto;min-height:24vh;background:transparent;
 color:var(--fg);border:0;outline:0;resize:vertical}
-.tw{margin:8px 0;border:1px solid var(--line);border-radius:4px;
-padding:6px;background:#141821}
+.tw{margin:8px 0;border:1px solid var(--line2);border-radius:6px;
+padding:6px;background:var(--lvl3)}
 .tl{font-size:10px;color:var(--dim);margin-bottom:4px}
 .th{margin:14px 0 4px;font-size:11px;color:var(--dim);border-top:1px solid var(--line);
 padding-top:8px;display:flex;justify-content:space-between}
-table{border-collapse:collapse;width:100%;font:11px/1.4 ui-monospace,Consolas,monospace}
-td,th{border:1px solid var(--line);padding:3px 5px;vertical-align:top}
-td:focus{outline:2px solid #6aa7ff;background:#1b2130}
+table{border-collapse:collapse;width:100%;
+font:11px/1.4 "Berkeley Mono",ui-monospace,Consolas,monospace}
+td,th{border:1px solid var(--line2);padding:3px 5px;vertical-align:top}
+td:focus{outline:2px solid var(--accent);background:var(--lvl3)}
 mark{background:rgba(255,107,107,.28);color:#ffd7d7;border-bottom:1px solid var(--bad);
 border-radius:2px}
 ins{background:var(--add);color:#bbf7d0;text-decoration:none}
 del{background:var(--del);color:#fecaca}
 #st{margin-left:auto;color:var(--dim)}
 .hide{display:none !important}
-.nh{padding:5px 10px;font-size:11px;color:var(--dim);background:var(--panel);
-border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
-#note{height:88px;min-height:88px;padding:8px 10px;flex:0 0 auto;
-font:12px/1.5 ui-sans-serif,system-ui,sans-serif;background:#14181f}
-#note::placeholder{color:#5b6478}
 button.t{padding:5px 9px;font-size:11px}
 </style>
-<div id=side><h1>Flagged documents</h1><div id=list>loading…</div></div>
+<div id=side>
+  <div id=counts>loading…</div>
+  <div id=filters></div>
+  <input id=search placeholder="search documents…" spellcheck=false>
+  <div id=list>loading…</div>
+  <div id=notewrap>
+    <div class=nh id=noteh>note — what is wrong with this page?</div>
+    <textarea id=note spellcheck=true placeholder="e.g. table is a repetition loop, ~13 invented rows · merchant name misread · handwriting unreadable, do not embed a guess"></textarea>
+    <div id=others></div>
+  </div>
+</div>
 <div id=main>
   <div id=bar>
     <button class=t onclick="tog('side')" title="[ key">☰ list</button>
     <button class=t onclick="tog('p-diff')" title="] key">diff</button>
     <button onclick="pg(-1)">◀ page</button><b id=pn>—</b><button onclick="pg(1)">page ▶</button>
     <button onclick="save()">Save page</button>
-    <button class=p onclick="setVerdict('approved')" id=bok
-      title="Saves this page first, then marks the document safe to embed">Approve ▶</button>
+    <button class=p onclick="setVerdict('submitted')" id=bok
+      title="Saves this page first, then marks the document SUBMITTED — edits done, pending application and re-upload (v2/v3)">Submit ▶</button>
+    <button class=f onclick="setVerdict('approved')" id=bfin
+      title="Saves first, then marks the document FINAL — correct, and marked for the next step: embed, PDF/A, QC, Papra">✔ Approve Final</button>
     <button class=h onclick="setVerdict('hold')"
       title="Saves this page first, then marks the document DO NOT EMBED">⏸ Hold</button>
+    <span id=tags></span>
+    <select id=tagsel onchange="addTag(this.value)">
+      <option value="">+ tag</option>
+      <option>v2</option><option>v3</option>
+      <option>needs-reocr</option><option>illegible</option>
+      <option>reading-order</option><option>repetition</option>
+      <option>handwriting</option>
+      <option value="__custom">custom…</option>
+    </select>
     <span id=st></span>
   </div>
   <div id=panes>
@@ -769,9 +907,6 @@ button.t{padding:5px 9px;font-size:11px}
       <span id=tc></span></div>
       <div class=pb><textarea id=ed spellcheck=false></textarea>
         <div id=tbl></div></div>
-      <div class=nh>note — what is wrong with this page?</div>
-      <textarea id=note spellcheck=true placeholder="e.g. table is a repetition loop, ~13 invented rows · merchant name misread · handwriting unreadable, do not embed a guess"></textarea>
-      <div id=others></div>
     </div>
     <div class=pane id=p-diff><div class=ph><span>diff</span>
       <select id=dm onchange=render()>
@@ -783,16 +918,70 @@ button.t{padding:5px 9px;font-size:11px}
 <script>
 let Q=[],D=null,i=0,dirty=false;
 const $=x=>document.getElementById(x);
-// The Approve button relabels itself when there are pending edits, so it is
-// visible on the button -- not just inferable from the code -- that approving
+// The Submit button relabels itself when there are pending edits, so it is
+// visible on the button -- not just inferable from the code -- that submitting
 // saves your work rather than discarding it.
 function mark(){dirty=true;$('st').textContent='unsaved';
-  $('bok').textContent='Save + Approve ▶';}
-function unmark(){dirty=false;$('bok').textContent='Approve ▶';}
+  $('bok').textContent='Save + Submit ▶';}
+function unmark(){dirty=false;$('bok').textContent='Submit ▶';}
 const esc=s=>s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
 
-async function boot(){Q=await(await fetch('/queue')).json();drawList();
-  if(Q.length)openDoc(0);}
+// ---- filters + counts ------------------------------------------------------
+// The list carries the WHOLE corpus; the chips decide what is visible. The
+// filter is a predicate over the document row, so adding one is one line.
+const FILTERS={
+  all:      [ 'All',        d=>true ],
+  flagged:  [ 'Flagged',    d=>d.flagged ],
+  loops:    [ '⟳ Loops', d=>d.repeats ],
+  low:      [ 'Low',        d=>d.conf==='low' ],
+  medium:   [ 'Med',        d=>d.conf==='medium' ],
+  high:     [ 'High',       d=>d.conf==='high' ],
+  unrev:    [ 'Unreviewed', d=>d.state==='unreviewed' ],
+  submitted:[ 'Submitted',  d=>d.state==='submitted' ],
+  final:    [ 'Final ✓', d=>d.state==='approved' ],
+  held:     [ 'Held',       d=>d.state==='hold' ],
+  mine:     [ 'Mine',       d=>!!d.verdict ],
+  noted:    [ '✎ Noted', d=>d.noted>0 ],
+};
+let FILTER='flagged', SEARCH='';
+function visible(){const f=FILTERS[FILTER][1], s=SEARCH.toLowerCase();
+  return Q.map((d,n)=>[d,n]).filter(([d])=>f(d)&&
+    (!s||(d.key||'').toLowerCase().includes(s)||
+     (d.tags||[]).some(t=>t.toLowerCase().includes(s))));}
+function drawFilters(){$('filters').innerHTML=Object.entries(FILTERS).map(
+  ([k,[label]])=>{const n=Q.filter(FILTERS[k][1]).length;
+    return `<span class="fc ${k===FILTER?'on':''}" onclick="setFilter('${k}')"
+      title="${n} document(s)">${label}</span>`;}).join('');}
+function setFilter(k){FILTER=k;drawFilters();drawList();}
+$('search').addEventListener('input',e=>{SEARCH=e.target.value;drawList();});
+
+// The pipeline readout. "to review" is the gate's queue; submitted is waiting
+// on the apply+reupload round trip; final is marked for embed -> PDF/A -> QC
+// -> Papra. These are EFFECTIVE states across both reviewers (hold trumps).
+function drawCounts(){
+  const c={r:0,s:0,f:0,h:0};
+  for(const d of Q){
+    if(d.state==='approved')c.f++;
+    else if(d.state==='hold')c.h++;
+    else if(d.state==='submitted')c.s++;
+    else if(d.flagged)c.r++;}
+  $('counts').innerHTML=
+    `<span class=cr><b>${c.r}</b> to review</span>`+
+    `<span class=cs><b>${c.s}</b> submitted</span>`+
+    `<span class=cf><b>${c.f}</b> final</span>`+
+    `<span class=ch><b>${c.h}</b> held</span>`;}
+
+// Recompute a document's effective state after a verdict changes, using the
+// same rule the server applies: hold trumps approved trumps submitted.
+function effState(d){
+  const vs=new Set([d.verdict,...Object.values(d.peers||{})].filter(Boolean));
+  return vs.has('hold')?'hold':vs.has('approved')?'approved'
+        :vs.has('submitted')?'submitted':'unreviewed';}
+
+async function boot(){Q=await(await fetch('/queue')).json();
+  drawCounts();drawFilters();drawList();
+  const first=Q.findIndex(d=>d.flagged&&d.state==='unreviewed');
+  if(Q.length)openDoc(first>=0?first:0);}
 // ---- scan zoom / pan -------------------------------------------------------
 // The first version of this tool served the PDF itself and let the browser's
 // PDF viewer handle zoom. This one renders a page image server-side, which is
@@ -827,26 +1016,32 @@ $('imgwrap').addEventListener('dblclick',zfit);
 $('img').addEventListener('load',()=>{if(!Z)zfit();else apply();});
 addEventListener('resize',()=>{if(!Z)zfit();});
 
-function drawList(){$('list').innerHTML=Q.map((d,n)=>
-  `<div class="q ${n==cur?'on':''} ${d.verdict==='approved'?'done':''} ${
-     d.verdict==='hold'?'held':''}" onclick="openDoc(${n})">
-   <span class=k>${d.verdict==='approved'?'✓ ':d.verdict==='hold'?'⏸ ':''}${
+const VICON={approved:'✓ ',hold:'⏸ ',submitted:'↑ '};
+function drawList(){const rows=visible();
+  $('list').innerHTML=rows.map(([d,n])=>
+  `<div class="q ${n==cur?'on':''} ${d.state==='approved'?'done':''} ${
+     d.state==='hold'?'held':''} ${d.state==='submitted'?'subm':''}"
+     onclick="openDoc(${n})">
+   <span class=k>${VICON[d.verdict]||''}${
      d.repeats?'⟳ ':''}${d.thin?'· ':''}${esc(d.key)}${
      Object.keys(d.peers||{}).length?` <span class=pk>${
        Object.entries(d.peers).map(([w,v])=>
-         `${esc(w)}:${v==='approved'?'✓':'⏸'}`).join(' ')}</span>`:''}</span>
-   <span class=m>${d.repeats?`<b style=color:#fbbf24>${d.maxRep} identical rows in a row</b> · `:''}${d.rate}% text${
+         `${esc(w)}:${VICON[v]?VICON[v].trim():'?'}`).join(' ')}</span>`:''}${
+     (d.tags||[]).map(t=>`<span class=tag>${esc(t)}</span>`).join('')}</span>
+   <span class=m>${d.repeats?`<b style=color:var(--warn)>${d.maxRep} identical rows in a row</b> · `:''}${d.rate}% text${
      d.twords?` · ${d.allRate}% w/tables`:''} · ${
      d.bad+d.tbad}/${d.words+d.twords} words · ${d.pages}p${
+     d.noted?' · ✎':''}${
      d.thin?' · thin, rate unreliable':''}</span></div>`
-  ).join('');}
+  ).join('')||'<div class=q style=color:var(--dim2)>nothing matches this filter</div>';}
 let cur=0;
 // NOT named open(): inline onclick handlers resolve identifiers against the
 // document object before window, so open() reached document.open(), which
 // blanks the page and starts a new stream -- the white screen. Calls from
 // normal scope (boot) hit the intended function, which is why only clicking broke.
 async function openDoc(n){if(dirty&&!confirm('Discard unsaved edits?'))return;
-  cur=n;i=0;D=await(await fetch('/doc?id='+Q[n].id)).json();drawList();render();}
+  cur=n;i=0;D=await(await fetch('/doc?id='+Q[n].id)).json();
+  drawList();drawTags();render();}
 
 function marks(t,sp){if(!sp.length)return esc(t);
   sp=sp.slice().sort((a,b)=>a.s-b.s);let o='',p=0;
@@ -943,6 +1138,7 @@ function render(){if(!D||!D.pages.length)return;
     : diff(p.text,$('ed').value);
   drawTables(p);
   $('note').value=p.note||'';
+  $('noteh').textContent=`note — page ${i+1} of ${D.pages.length}: what is wrong?`;
   // What the other reviewer did on THIS page -- their note, and whether their
   // text differs from Mistral's. Read-only: their row is theirs, and saving
   // never touches it.
@@ -986,7 +1182,7 @@ async function save(){const p=D.pages[i],t=$('ed').value,tb=readTables(p),
   $('st').textContent='saved '+new Date().toLocaleTimeString();}
 
 // ALWAYS saves first. A verdict must never discard the edits or the note that
-// justify it -- that ambiguity is the whole reason there are three states.
+// justify it -- that ambiguity is the whole reason there are four states.
 async function setVerdict(v){
   if(dirty)await save();
   if(dirty)return;                    // save failed; the error is on screen
@@ -994,9 +1190,32 @@ async function setVerdict(v){
     body:JSON.stringify({id:D.id,verdict:v})});
   const j=await r.json();
   if(j.error){$('st').textContent='FAILED: '+j.error;return;}
-  Q[cur].verdict=v;drawList();
-  const nx=Q.findIndex(d=>!d.verdict);
-  if(nx>=0)openDoc(nx);else $('st').textContent='queue complete';}
+  Q[cur].verdict=v;Q[cur].state=effState(Q[cur]);
+  drawCounts();drawFilters();drawList();
+  // Next unhandled document WITHIN the current filter, so working a filtered
+  // slice (say, loops only) walks that slice rather than jumping out of it.
+  const nx=visible().find(([d,n])=>n!==cur&&d.state==='unreviewed');
+  if(nx)openDoc(nx[1]);else $('st').textContent='no unreviewed left in this filter';}
+
+// ---- tags ------------------------------------------------------------------
+// Shared, document-level. Click a chip to remove it; the select adds one.
+// v2/v3 mark the re-upload round; the rest describe what is wrong.
+function drawTags(){const d=Q[cur];if(!d)return;
+  $('tags').innerHTML=(d.tags||[]).map(t=>
+    `<span class=tag onclick="rmTag('${esc(t).replace(/'/g,"\\'")}')"
+      title="click to remove">${esc(t)} ×</span>`).join('');}
+async function pushTags(tags){
+  const r=await fetch('/tags',{method:'POST',
+    body:JSON.stringify({id:D.id,tags})});
+  const j=await r.json();
+  if(j.error){$('st').textContent='TAGS FAILED: '+j.error;return false;}
+  Q[cur].tags=tags;drawTags();drawList();return true;}
+function addTag(v){$('tagsel').value='';if(!v)return;
+  if(v==='__custom'){v=(prompt('tag:')||'').trim();if(!v)return;}
+  const t=[...(Q[cur].tags||[])];
+  if(!t.some(x=>x.toLowerCase()===v.toLowerCase()))t.push(v);
+  pushTags(t);}
+function rmTag(v){pushTags((Q[cur].tags||[]).filter(t=>t!==v));}
 
 $('note').addEventListener('input',mark);
 
@@ -1012,8 +1231,9 @@ if __name__ == "__main__":
     with db() as c, c.cursor() as cur:
         q = build_queue(cur, SOLO or (sorted(USERS) or ["alden"])[0])
     print("OCR review  ->  http://%s:%d" % (HOST, PORT))
-    print("%d documents over %.0f%% suspect words (threshold %.2f)"
-          % (len(q), GATE, BAD_WORD))
+    flagged = sum(1 for d in q if d.get("flagged"))
+    print("%d documents (%d flagged over %.0f%% suspect words, threshold %.2f)"
+          % (len(q), flagged, GATE, BAD_WORD))
     print("corrections write to Neon as method='%s:<reviewer>'; Mistral untouched"
           % HUMAN)
     print("page images : %s" % ("R2 %s/%s (signed, %ds)"


=== DIFF: review/workflow_test.py ===
@@ -0,0 +1,116 @@
+# /// script
+# requires-python = ">=3.10"
+# dependencies = ["psycopg[binary]>=3.1", "pymupdf>=1.24", "boto3>=1.34"]
+# ///
+"""The submit/final/tags round trip, against live Neon, leaving no trace.
+
+Uses the LAST document in the queue (least likely to be actively reviewed),
+walks it unreviewed -> submitted -> approved -> hold -> cleared, sets and
+clears tags, and asserts the queue reflects each step. Every write is undone.
+"""
+import http.client, importlib, json, os, socketserver, sys, threading, time
+
+APP = os.path.dirname(os.path.abspath(__file__))
+sys.path.insert(0, APP)
+os.chdir(APP)
+os.environ["REVIEW_USERS"] = "alden:wf-pw"
+os.environ["SESSION_SECRET"] = "wf-secret"
+os.environ["HOST"] = "127.0.0.1"
+
+import ocr_review_app as app
+importlib.reload(app)
+
+ok = fail = 0
+def check(label, cond, detail=""):
+    global ok, fail
+    if cond: ok += 1; print("  PASS  %s" % label)
+    else:    fail += 1; print("  FAIL  %s   %s" % (label, detail))
+
+class S(socketserver.ThreadingTCPServer):
+    allow_reuse_address = True
+    daemon_threads = True
+srv = S(("127.0.0.1", 8896), app.Handler)
+threading.Thread(target=srv.serve_forever, daemon=True).start()
+time.sleep(0.5)
+
+def req(method, path, body=None, cookie=None):
+    c = http.client.HTTPConnection("127.0.0.1", 8896, timeout=60)
+    h = {"Cookie": cookie} if cookie else {}
+    if body is not None:
+        h["Content-Type"] = "application/x-www-form-urlencoded"
+    c.request(method, path, body, h)
+    r = c.getresponse()
+    return r.status, dict(r.getheaders()), r.read()
+
+s, h, _ = req("POST", "/login", "user=alden&pw=wf-pw")
+COOKIE = h.get("Set-Cookie", "").split(";")[0]
+check("login", s == 302, s)
+
+def queue():
+    s, _, b = req("GET", "/queue", cookie=COOKIE)
+    assert s == 200, b[:200]
+    return json.loads(b)
+
+q = queue()
+check("queue returns the WHOLE corpus (1464)", len(q) == 1464, len(q))
+check("rows carry conf tier", all(d.get("conf") in ("low","medium","high") for d in q))
+check("rows carry effective state", all("state" in d for d in q))
+check("rows carry tags list", all(isinstance(d.get("tags"), list) for d in q))
+flagged = [d for d in q if d["flagged"]]
+check("flagged subset = 256 (the old gate)", len(flagged) == 256, len(flagged))
+check("flagged sort first", all(d["flagged"] for d in q[:len(flagged)]))
+tiers = {t: sum(1 for d in q if d["conf"] == t) for t in ("low","medium","high")}
+print("       tiers: %s" % tiers)
+
+victim = q[-1]
+did = victim["id"]
+orig_verdict = victim["verdict"]
+orig_tags = victim["tags"]
+print("       victim doc %s (%s) verdict=%r tags=%r"
+      % (did, victim["key"][:40], orig_verdict, orig_tags))
+
+def set_verdict(v):
+    s, _, b = req("POST", "/verdict",
+                  json.dumps({"id": did, "verdict": v}), cookie=COOKIE)
+    return s == 200, b[:120]
+
+def state_of():
+    return next(d for d in queue() if d["id"] == did)
+
+okv, msg = set_verdict("submitted")
+check("verdict submitted accepted", okv, msg)
+check("state becomes submitted", state_of()["state"] == "submitted")
+okv, _ = set_verdict("approved")
+check("state becomes approved (final)", okv and state_of()["state"] == "approved")
+okv, _ = set_verdict("hold")
+check("hold trumps: state becomes hold", okv and state_of()["state"] == "hold")
+okv, _ = set_verdict(None)
+check("cleared back to unreviewed", okv and state_of()["state"] == "unreviewed")
+s, _, b = req("POST", "/verdict",
+              json.dumps({"id": did, "verdict": "banana"}), cookie=COOKIE)
+check("bad verdict rejected", s == 400, s)
+
+s, _, b = req("POST", "/tags",
+              json.dumps({"id": did, "tags": ["v2", "needs-reocr", "V2"]}),
+              cookie=COOKIE)
+check("tags set", s == 200, b[:120])
+t = state_of()["tags"]
+check("tags dedupe case-insensitively", t == ["v2", "needs-reocr"], t)
+s, _, b = req("POST", "/tags", json.dumps({"id": did, "tags": ["x" * 50]}),
+              cookie=COOKIE)
+check("overlong tag rejected", s == 400, s)
+s, _, b = req("POST", "/tags", json.dumps({"id": did, "tags": orig_tags}),
+              cookie=COOKIE)
+check("tags restored", s == 200 and state_of()["tags"] == orig_tags)
+
+if orig_verdict:                                  # restore if it had one
+    set_verdict(orig_verdict)
+final = state_of()
+check("victim fully restored", final["verdict"] == orig_verdict
+      and final["tags"] == orig_tags,
+      "verdict=%r tags=%r" % (final["verdict"], final["tags"]))
+
+srv.shutdown()
+print()
+print("PASS %d   FAIL %d" % (ok, fail))
+sys.exit(1 if fail else 0)


Investigate per the method in your instructions, then return the findings list.

---

## 🤖 Claude

> 🔧 `Read` — /home/user/review/ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Glob` — **/ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\ocr_review_app.py

---

## 🤖 Claude

> 🔧 `Read` — C:\Users\busin\Documents\Paper-Oxidizer\.claude\worktrees\genius-extract-and-app\review\ocr_review_app.py
