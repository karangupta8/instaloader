// Service worker: reads the Instagram cookies, talks to the local server.py over
// WebSocket (a service worker isn't subject to Instagram's page CSP), and reports back.
// One short-lived socket per download keeps this robust against MV3 worker sleep.

const SERVER_URL = "ws://127.0.0.1:7432";
const IG_URL = "https://www.instagram.com";

async function getCookie(name) {
  const cookie = await chrome.cookies.get({ url: IG_URL, name });
  if (cookie) return cookie.value;
  // Fallback: match by domain in case the url lookup misses the cookie.
  const all = await chrome.cookies.getAll({ domain: "instagram.com", name });
  return all.length ? all[0].value : null;
}

/** Send `messages` in order over a fresh socket; each step waits for the server's reply. */
function runSession(steps) {
  return new Promise((resolve, reject) => {
    let ws;
    try {
      ws = new WebSocket(SERVER_URL);
    } catch (err) {
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
      // Server greets with "status"; replies to our steps are "auth_ok" / "done".
      const expected = stepIdx === 0 ? "status" : steps[stepIdx - 1].reply;
      if (msg.type !== expected) return;
      if (stepIdx === steps.length) return finish(resolve, msg);
      ws.send(JSON.stringify(steps[stepIdx].payload));
      stepIdx += 1;
    };
  });
}

/** Mirror console.js: the saved page downloads via the "collection" route (-> _saved folder,
 *  download-order stamping); everywhere else it is a plain single "post". */
function downloadPayload(shortcode, fromSavedPage) {
  return fromSavedPage
    ? { type: "download", page_type: "collection", identifiers: [shortcode] }
    : { type: "download", page_type: "post", identifier: shortcode };
}

async function downloadShortcode(shortcode, fromSavedPage) {
  const [sessionid, csrftoken] = await Promise.all([getCookie("sessionid"), getCookie("csrftoken")]);
  if (!sessionid || !csrftoken) {
    throw new Error(`Missing Instagram cookie(s): ${[!sessionid && "sessionid", !csrftoken && "csrftoken"]
      .filter(Boolean).join(", ")}. Log in to instagram.com in this Chrome profile.`);
  }
  const done = await runSession([
    { payload: { type: "auth", sessionid, csrftoken }, reply: "auth_ok" },
    { payload: downloadPayload(shortcode, fromSavedPage), reply: "done" },
  ]);
  // The collection route swallows per-post errors and reports 0 instead of failing.
  if (!done.downloaded) throw new Error("Nothing downloaded; see the server console for the error");
  return done;
}

/** Saved-page bulk download: same "collection" route igdl() uses, count = every shortcode given. */
async function downloadMany(shortcodes) {
  const [sessionid, csrftoken] = await Promise.all([getCookie("sessionid"), getCookie("csrftoken")]);
  if (!sessionid || !csrftoken) {
    throw new Error("Missing Instagram cookies. Log in to instagram.com in this Chrome profile.");
  }
  const done = await runSession([
    { payload: { type: "auth", sessionid, csrftoken }, reply: "auth_ok" },
    {
      payload: { type: "download", page_type: "collection", identifiers: shortcodes, count: shortcodes.length, skip: 0 },
      reply: "done",
    },
  ]);
  if (!done.downloaded) throw new Error("Nothing downloaded; see the server console for the error");
  return done;
}

chrome.runtime.onMessage.addListener((request, _sender, sendResponse) => {
  if (request?.type === "download-many") {
    const codes = Array.isArray(request.shortcodes) ? request.shortcodes : [];
    if (!codes.length || !codes.every((c) => /^[A-Za-z0-9_-]+$/.test(c))) {
      sendResponse({ ok: false, error: "No valid shortcodes" });
      return false;
    }
    downloadMany(codes)
      .then((done) => sendResponse({ ok: true, downloaded: done.downloaded, target: done.target }))
      .catch((err) => sendResponse({ ok: false, error: err.message }));
    return true;
  }
  if (request?.type !== "download" || !/^[A-Za-z0-9_-]+$/.test(request.shortcode ?? "")) {
    return false;
  }
  downloadShortcode(request.shortcode, request.fromSavedPage === true)
    .then((done) => sendResponse({ ok: true, downloaded: done.downloaded, target: done.target }))
    .catch((err) => sendResponse({ ok: false, error: err.message }));
  return true; // async response
});
