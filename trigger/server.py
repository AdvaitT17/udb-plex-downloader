"""FastAPI trigger service.

Endpoints:
  POST   /download        enqueue a download job
  GET    /jobs            list recent jobs
  GET    /jobs/{id}       fetch one job (incl. log tail)
  GET    /watches         list shows being auto-rescanned
  POST   /watches         add a show to the rescan list
  DELETE /watches/{id}    remove a show from the rescan list
  POST   /watches/rescan  force-trigger a rescan now
  GET    /health          liveness probe

Auth: shared secret via `X-Auth-Token` header (env var UDB_TRIGGER_TOKEN).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .db import JobDB
from .worker import Worker


logger = logging.getLogger("udb_trigger")


# --- configuration via env ---------------------------------------------------
UDB_ROOT = Path(os.environ.get("UDB_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_FILE = Path(os.environ.get("UDB_CONFIG", UDB_ROOT / "config_udb.yaml"))
DATA_DIR = Path(os.environ.get("UDB_TRIGGER_DATA", UDB_ROOT / "trigger" / "data"))
LOG_DIR = Path(os.environ.get("UDB_TRIGGER_LOGS", DATA_DIR / "logs"))
DB_PATH = Path(os.environ.get("UDB_TRIGGER_DB", DATA_DIR / "queue.sqlite"))
TOKEN = os.environ.get("UDB_TRIGGER_TOKEN", "")
PYTHON_BIN = os.environ.get("UDB_PYTHON", "python3")
PLEX_REFRESH_URL = os.environ.get("UDB_PLEX_REFRESH_URL") or None
# Download root inside the container (maps to the Plex library via bind mount).
# Used by the worker to rename files into Plex-friendly format post-download.
DOWNLOAD_ROOT = Path(os.environ.get("UDB_DOWNLOAD_ROOT", "/downloads"))
# "HH:MM" local-time of day to rescan watched ongoing shows. 0-disabled if empty.
RESCAN_AT = os.environ.get("UDB_RESCAN_AT", "04:15").strip()

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

db = JobDB(DB_PATH)
worker = Worker(
    db=db,
    udb_root=UDB_ROOT,
    config_file=CONFIG_FILE,
    log_dir=LOG_DIR,
    python_bin=PYTHON_BIN,
    plex_refresh_url=PLEX_REFRESH_URL,
    download_root=DOWNLOAD_ROOT,
)


# --- auth --------------------------------------------------------------------
def require_token(x_auth_token: Optional[str] = Header(default=None)) -> None:
    if not TOKEN:
        # running without a token configured is only allowed for health
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UDB_TRIGGER_TOKEN not configured on server",
        )
    if x_auth_token != TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


# --- models ------------------------------------------------------------------
class DownloadRequest(BaseModel):
    name: str = Field(..., description="Series name to search for in UDB")
    year: Optional[int] = None
    series_type: int = Field(
        2, description="UDB client index (1=Animepahe, 2=KissKh)"
    )
    seasons: Optional[list[str]] = None
    episodes: Optional[list[str]] = Field(
        default=None,
        description="Episode numbers or ranges, e.g. ['1-12'] or ['3','5','7-9']",
    )
    resolution: Optional[str] = None
    # free-form label shown in UI; not passed to udb
    source_url: Optional[str] = None
    # If true, also register this show for daily auto-rescan so newly-aired
    # episodes of ongoing shows get downloaded without re-clicking the button.
    watch: bool = False


class WatchRequest(BaseModel):
    name: str
    year: Optional[int] = None
    series_type: int = 2
    seasons: Optional[list[str]] = None
    episodes: Optional[list[str]] = None
    resolution: Optional[str] = None
    source_url: Optional[str] = None


# --- rescan helpers ----------------------------------------------------------
def _rescan_all_watches() -> int:
    """Enqueue a job for every watched show. Called daily by APScheduler."""
    watches = db.list_watches()
    logger.info("rescan: enqueueing %d watched shows", len(watches))
    enqueued = 0
    for w in watches:
        payload = w["payload"] if isinstance(w["payload"], dict) else {}
        # Strip any transient fields; keep the core ones.
        job_payload = {
            "name": payload.get("name") or w.get("name"),
            "year": payload.get("year") if payload.get("year") is not None else w.get("year"),
            "series_type": payload.get("series_type", 2),
            "seasons": payload.get("seasons"),
            "episodes": payload.get("episodes"),
            "resolution": payload.get("resolution") or "720",
            "source_url": payload.get("source_url"),
        }
        try:
            db.enqueue(job_payload)
            db.mark_watch_scanned(w["id"])
            enqueued += 1
        except Exception:
            logger.exception("rescan: failed to enqueue watch id=%s", w.get("id"))
    return enqueued


def _parse_hhmm(s: str) -> tuple[int, int] | None:
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


scheduler: BackgroundScheduler | None = None


# --- app ---------------------------------------------------------------------
app = FastAPI(title="UDB Trigger", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # LAN-only + token-gated; extension origin varies by install id
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    worker.start()
    # Schedule daily rescan of watched shows
    global scheduler
    hhmm = _parse_hhmm(RESCAN_AT) if RESCAN_AT else None
    if hhmm:
        scheduler = BackgroundScheduler(timezone=os.environ.get("TZ") or "UTC")
        scheduler.add_job(
            _rescan_all_watches,
            CronTrigger(hour=hhmm[0], minute=hhmm[1]),
            id="daily_watch_rescan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.start()
        logger.info("scheduler started; daily rescan at %02d:%02d", hhmm[0], hhmm[1])
    else:
        logger.info("scheduler disabled (UDB_RESCAN_AT=%r)", RESCAN_AT)


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()
    if scheduler is not None:
        scheduler.shutdown(wait=False)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "token_configured": bool(TOKEN)}


@app.post("/download", dependencies=[Depends(require_token)])
def enqueue(req: DownloadRequest) -> dict:
    # KissKh search returns many matches per keyword. udb auto-resolves when
    # (a) year is provided, (b) there's only one match, or (c) one match's
    # title is an exact (case-insensitive) hit on the keyword — which is why
    # the extension wraps the title in double quotes. If none of those hold,
    # udb errors out non-interactively rather than dropping into a prompt.
    payload = req.model_dump()
    # Strip the watch flag before passing to udb — it's a server-only concern.
    watch_flag = bool(payload.pop("watch", False))
    job_id = db.enqueue(payload)
    watch_id = None
    if watch_flag:
        try:
            watch_id = db.add_watch(payload)
        except Exception:
            logger.exception("failed to register watch for %s", payload.get("name"))
    return {"job_id": job_id, "status": "queued", "watch_id": watch_id}


@app.get("/watches", dependencies=[Depends(require_token)])
def list_watches() -> dict:
    return {"watches": db.list_watches(), "rescan_at": RESCAN_AT}


@app.post("/watches", dependencies=[Depends(require_token)])
def add_watch(req: WatchRequest) -> dict:
    watch_id = db.add_watch(req.model_dump())
    return {"watch_id": watch_id}


@app.delete("/watches/{watch_id}", dependencies=[Depends(require_token)])
def delete_watch(watch_id: int) -> dict:
    ok = db.remove_watch(watch_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.post("/watches/rescan", dependencies=[Depends(require_token)])
def rescan_now() -> dict:
    n = _rescan_all_watches()
    return {"enqueued": n}


# --- job controls ------------------------------------------------------------
@app.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_token)])
def cancel_job(job_id: int) -> dict:
    job = db.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    if job["status"] == "running":
        ok = worker.cancel_job(job_id)
        return {"cancelled": ok, "status": "killing"}
    if job["status"] == "queued":
        # No subprocess yet — just mark it failed so claim_next won't pick it.
        db.finish(job_id, 130, error="Cancelled by user before start")
        return {"cancelled": True, "status": "failed"}
    return {"cancelled": False, "status": job["status"], "reason": "not active"}


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_token)])
def delete_job(job_id: int) -> dict:
    job = db.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    try:
        deleted = db.delete(job_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    # Best-effort cleanup of the log file on disk.
    log_path = (deleted or {}).get("log_path")
    if log_path:
        try:
            Path(log_path).unlink(missing_ok=True)
        except Exception:
            pass
    return {"deleted": True, "id": job_id}


@app.post("/jobs/cleanup", dependencies=[Depends(require_token)])
def cleanup_jobs(keep_last: int = Query(0, ge=0, le=500)) -> dict:
    """Delete all done/failed jobs except the most recent `keep_last`."""
    deleted = db.cleanup_finished(keep_last=keep_last)
    for row in deleted:
        log_path = row.get("log_path")
        if log_path:
            try:
                Path(log_path).unlink(missing_ok=True)
            except Exception:
                pass
    return {"deleted": len(deleted)}


# --- dashboard ---------------------------------------------------------------
# Served by FastAPI directly. Auth happens client-side: the dashboard asks for
# the token on first load and stores it in localStorage, then attaches it on
# every subsequent API call. That keeps this route reachable even when the
# user hasn't configured anything yet.
_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>UDB Trigger — Dashboard</title>
<style>
  :root {
    --bg: #121212; --panel: #1e1e1e; --panel2: #262626; --text: #e0e0e0;
    --muted: #888; --red: #e50914; --green: #4caf50; --yellow: #ff9800;
    --grey: #555;
  }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, system-ui, Segoe UI, sans-serif;
         background: var(--bg); color: var(--text); margin: 0; padding: 16px;
         min-height: 100vh; }
  h1 { font-size: 20px; margin: 0 0 16px; display: flex; align-items: center;
       gap: 10px; }
  h1 .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--green);
            display: inline-block; }
  h1 .dot.off { background: var(--red); }
  h2 { font-size: 14px; margin: 20px 0 8px; color: var(--muted);
       text-transform: uppercase; letter-spacing: 0.5px; }
  .panel { background: var(--panel); border-radius: 8px; padding: 12px 14px;
           margin-bottom: 12px; }
  .row { display: flex; gap: 12px; align-items: center; justify-content: space-between;
         padding: 8px 0; border-bottom: 1px solid #2a2a2a; }
  .row:last-child { border-bottom: none; }
  .row .meta { color: var(--muted); font-size: 12px; }
  .name { font-weight: 600; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px;
           font-size: 11px; color: #fff; font-weight: 600; }
  .badge.queued  { background: var(--grey); }
  .badge.running { background: var(--yellow); }
  .badge.done    { background: var(--green); }
  .badge.failed  { background: var(--red); }
  button { background: #333; color: var(--text); border: 1px solid #444;
           border-radius: 6px; padding: 4px 10px; font: inherit; font-size: 12px;
           cursor: pointer; }
  button:hover:not(:disabled) { background: #444; }
  button.primary { background: var(--red); border-color: var(--red);
                   font-weight: 600; padding: 6px 12px; font-size: 13px; }
  button.primary:hover { background: #ff2b36; }
  button.danger:hover { background: #8b1a1a; border-color: var(--red); color: #fff; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .controls { display: flex; gap: 6px; flex-wrap: wrap; }
  .err { color: var(--red); font-size: 12px; margin-top: 4px; font-family: monospace;
         word-break: break-word; }
  .actions { display: flex; gap: 8px; margin: 8px 0 4px; }
  .hidden { display: none !important; }
  pre.log { background: #000; color: #ddd; padding: 10px; border-radius: 6px;
            max-height: 280px; overflow: auto; font-size: 11px;
            white-space: pre-wrap; word-break: break-word; }
  .auth { max-width: 420px; margin: 60px auto; }
  .auth input { width: 100%; background: #111; color: var(--text); border: 1px solid #333;
                border-radius: 6px; padding: 8px 10px; font: inherit; margin: 8px 0; }
  a.plain { color: var(--yellow); }
</style>
</head>
<body>
<div id="auth-gate" class="auth panel hidden">
  <h1><span class="dot off"></span> Not authenticated</h1>
  <div>Enter the UDB trigger token to continue. Saved in localStorage on this browser only.</div>
  <input id="auth-token" type="password" placeholder="X-Auth-Token" autocomplete="off" />
  <button class="primary" id="auth-submit">Continue</button>
  <div id="auth-err" class="err"></div>
</div>

<div id="app" class="hidden">
  <h1><span class="dot" id="live-dot"></span> UDB Trigger Dashboard</h1>

  <div class="panel">
    <div class="actions">
      <button id="refresh-btn">Refresh</button>
      <button id="rescan-btn" class="primary">Rescan watched shows now</button>
      <button id="cleanup-btn">Cleanup finished (keep 10)</button>
      <span style="margin-left: auto; color: var(--muted); font-size: 12px;"
            id="last-updated"></span>
    </div>
  </div>

  <h2>Jobs</h2>
  <div id="jobs" class="panel">Loading…</div>

  <h2>Watching <span id="rescan-at" style="color:var(--muted); font-weight:normal; text-transform:none;"></span></h2>
  <div id="watches" class="panel">Loading…</div>
</div>

<script>
const BASE = location.origin;
const STORAGE = "udb_trigger_token";

function $(id) { return document.getElementById(id); }
function setHidden(el, h) { el.classList[h ? "add" : "remove"]("hidden"); }
function esc(s) { return String(s ?? "").replace(/[&<>'"]/g,
  c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c])); }

let TOKEN = localStorage.getItem(STORAGE) || "";
// Track which job logs the user has expanded so polling doesn't collapse them.
const openLogs = new Set();

async function api(path, opts = {}) {
  const resp = await fetch(BASE + path, {
    ...opts,
    headers: { ...(opts.headers || {}), "X-Auth-Token": TOKEN, "Content-Type": "application/json" },
  });
  if (resp.status === 401) {
    localStorage.removeItem(STORAGE);
    TOKEN = "";
    showAuthGate("Token rejected.");
    throw new Error("unauthorized");
  }
  if (!resp.ok) throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  return resp.json();
}

function showAuthGate(err) {
  setHidden($("auth-gate"), false);
  setHidden($("app"), true);
  if (err) $("auth-err").textContent = err;
}

function hideAuthGate() {
  setHidden($("auth-gate"), true);
  setHidden($("app"), false);
}

$("auth-submit").addEventListener("click", async () => {
  const v = $("auth-token").value.trim();
  if (!v) return;
  TOKEN = v;
  try {
    await api("/jobs?limit=1");
    localStorage.setItem(STORAGE, TOKEN);
    hideAuthGate();
    startLoop();
  } catch (e) { /* api() already showed gate with err */ }
});
$("auth-token").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("auth-submit").click();
});

function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  return d.toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
}

function jobRow(j) {
  const p = j.payload || {};
  const name = p.name || "(unknown)";
  const year = p.year ? ` (${p.year})` : "";
  const res = p.resolution ? ` · ${p.resolution}P` : "";
  const statusBadge = `<span class="badge ${j.status}">${j.status}</span>`;
  const times = [
    j.created_at  && `queued ${fmtTime(j.created_at)}`,
    j.started_at  && `started ${fmtTime(j.started_at)}`,
    j.finished_at && `finished ${fmtTime(j.finished_at)}`,
  ].filter(Boolean).join(" · ");
  const ctrls = [];
  if (j.status === "queued")  ctrls.push(`<button class="cancel-btn" data-id="${j.id}">Cancel</button>`);
  if (j.status === "running") ctrls.push(`<button class="cancel-btn danger" data-id="${j.id}">Kill</button>`);
  if (j.status === "done" || j.status === "failed")
    ctrls.push(`<button class="delete-btn danger" data-id="${j.id}">Delete</button>`);
  ctrls.push(`<button class="log-btn" data-id="${j.id}">Log</button>`);
  const errLine = j.error ? `<div class="err">${esc(j.error)}</div>` : "";
  return `
    <div class="row" data-id="${j.id}">
      <div style="min-width:0; flex:1;">
        <div class="name">${esc(name)}${esc(year)}${esc(res)} ${statusBadge}</div>
        <div class="meta">job #${j.id}${times ? " · " + times : ""}</div>
        ${errLine}
        <pre class="log hidden" id="log-${j.id}"></pre>
      </div>
      <div class="controls">${ctrls.join("")}</div>
    </div>
  `;
}

function watchRow(w) {
  const p = w.payload || {};
  const name = p.name || w.name || "(unknown)";
  const year = (p.year ?? w.year) ? ` (${p.year ?? w.year})` : "";
  const last = w.last_scanned_at ? `last rescan ${fmtTime(w.last_scanned_at)}` : "never rescanned";
  return `
    <div class="row" data-id="${w.id}">
      <div>
        <div class="name">${esc(name)}${esc(year)}</div>
        <div class="meta">${last}</div>
      </div>
      <div class="controls">
        <button class="unwatch-btn danger" data-id="${w.id}">Unwatch</button>
      </div>
    </div>
  `;
}

async function loadJobs() {
  try {
    const data = await api("/jobs?limit=30");
    if (!data.jobs.length) {
      $("jobs").textContent = "No jobs yet.";
      return;
    }
    $("jobs").innerHTML = data.jobs.map(jobRow).join("");
    for (const b of document.querySelectorAll(".cancel-btn")) {
      b.addEventListener("click", async () => {
        if (!confirm("Cancel this job? Partial files stay; udb will skip them on retry.")) return;
        b.disabled = true;
        try { await api(`/jobs/${b.dataset.id}/cancel`, { method: "POST" }); loadJobs(); }
        catch (e) { alert(e.message); b.disabled = false; }
      });
    }
    for (const b of document.querySelectorAll(".delete-btn")) {
      b.addEventListener("click", async () => {
        if (!confirm("Delete this job record and its log?")) return;
        b.disabled = true;
        try { await api(`/jobs/${b.dataset.id}`, { method: "DELETE" }); loadJobs(); }
        catch (e) { alert(e.message); b.disabled = false; }
      });
    }
    for (const b of document.querySelectorAll(".log-btn")) {
      b.addEventListener("click", async () => {
        const id = b.dataset.id;
        const box = document.getElementById(`log-${id}`);
        if (!box.classList.contains("hidden")) {
          box.classList.add("hidden");
          openLogs.delete(id);
          return;
        }
        openLogs.add(id);
        await refreshLog(id);
      });
    }
    // Re-attach / refresh any logs that were open before this re-render.
    for (const id of Array.from(openLogs)) {
      const box = document.getElementById(`log-${id}`);
      if (!box) { openLogs.delete(id); continue; }
      refreshLog(id);
    }
  } catch (e) {
    $("jobs").textContent = `Error: ${e.message}`;
  }
}

async function refreshLog(id) {
  const box = document.getElementById(`log-${id}`);
  if (!box) return;
  try {
    const j = await api(`/jobs/${id}?log_tail=200`);
    // Preserve scroll position if the user is reading mid-log.
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 20;
    box.textContent = j.log_tail || "(empty)";
    box.classList.remove("hidden");
    if (atBottom) box.scrollTop = box.scrollHeight;
  } catch (e) {
    box.textContent = e.message;
    box.classList.remove("hidden");
  }
}

async function loadWatches() {
  try {
    const data = await api("/watches");
    if (data.rescan_at) $("rescan-at").textContent = `(daily @ ${data.rescan_at})`;
    if (!data.watches.length) { $("watches").textContent = "No shows being watched."; return; }
    $("watches").innerHTML = data.watches.map(watchRow).join("");
    for (const b of document.querySelectorAll(".unwatch-btn")) {
      b.addEventListener("click", async () => {
        b.disabled = true;
        try { await api(`/watches/${b.dataset.id}`, { method: "DELETE" }); loadWatches(); }
        catch (e) { alert(e.message); b.disabled = false; }
      });
    }
  } catch (e) {
    $("watches").textContent = `Error: ${e.message}`;
  }
}

async function refreshAll() {
  await Promise.all([loadJobs(), loadWatches()]);
  $("last-updated").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

$("refresh-btn").addEventListener("click", refreshAll);
$("rescan-btn").addEventListener("click", async () => {
  const b = $("rescan-btn");
  b.disabled = true; const orig = b.textContent; b.textContent = "Rescanning…";
  try {
    const data = await api("/watches/rescan", { method: "POST" });
    b.textContent = `Queued ${data.enqueued}`;
  } catch (e) { b.textContent = `Err: ${e.message}`; }
  setTimeout(() => { b.textContent = orig; b.disabled = false; refreshAll(); }, 1500);
});
$("cleanup-btn").addEventListener("click", async () => {
  if (!confirm("Delete all finished/failed jobs except the 10 most recent?")) return;
  try {
    const data = await api("/jobs/cleanup?keep_last=10", { method: "POST" });
    alert(`Deleted ${data.deleted} jobs.`);
    refreshAll();
  } catch (e) { alert(e.message); }
});

let _loopTimer = null;
function startLoop() {
  refreshAll();
  if (_loopTimer) clearInterval(_loopTimer);
  _loopTimer = setInterval(refreshAll, 4000);
}

// --- init ---
if (!TOKEN) {
  showAuthGate();
} else {
  // Verify token first
  (async () => {
    try { await api("/jobs?limit=1"); hideAuthGate(); startLoop(); }
    catch (_) { /* gate already shown on 401 */ }
  })();
}
</script>
</body>
</html>
"""


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_DASHBOARD_HTML)


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    # Convenience: redirect-like — send users straight to the dashboard.
    return HTMLResponse(
        content='<meta http-equiv="refresh" content="0; url=/dashboard">',
        status_code=200,
    )


@app.get("/jobs", dependencies=[Depends(require_token)])
def list_jobs(limit: int = 50) -> dict:
    return {"jobs": db.list(limit=limit)}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_token)])
def get_job(job_id: int, log_tail: int = 50) -> dict:
    job = db.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="not found")
    log_lines: list[str] = []
    if job.get("log_path"):
        try:
            with open(job["log_path"], "r", errors="replace") as f:
                log_lines = f.readlines()[-max(1, log_tail):]
        except FileNotFoundError:
            pass
    job["log_tail"] = "".join(log_lines)
    return job
