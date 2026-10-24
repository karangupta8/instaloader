// Service worker: reads the Instagram cookies, talks to the local server.py over
// WebSocket (a service worker isn't subject to Instagram's page CSP), and reports back.
// One short-lived socket per request keeps this robust against MV3 worker sleep.

const SERVER_URL = "ws://127.0.0.1:7432";
const IG_URL = "https://www.instagram.com";
const STATUS_ALARM = "igdl-status";
const SHORTCODE_OK = /^[A-Za-z0-9_-]+$/;

async function getCookie(name) {
  const cookie = await chrome.cookies.get({ url: IG_URL, name });
  if (cookie) return cookie.value;
  // Fallback: match by domain in case the url lookup misses the cookie.
  const all = await chrome.cookies.getAll({ domain: "instagram.com", name });
  return all.length ? all[0].value : null;
}

async function getAuthPayload() {
  const [sessionid, csrftoken] = await Promise.all([getCookie("sessionid"), getCookie("csrftoken")]);
  if (!sessionid || !csrftoken) {
    const missing = [!sessionid && "sessionid", !csrftoken && "csrftoken"].filter(Boolean).join(", ");
    throw new Error(`Missing Instagram cookie(s): ${missing}. Log in to instagram.com in this Chrome profile.`);
  }
  return { type: "auth", sessionid, csrftoken };
}

/** Send `steps` in order over a fresh socket; each step waits for the server's reply.
 *  With no steps it resolves with the server's initial "status" message. */
function runSession(steps) {
  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(SERVER_URL);
    } catch {
      reject(new Error("Cannot open socket to server.py"));
      return;
    }
    let stepIdx = 0;
    const finish = (fn, value) => {
      try { ws.close(); } catch { /* already closed */ }
      fn(value);
    };

    ws.onerror = () => finish(reject, new Error("Server not reachable. Is server.py running?"));
    ws.onclose = () => reject(new Error("Connection closed before finishing"));
    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data);
      if (msg.type === "error") return finish(reject, new Error(msg.error));
      // Server greets with "status"; replies to our steps follow.
      const expected = stepIdx === 0 ? "status" : steps[stepIdx - 1].reply;
      if (msg.type !== expected) return;
      if (stepIdx === steps.length) return finish(resolve, msg);
      ws.send(JSON.stringify(steps[stepIdx].payload));
      stepIdx += 1;
    };
  });
}

// ── Toolbar badge / status ──────────────────────────────────────────────────

function setBadge(text, color, title) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  chrome.action.setTitle({ title });
}

/** Probe the server (and our login) and reflect the result on the toolbar icon. */
async function refreshStatus() {
  let result;
  try {
    await runSession([]);
  } catch {
    result = { state: "down", message: "Server not running. Start: python download-insta-tab/server.py" };
    setBadge("OFF", "#c62828", result.message);
    return result;
  }
  try {
    const auth = await runSession([{ payload: await getAuthPayload(), reply: "auth_ok" }]);
    result = { state: "ok", user: auth.logged_in_as, output: auth.output };
    setBadge("ON", "#1a8f3c", `Server ready: @${auth.logged_in_as} -> ${auth.output}`);
  } catch (err) {
    result = { state: "auth", message: err.message };
    setBadge("!", "#ef6c00", err.message);
  }
  return result;
}

function scheduleStatusPolling() {
  chrome.alarms.create(STATUS_ALARM, { periodInMinutes: 1 });
  refreshStatus();
}

chrome.runtime.onInstalled.addListener(scheduleStatusPolling);
chrome.runtime.onStartup.addListener(scheduleStatusPolling);
// Only poll while an Instagram tab is open: no point probing (and logging a failed socket
// every minute) when nothing could use the server.
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== STATUS_ALARM) return;
  const tabs = await chrome.tabs.query({ url: `${IG_URL}/*` });
  if (tabs.length) refreshStatus();
});

// ── Downloads ───────────────────────────────────────────────────────────────

/** Mirror console.js: the saved page downloads via the "collection" route (-> _saved folder,
 *  download-order stamping); everywhere else it is a plain single "post". */
function downloadPayload(shortcode, fromSavedPage) {
  return fromSavedPage
    ? { type: "download", page_type: "collection", identifiers: [shortcode] }
    : { type: "download", page_type: "post", identifier: shortcode };
}

/** The collection route swallows per-post errors and reports 0 instead of failing. */
function assertProgress(done) {
  if (done.aborted) {
    throw new Error(`Stopped early after ${done.downloaded} post(s): ${done.aborted}. ` +
      "Instagram pushed back; downloads are paused for ~15 min. Avoid heavy use meanwhile.");
  }
  if (!done.downloaded && !done.skipped) {
    throw new Error("Nothing downloaded; see the server console for the error");
  }
  return done;
}

async function downloadShortcode(shortcode, fromSavedPage) {
  const done = await runSession([
    { payload: await getAuthPayload(), reply: "auth_ok" },
    { payload: downloadPayload(shortcode, fromSavedPage), reply: "done" },
  ]);
  return assertProgress(done);
}

/** Saved-page bulk download: same "collection" route igdl() uses, count = every shortcode given. */
async function downloadMany(shortcodes) {
  const done = await runSession([
    { payload: await getAuthPayload(), reply: "auth_ok" },
    {
      payload: { type: "download", page_type: "collection", identifiers: shortcodes, count: shortcodes.length, skip: 0 },
      reply: "done",
    },
  ]);
  return assertProgress(done);
}

async function checkDownloaded(shortcodes) {
  const reply = await runSession([{ payload: { type: "check", shortcodes }, reply: "checked" }]);
  return reply.downloaded;
}

const validCodes = (codes) =>
  Array.isArray(codes) && codes.length > 0 && codes.every((c) => typeof c === "string" && SHORTCODE_OK.test(c));

const summarize = (done) => ({
  ok: true, downloaded: done.downloaded, skipped: done.skipped ?? 0, target: done.target,
});

/** Reply to `sendResponse` from a promise, converting failures to {ok:false}. */
function respond(promise, sendResponse, onOk, refresh = true) {
  promise
    .then((value) => { sendResponse(onOk(value)); if (refresh) refreshStatus(); })
    .catch((err) => { sendResponse({ ok: false, error: err.message }); if (refresh) refreshStatus(); });
  return true; // async response
}

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  switch (request?.type) {
    case "download":
      if (!validCodes([request.shortcode])) return false;
      return respond(downloadShortcode(request.shortcode, request.fromSavedPage === true), sendResponse, summarize);
    case "download-many":
      if (!validCodes(request.shortcodes)) {
        sendResponse({ ok: false, error: "No valid shortcodes" });
        return false;
      }
      return respond(downloadMany(request.shortcodes), sendResponse, summarize);
    case "check":
      if (!validCodes(request.shortcodes)) {
        sendResponse({ ok: false, error: "No valid shortcodes" });
        return false;
      }
      return respond(checkDownloaded(request.shortcodes), sendResponse, (downloaded) => ({ ok: true, downloaded }), false);
    case "status":
      refreshStatus().then(sendResponse);
      return true;
    default:
      return false;
  }
});
