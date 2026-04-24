/* Background service worker: talks to the Umbrel trigger server.
 *
 * Config is hardcoded below — edit these two lines if your Umbrel IP changes
 * or you rotate the token.
 */
const SERVER_URL = "http://umbrel.local:8787";
const TOKEN = "a6e6a97276b9c4cf76448e1290da01d6";

async function enqueue(payload) {
  const url = SERVER_URL.replace(/\/+$/, "") + "/download";
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Auth-Token": TOKEN,
      },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const text = await resp.text();
      return { ok: false, error: `HTTP ${resp.status}: ${text.slice(0, 200)}` };
    }
    const data = await resp.json();
    notify(
      "Download queued",
      `${payload.name}${payload.year ? ` (${payload.year})` : ""} — job #${data.job_id}`
    );
    return { ok: true, job_id: data.job_id };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create("udb-trigger-job", {
      type: "basic",
      iconUrl: "icons/icon128.png",
      title,
      message,
    });
  } catch (_) {
    /* best-effort */
  }
}

// When any UDB notification is clicked, open the dashboard in a new tab and
// dismiss the notification.
chrome.notifications.onClicked.addListener((notifId) => {
  // All our notifications share the id prefix "udb-trigger-" so we only
  // handle ours — leaves other extensions' notifications alone.
  if (!notifId || !notifId.startsWith("udb-trigger")) return;
  const dashboardUrl = SERVER_URL.replace(/\/+$/, "") + "/dashboard";
  chrome.tabs.create({ url: dashboardUrl });
  chrome.notifications.clear(notifId);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "enqueue") {
    enqueue(msg.payload).then(sendResponse);
    return true; // async
  }
  if (msg?.type === "getConfig") {
    sendResponse({ serverUrl: SERVER_URL, token: TOKEN });
    return false;
  }
});
