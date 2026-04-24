/* Popup: show recent jobs + watched shows from the Umbrel trigger server.
 * Config is hardcoded — keep in sync with background.js.
 */
const SERVER_URL = "http://umbrel.local:8787";
const TOKEN = "a6e6a97276b9c4cf76448e1290da01d6";

const $ = (id) => document.getElementById(id);

function fmtJob(j) {
  const p = j.payload || {};
  const name = p.name || "(unknown)";
  const year = p.year ? ` (${p.year})` : "";
  return `
    <div class="job">
      <div class="name">${name}${year} <span class="badge ${j.status}">${j.status}</span></div>
      <div class="meta">job #${j.id} · ${new Date(j.created_at * 1000).toLocaleString()}</div>
    </div>
  `;
}

function fmtWatch(w) {
  const p = w.payload || {};
  const name = p.name || w.name || "(unknown)";
  const year = (p.year ?? w.year) ? ` (${p.year ?? w.year})` : "";
  const last = w.last_scanned_at
    ? `last rescan: ${new Date(w.last_scanned_at * 1000).toLocaleString()}`
    : "never rescanned";
  return `
    <div class="watch" data-id="${w.id}">
      <div class="row">
        <div>
          <div class="name">${name}${year}</div>
          <div class="meta">${last}</div>
        </div>
        <button class="unwatch" data-id="${w.id}">Unwatch</button>
      </div>
    </div>
  `;
}

async function api(path, opts = {}) {
  const url = SERVER_URL.replace(/\/+$/, "") + path;
  const resp = await fetch(url, {
    ...opts,
    headers: { ...(opts.headers || {}), "X-Auth-Token": TOKEN },
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function loadJobs() {
  try {
    const data = await api("/jobs?limit=10");
    if (!data.jobs.length) {
      $("jobs").textContent = "No jobs yet.";
      return;
    }
    $("jobs").innerHTML = data.jobs.map(fmtJob).join("");
  } catch (e) {
    $("jobs").innerHTML =
      `Can't reach server (${e.message}).<br/>URL: <code>${SERVER_URL}</code>`;
  }
}

async function loadWatches() {
  try {
    const data = await api("/watches");
    if (data.rescan_at) {
      $("rescan-at").textContent = `(daily @ ${data.rescan_at})`;
    }
    if (!data.watches.length) {
      $("watches").textContent = "No shows being watched.";
      return;
    }
    $("watches").innerHTML = data.watches.map(fmtWatch).join("");
    for (const btn of document.querySelectorAll("button.unwatch")) {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        btn.disabled = true;
        btn.textContent = "…";
        try {
          await api(`/watches/${id}`, { method: "DELETE" });
          loadWatches();
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "Unwatch";
          btn.title = e.message;
        }
      });
    }
  } catch (e) {
    $("watches").innerHTML = `Can't load watches (${e.message}).`;
  }
}

async function onRescanClick() {
  const btn = $("rescan-btn");
  btn.disabled = true;
  const orig = btn.textContent;
  btn.textContent = "Rescanning…";
  try {
    const data = await api("/watches/rescan", { method: "POST" });
    btn.textContent = `Queued ${data.enqueued}`;
    setTimeout(() => {
      btn.textContent = orig;
      btn.disabled = false;
      loadJobs();
      loadWatches();
    }, 1500);
  } catch (e) {
    btn.textContent = `Err: ${e.message}`;
    setTimeout(() => {
      btn.textContent = orig;
      btn.disabled = false;
    }, 2000);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  const dl = document.getElementById("dashboard-link");
  if (dl) dl.href = SERVER_URL.replace(/\/+$/, "") + "/dashboard";
  loadJobs();
  loadWatches();
  document.getElementById("rescan-btn").addEventListener("click", onRescanClick);
});
