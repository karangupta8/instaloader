"""
Shared login + browser-identity helpers for server.py and scrape_comments.py.

Instagram sessions here come from raw browser cookies (no password login), optionally
dressed up with the real browser's user-agent and client-hint headers.
"""

import re
import urllib.parse

# Headers a real Chrome tab sends to www.instagram.com that python-requests doesn't.
CLIENT_HINT_KEYS = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform", "accept-language")
SAME_ORIGIN_FETCH = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}

# Besides sessionid/csrftoken, the browser holds cookies that identify this *device* (mid, ig_did,
# datr...). Without them every server start looks like a brand-new device using your session,
# which is the "multiple sessions" pattern Instagram restricts accounts for. Only these are forwarded.
DEVICE_COOKIE_NAMES = ("mid", "ig_did", "datr", "ds_user_id", "rur", "ig_nrcb", "wd", "dpr",
                       "ps_l", "ps_n", "shbid", "shbts", "ig_direct_region_hint")


def user_id_from_sessionid(sessionid: str) -> int | None:
    """Instagram's sessionid cookie is "<user_id>:<token>:<version>:<hash>" (colons may be
    percent-encoded), so the numeric user id can be read straight out of it with no extra request."""
    match = re.match(r"^(\d+)[:%]", urllib.parse.unquote(sessionid))
    return int(match.group(1)) if match else None


def valid_user_agent(user_agent: object) -> bool:
    """Accept only a plain browser-looking UA string (it ends up in an HTTP header)."""
    return (isinstance(user_agent, str) and user_agent.startswith("Mozilla/5.0")
            and len(user_agent) <= 300 and user_agent.isprintable())


def clean_client_headers(raw: object) -> dict[str, str]:
    """Keep only the whitelisted browser headers, with sane printable values."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for key in CLIENT_HINT_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value and len(value) <= 400 and value.isprintable():
            cleaned[key] = value
    return cleaned


def clean_cookies(raw: object) -> dict[str, str]:
    """Keep only the whitelisted device cookies, with sane printable values."""
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for name in DEVICE_COOKIE_NAMES:
        value = raw.get(name)
        if isinstance(value, str) and value and len(value) <= 1000 and value.isprintable():
            cleaned[name] = value
    return cleaned


def install_browser_headers(session, client_headers: dict[str, str]) -> None:
    """Add Chrome's client hints + fetch-metadata headers to www.instagram.com requests.

    Done per request (not as session defaults) so it never touches CDN downloads or the iPhone
    API, which use their own sessions and must not look like a browser tab.
    """
    extra = {**SAME_ORIGIN_FETCH, **client_headers}
    original = session.prepare_request

    def prepare_request(request):
        prepared = original(request)
        if urllib.parse.urlsplit(prepared.url).hostname == "www.instagram.com":
            prepared.headers.update(extra)
        return prepared

    session.prepare_request = prepare_request


def authenticate(loader, sessionid: str, csrftoken: str, user_agent: str | None = None,
                 client_headers: dict[str, str] | None = None,
                 device_cookies: dict[str, str] | None = None) -> str:
    """Load cookie values into `loader` and return the logged-in username.

    user_agent / client_headers, when given, are the real browser's, so requests match the browser
    that owns the session instead of instaloader's generic ones. The UA must be set *before*
    load_session(), which bakes it into the session headers. device_cookies are the browser's
    device-identifying cookies (see DEVICE_COOKIE_NAMES).

    Raises ValueError if the cookies are invalid/expired or malformed.
    """
    if user_agent:
        loader.context.user_agent = user_agent
    loader.context.load_session("unknown", {**(device_cookies or {}), "sessionid": sessionid,
                                            "csrftoken": csrftoken})
    # No public API for per-request headers; the context's session is what load_session() just built.
    install_browser_headers(loader.context._session, client_headers or {})  # pylint: disable=protected-access

    username = loader.test_login()
    if not username:
        raise ValueError("Session cookies are invalid or expired. Log in again in Chrome and retry.")

    loader.context.username = username
    # login() normally sets this from Instagram's response; load_session() (raw cookies) doesn't,
    # so the iPhone comments endpoint (used for posts with many comments) sends "ig-intended-user-id: None"
    # and Instagram rejects it with a generic "fail" response. Parse it from the sessionid cookie instead
    # of querying the profile endpoint, which is a separate call that gets rate-limited (429) quickly.
    user_id = user_id_from_sessionid(sessionid)
    if user_id is None:
        raise ValueError("Could not parse a user id out of the sessionid; make sure the full cookie value was used.")
    loader.context.user_id = user_id
    return username
