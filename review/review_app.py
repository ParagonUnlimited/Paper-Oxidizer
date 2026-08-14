"""Local review app for the split documents.

    python review_app.py            # then open http://127.0.0.1:8777

Why a local server and not a claude.ai artifact: a published artifact runs on
remote hosting behind a strict content-security policy and physically cannot
open a file under C:\\. The whole point of this screen is to show you the actual
PDF next to the question about it, so it has to run here.

Why a server and not just an .html file opened off disk: over http:// the PDF
viewer, the fetch calls and the save-back all behave the same in every browser.
Off a file:// page they are subject to per-browser restrictions I cannot verify
from here, and a review tool that silently fails to show the document is worse
than no tool.

Decisions are written straight into splits/_review-decisions.csv the moment you
pick them -- the same file you would edit in Excel, so the two are interchangeable
and nothing lives only in the browser. That is the lesson from the stage-3 agent
losses: assume the process dies at any moment; a partial file is a success.
"""
import csv, io, json, os, re, socketserver, sys, tempfile, threading, webbrowser
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
SPLITS = os.path.join(BASE, "splits")
CSV_PATH = os.path.join(SPLITS, "_review-decisions.csv")
APP_HTML = os.path.join(BASE, "review_app.html")
PORT = 8777

# The sheet keeps =HYPERLINK("C:\\...","open") in OPEN and COMPARE so Excel can
# click through. The browser wants a plain relative URL, so unwrap them.
HYPER_RE = re.compile(r'^=HYPERLINK\("(.+?)","[^"]*"\)$')


def unwrap(cell):
    m = HYPER_RE.match((cell or "").strip())
    if not m:
        return ""
    p = m.group(1)
    return os.path.relpath(p, SPLITS).replace(os.sep, "/") if os.path.isabs(p) else p


def read_rows():
    with io.open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    out = []
    for i, r in enumerate(rows):
        out.append({
            "i": i,
            "file": r["FILE"],
            "open": unwrap(r.get("OPEN")),
            "compare": unwrap(r.get("COMPARE")),
            "comparePage": r.get("COMPARE PAGE", ""),
            "decision": (r.get("DECISION") or "").strip(),
            "correction": (r.get("CORRECTION") or "").strip(),
            "review": r.get("REVIEW", ""),
            "why": r.get("WHY", ""),
            "issuer": r.get("ISSUER", ""),
            "account": r.get("ACCOUNT", ""),
            "date": r.get("DATE", ""),
            "kind": r.get("KIND", ""),
            "pages": r.get("PAGES", ""),
            "sourcePages": r.get("SOURCE PAGES", ""),
            "orderEvidence": r.get("ORDER EVIDENCE", ""),
            "bucket": r.get("BUCKET", ""),
            "missing": r.get("MISSING", ""),
            "notes": r.get("NOTES", ""),
        })
    return out


BACKUP = os.path.join(BASE, "_review-decisions.backup.csv")


def write_decision(target_file, decision, correction):
    """Rewrite one row's two editable columns, leaving every other cell -- and
    the hyperlink formulas -- exactly as they were.

    Two write paths, because the obvious one breaks the moment the sheet is
    open in Excel -- observed here, WinError 5 on os.replace. Note it surfaces
    as Access Denied, NOT the sharing violation you would expect, and Excel
    leaves no ~$ lock file for a .csv, so neither the error code nor the
    directory listing tells you the real cause. Rewriting the file in place
    succeeds even while Excel holds it open for reading.

    So: try the atomic rename first because it cannot half-write; fall back to
    in-place, having first copied the current file to BACKUP so that even the
    non-atomic path cannot leave the review with no recoverable record."""
    with io.open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        cols, rows = rd.fieldnames, list(rd)
    hit = 0
    for r in rows:
        if r["FILE"] == target_file:
            r["DECISION"], r["CORRECTION"], hit = decision, correction, hit + 1
    if not hit:
        raise KeyError(target_file)

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    text = buf.getvalue()

    fd, tmp = tempfile.mkstemp(dir=SPLITS, suffix=".csv")
    os.close(fd)
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        f.write(text)
    try:
        os.replace(tmp, CSV_PATH)
        return hit
    except OSError:
        pass                                    # locked -- fall through
    try:
        with io.open(BACKUP, "w", encoding="utf-8-sig", newline="") as f:
            f.write(io.open(CSV_PATH, encoding="utf-8-sig", newline="").read())
        with io.open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return hit


def extract_text(rel):
    """Return the invisible text layer of each page -- the corrected vision
    transcription that build.py embedded, and the thing that will actually be
    read when these files are indexed for search. The scan image is what you
    see; this is what a machine sees, and the two are worth comparing."""
    if not rel or ".." in rel:
        return {"error": "bad path"}
    path = os.path.join(SPLITS, rel.replace("/", os.sep))
    if not os.path.isfile(path):
        return {"error": "not found: %s" % rel}
    try:
        import fitz
    except ImportError:
        return {"error": "PyMuPDF is not installed in this Python."}
    doc = fitz.open(path)
    pages = [p.get_text().strip() for p in doc]
    doc.close()
    return {"pages": pages, "chars": sum(len(p) for p in pages)}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=SPLITS, **kw)

    def log_message(self, fmt, *args):
        # log_error() calls this with an HTTPStatus, not a request line, so the
        # args are NOT always strings. Assuming they were turned a harmless
        # favicon 404 into a TypeError that killed the whole request thread --
        # the connection just closed with no response, which looked like the
        # feature I had added being broken.
        line = str(args[0]) if args else ""
        if "/save" in line:
            sys.stderr.write("saved  %s\n" % line)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        path = unquote(self.path.split("?")[0])
        if path in ("/", "/index.html"):
            return self._send(200, io.open(APP_HTML, "rb").read(),
                              "text/html; charset=utf-8")
        if path == "/data":
            return self._send(200, json.dumps(read_rows()))
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        if path == "/text":
            return self._send(200, json.dumps(extract_text(
                unquote(parse_qs(urlparse(self.path).query).get("file", [""])[0]))))
        return super().do_GET()        # PDFs and anything else under splits/

    def do_POST(self):
        if unquote(self.path.split("?")[0]) != "/save":
            return self._send(404, '{"error":"not found"}')
        n = int(self.headers.get("Content-Length") or 0)
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            write_decision(p["file"], p.get("decision", ""), p.get("correction", ""))
            return self._send(200, '{"ok":true}')
        except PermissionError:
            return self._send(423, json.dumps({"error":
                "Windows refused to write the spreadsheet. Close it if you "
                "have it open in Excel, then click Retry. Your last saved "
                "decision is safe and a copy of the sheet is in the project "
                "folder as _review-decisions.backup.csv."}))
        except KeyError as e:
            return self._send(404, json.dumps({"error": "row not found: %s" % e}))
        except Exception as e:                                  # noqa: BLE001
            return self._send(500, json.dumps({"error": "%s: %s"
                                               % (type(e).__name__, e)}))


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    rows = read_rows()
    flagged = sum(1 for r in rows if r["review"])
    print("review app  ->  http://127.0.0.1:%d" % PORT)
    print("%d rows, %d flagged, %d already decided"
          % (len(rows), flagged, sum(1 for r in rows if r["decision"])))
    print("decisions save straight into splits/_review-decisions.csv")
    print("Ctrl-C to stop.")
    threading.Timer(1.0, webbrowser.open,
                    ("http://127.0.0.1:%d" % PORT,)).start()
    # 127.0.0.1, never 0.0.0.0 -- this serves probate documents and must not be
    # reachable from anything but this machine.
    Server(("127.0.0.1", PORT), Handler).serve_forever()
