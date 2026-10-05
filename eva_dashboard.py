#!/usr/bin/env python
"""EVA dashboard — a local, read-only web UI at http://127.0.0.1:8765.

Separate process by design: it serves whatever is in ``data/`` — the live
presence file (written by whichever EVA process is running) plus the SQLite
store (tasks, rules, the action journal with its per-step reasons). It never
owns the app, so you can run it next to the CLI without touching either.

Run:   python eva_dashboard.py            (opens your browser)
Flags: --port 8765   --no-browser
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from eva.avatar.presence import presence_path, read_presence

DEFAULT_PORT = 8765


# ---------------------------------------------------------------------- data
def _rows(db_path: str, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """SELECT-only reads; a missing/locked DB yields [] (never an error page)."""
    try:
        conn = sqlite3.connect(db_path, timeout=1.5)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _task_summary(row: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(row.get("task_json") or "{}")
    except (TypeError, ValueError):
        raw = {}
    counters = raw.get("counters") or {}
    return {
        "id": raw.get("id") or row.get("id"),
        "goal": str(raw.get("goal") or raw.get("user_request") or "")[:200],
        "status": row.get("status") or raw.get("status") or "?",
        "mode": raw.get("mode") or "plan",
        "planned_by": raw.get("planned_by") or "",
        "message": str(raw.get("result_message") or "")[:300],
        "actions": counters.get("actions_run", 0),
        "actor_calls": counters.get("actor_calls", 0),
        "failures": len(raw.get("failures") or []),
        "created_at": raw.get("created_at") or row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def build_state(settings) -> dict[str, Any]:
    """Everything the page shows, assembled from the two files EVA already owns."""
    presence = read_presence(presence_path(settings))
    db = str(getattr(settings, "db_path", ""))
    tasks = [_task_summary(r) for r in _rows(
        db, "SELECT id, status, task_json, created_at, updated_at FROM tasks "
            "ORDER BY updated_at DESC LIMIT 12")]
    rules = [{"id": r.get("id"), "instruction": str(r.get("instruction") or "")[:200],
              "when": r.get("when_text") or "", "scope": r.get("scope") or "global",
              "forbid": json.loads(r.get("forbid_tools") or "[]"),
              "prefer": json.loads(r.get("prefer_tools") or "[]"),
              "used": r.get("used_count") or 0}
             for r in _rows(db, "SELECT * FROM rules WHERE active=1 "
                                "ORDER BY created_at DESC LIMIT 12")]
    activity = [{"ts": r.get("ts"), "tool": r.get("tool"), "outcome": r.get("outcome"),
                 "reason": str(r.get("reason") or "")[:160]}
                for r in _rows(db, "SELECT ts, tool, outcome, reason FROM journal "
                                   "ORDER BY id DESC LIMIT 15")]
    totals = {r["status"]: r["n"] for r in _rows(
        db, "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")}
    return {"presence": presence, "tasks": tasks, "rules": rules,
            "activity": activity, "totals": totals, "generated_at": time.time()}


# ---------------------------------------------------------------------- page
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EVA dashboard</title>
<style>
  :root { --bg:#0b1220; --card:#131c2e; --ink:#e8edf7; --dim:#8fa2c2; --line:#22304a; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:14px/1.5 "Segoe UI", system-ui, sans-serif; }
  header { display:flex; align-items:center; gap:12px; padding:18px 22px;
           border-bottom:1px solid var(--line); }
  h1 { font-size:20px; margin:0; letter-spacing:.5px; }
  .dot { width:11px; height:11px; border-radius:50%; background:#3fbf8f; }
  .dot.off { background:#e05b5b; } .dot.wait { background:#f2b04e; }
  #presence { color:var(--dim); } #presence b { color:var(--ink); font-weight:600; }
  main { display:grid; gap:14px; padding:16px 22px;
         grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); }
  section { background:var(--card); border:1px solid var(--line); border-radius:10px;
            padding:14px 16px; }
  h2 { font-size:12px; text-transform:uppercase; letter-spacing:1.2px;
       color:var(--dim); margin:0 0 10px; }
  .row { display:flex; gap:9px; align-items:baseline; padding:7px 0;
         border-top:1px solid var(--line); }
  .row:first-of-type { border-top:0; }
  .chip { font-size:11px; padding:1px 8px; border-radius:99px; border:1px solid var(--line); }
  .FINISHED { color:#34c77a; border-color:#1e5c40; }
  .BLOCKED { color:#f2b04e; border-color:#6b4d18; }
  .FAILED { color:#e05b5b; border-color:#6b2b2b; }
  .EXECUTING, .IDLE, .PENDING { color:#5b8ff9; border-color:#274a86; }
  .goal { flex:1; } .meta { color:var(--dim); font-size:12px; }
  .reason { color:var(--dim); font-size:12px; font-style:italic; }
  .empty { color:var(--dim); font-style:italic; }
  footer { color:var(--dim); font-size:12px; padding:6px 22px 20px; }
  code { background:#0e1728; padding:1px 5px; border-radius:4px; }
</style></head>
<body>
<header>
  <div class="dot" id="dot"></div><h1>EVA</h1>
  <div id="presence">connecting…</div>
</header>
<main>
  <section><h2>Recent tasks</h2><div id="tasks"></div></section>
  <section><h2>Remembered rules</h2><div id="rules"></div></section>
  <section><h2>Recent actions (with reasons)</h2><div id="activity"></div></section>
</main>
<footer>read-only view of <code>data/presence.json</code> + <code>data/eva.db</code> ·
refreshes every 2s · run goals from <code>python eva_cli.py</code></footer>
<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const line = html => html;
function render(s) {
  const p = s.presence || {};
  const dot = document.getElementById("dot");
  dot.className = "dot" + (p.stale ? " off" : (p.state === "WAITING" ? " wait" : ""));
  document.getElementById("presence").innerHTML = p.stale
    ? "<b>EVA not running</b>" +
      (p.headline && p.headline !== "ready" ? " · last: " + esc(p.headline) : "") +
      " — start <code>python eva_cli.py</code>"
    : "<b>" + esc(p.state) + "</b> · " + esc(p.headline || "") +
      (p.detail ? " · <span class='meta'>" + esc(p.detail) + "</span>" : "");
  const t = (s.tasks || []).map(x =>
    `<div class="row"><span class="chip ${esc(x.status)}">${esc(x.status)}</span>` +
    `<span class="goal">${esc(x.goal)}</span>` +
    `<span class="meta">${esc(x.mode)} · ${esc(x.planned_by || "-")} · ${x.actions} actions` +
    `${x.failures ? " · " + x.failures + " failures" : ""}</span></div>`).join("");
  document.getElementById("tasks").innerHTML =
    t || `<div class="empty">no tasks yet — run one from the CLI</div>`;
  const r = (s.rules || []).map(x =>
    `<div class="row"><span class="goal">${esc(x.instruction)}</span>` +
    `<span class="meta">when: ${esc(x.when || "*")} · scope: ${esc(x.scope)}` +
    `${x.forbid.length ? " · never: " + esc(x.forbid.join(", ")) : ""}` +
    ` · used ${x.used}</span></div>`).join("");
  document.getElementById("rules").innerHTML =
    r || `<div class="empty">no rules yet — say “from now on …”</div>`;
  const a = (s.activity || []).map(x =>
    `<div class="row"><span class="meta">${x.ts ? new Date(x.ts * 1000).toLocaleTimeString() : ""}</span>` +
    `<span class="goal">${esc(x.tool)} <span class="chip">${esc(x.outcome)}</span></span>` +
    `<span class="reason">${esc(x.reason)}</span></div>`).join("");
  document.getElementById("activity").innerHTML =
    a || `<div class="empty">journal is empty</div>`;
}
async function poll() {
  try { render(await (await fetch("/api/state")).json()); }
  catch (e) { document.getElementById("presence").textContent = "EVA dashboard: " + e; }
}
poll(); setInterval(poll, 2000);
</script>
</body></html>"""


# --------------------------------------------------------------------- server
def make_handler(settings):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):          # quiet: a dashboard is not a logger
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):                      # noqa: N802 (stdlib API)
            if self.path in ("/", "/index.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                payload = json.dumps(build_state(settings), ensure_ascii=False,
                                     default=str).encode("utf-8")
                self._send(200, payload, "application/json; charset=utf-8")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eva_dashboard",
                                     description="EVA local web dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser automatically")
    args = parser.parse_args(argv)

    from eva.config import get_settings
    settings = get_settings()
    url = f"http://127.0.0.1:{args.port}/"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(settings))
    print(f"EVA dashboard: {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
