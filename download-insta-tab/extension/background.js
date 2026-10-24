// Service worker: reads the Instagram cookies, talks to the local server.py over
// WebSocket (a service worker isn't subject to Instagram's page CSP), and reports back.
// One short-lived socket per request keeps this robust against MV3 worker sleep; the
// server's progress messages double as keep-alive during long batches.

const SERVER_URL = "ws://127.0.0.1:7432";
const IG_URL = "https://www.instagram.com";
const STATUS_ALARM = "igdl-status";
const SHORTCODE_OK = /^[A-Za-z0-9_-]+$/;
const NAME_OK = /^[^\s/\\?#]{1,100}$/u;
const PAGE_TYPES = new Set(["profile", "hashtag"]);

let activeSocket = null; // the batch currently running, so it can be cancelled

async function getCookie(name) {
  const cookie = await chrome.cookies.get({ url: IG_URL, name });
  if (cookie) return cookie.value;
  // Fallback: match by domain in case the url lookup misses the cookie.
  const all = await chrome.cookies.getAll({ domain: "instagram.com", name });
  return all.length ? all[0].value : null;
}

/** The identity headers Chrome itself would send (client hints + Accept-Language). */
function getClientHeaders() {
  const headers = {};
  const uaData = navigator.userAgentData;
  if (uaData) {
    headers["sec-ch-ua"] = uaData.brands.map((b) => `"${b.brand}";v="${b.version}"`).join(", ");
    headers["sec-ch-ua-mobile"] = uaData.mobile ? "?1" : "?0";
    headers["sec-ch-ua-platform"] = `"${uaData.platform}"`;
  }
  if (navigator.languages?.length) {
    headers["accept-language"] = navigator.languages
      .map((lang, i) => (i === 0 ? lang : `${lang};q=${Math.max(0.1, 1 - i * 0.1).toFixed(1)}`))
      .join(",");
  }
  return headers;
}

async function getAuthPayload() {
  const [sessionid, csrftoken] = await Promise.all([getCookie("sessionid"), getCookie("csrftoken")]);
  if (!sessionid || !csrftoken) {
    const missing = [!sessionid && "sessionid", !csrftoken && "csrftoken"].filter(Boolean).join(", ");
    throw new Error(`Missing Instagram cookie(s): ${missing}. Log in to instagram.com in this Chrome profile.`);
  }
  // Send the browser's own identity so the server's requests match the browser that owns this session.
  return {
    type: "auth", sessionid, csrftoken,
    user_agent: navigator.userAgent,
    client_headers: getClientHeaders(),
  };
}

/** Send `steps` in order over a fresh socket; each step waits for the server's reply.
 *  With no steps it resolves with the server's initial "status" message.
 *  Options: onProgress(msg) for the server's "progress" messages, onSocket(ws) to grab the socket. */
function runSession(steps, { onProgress, onSocket } = {}) {
  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(SERVER_URL);
    } catch {
      reject(new Error("Cannot open socket to server.py"));
      return;
    }
    onSocket?.(ws);
    let stepIdx = 0;
    const finish = (fn, value) => {
      try { ws.close(); } catch { /* already closed */ }
      fn(value);
    };

    ws.onerror = () => finish(reject, new Error("Server not reachable. Is server.py running?"));
    ws.onclose = () => reject(new Error("Connection closed before finishing"));
    ws.onmessage = ({ data }) => {
      const msg = JSON.parse(data);
      if (msg.type === "progress") return onProgress?.(msg);
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

/** Judge the server's "done" message. A cancel is a normal outcome; Instagram pushing back,
 *  or a request where nothing was saved or skipped, is an error worth showing. */
function assertProgress(done) {
  if (done.cancelled) return done;
  if (done.aborted) {
    throw new Error(`Stopped early after ${done.downloaded} post(s): ${done.aborted}`);
  }
  if (!done.downloaded && !done.skipped) {
    throw new Error(done.error || "Nothing downloaded; see the server console for the error");
  }
  return done;
}

function relayProgress(tabId, update) {
  if (tabId != null) chrome.tabs.sendMessage(tabId, { ...update, type: "progress" }).catch(() => {});
}

/** Authenticate, send one download request, and relay the server's progress to `tabId`. */
async function runDownload(downloadPayload, tabId) {
  try {
    const done = await runSession(
      [
        { payload: await getAuthPayload(), reply: "auth_ok" },
        { payload: downloadPayload, reply: "done" },
      ],
      { onProgress: (update) => relayProgress(tabId, update), onSocket: (ws) => { activeSocket = ws; } },
    );
    return assertProgress(done);
  } finally {
    activeSocket = null;
  }
}

/** Mirror igdl(): the saved page downloads via the "collection" route (-> _saved folder,
 *  download-order stamping); everywhere else it is a plain single "post". */
function singlePayload(shortcode, fromSavedPage) {
  return fromSavedPage
    ? { type: "download", page_type: "collection", identifiers: [shortcode] }
    : { type: "download", page_type: "post", identifier: shortcode };
}

async function checkDownloaded(shortcodes) {
  const reply = await runSession([{ payload: { type: "check", shortcodes }, reply: "checked" }]);
  return reply.downloaded;
}

const validCodes = (codes) =>
  Array.isArray(codes) && codes.length > 0 && codes.every((c) => typeof c === "string" && SHORTCODE_OK.test(c));

const summarize = (done) => ({
  ok: true,
  downloaded: done.downloaded,
  skipped: done.skipped ?? 0,
  failed: done.failed ?? 0,
  cancelled: done.cancelled === true,
  target: done.target,
});

/** Reply to `sendResponse` from a promise, converting failures to {ok:false}. */
function respond(promise, sendResponse, onOk, refresh = true) {
  promise
    .then((value) => { sendResponse(onOk(value)); if (refresh) refreshStatus(); })
    .catch((err) => { sendResponse({ ok: false, error: err.message }); if (refresh) refreshStatus(); });
  return true; // async response
}

function refuse(sendResponse, error) {
  sendResponse({ ok: false, error });
  return false;
}

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
  const tabId = sender.tab?.id;
  switch (request?.type) {
    case "download":
      if (!validCodes([request.shortcode])) return false;
      return respond(runDownload(singlePayload(request.shortcode, request.fromSavedPage === true), tabId),
        sendResponse, summarize);

    case "download-many": // saved page: every given shortcode, via the collection route
      if (!validCodes(request.shortcodes)) return refuse(sendResponse, "No valid shortcodes");
      return respond(runDownload({
        type: "download", page_type: "collection",
        identifiers: request.shortcodes, count: request.shortcodes.length, skip: 0,
      }, tabId), sendResponse, summarize);

    case "download-page": // profile / hashtag page: the newest `count` posts
      if (!PAGE_TYPES.has(request.page_type) || !NAME_OK.test(request.identifier ?? "")
          || !Number.isInteger(request.count) || request.count < 1) {
        return refuse(sendResponse, "Invalid page download request");
      }
      return respond(runDownload({
        type: "download", page_type: request.page_type,
        identifier: request.identifier, count: request.count, skip: 0,
      }, tabId), sendResponse, summarize);

    case "cancel-batch":
      try { activeSocket?.send(JSON.stringify({ type: "cancel" })); } catch { /* socket already gone */ }
      sendResponse({ ok: true });
      return false;

    case "check":
      if (!validCodes(request.shortcodes)) return refuse(sendResponse, "No valid shortcodes");
      return respond(checkDownloaded(request.shortcodes), sendResponse,
        (downloaded) => ({ ok: true, downloaded }), false);

    case "status":
      refreshStatus().then(sendResponse);
      return true;

    default:
      return false;
  }
});
