import asyncio
import json

import instaloader
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import server
from helpers import make_downloader, make_post


# ── origin check ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin, ext_id, ok", [
    (None, None, True),
    ("https://www.instagram.com", None, True),
    ("https://www.instagram.com.evil.net", None, False),  # prefix tricks must not pass
    ("https://evil.example", None, False),
    ("chrome-extension://abc", None, True),
    ("chrome-extension://abc", "abc", True),
    ("chrome-extension://other", "abc", False),
])
def test_origin_allowed(origin, ext_id, ok):
    assert server.origin_allowed(origin, ext_id) is ok


# ── command line + config file ───────────────────────────────────────────────

def write_config(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_config_file_supplies_defaults_and_cli_overrides_it(tmp_path):
    cfg = write_config(tmp_path, "output = 'D:/dl'\ndaily_cap = 50\npost_delay = [1.0, 2.0]\ndelete_originals = true\n")

    args = server.parse_args(["--config", str(cfg), "--daily-cap", "10"])

    assert args.output == "D:/dl" and args.delete_originals is True
    assert args.post_delay == (1.0, 2.0)
    assert args.daily_cap == 10  # CLI wins


def test_config_with_unknown_key_is_rejected(tmp_path):
    cfg = write_config(tmp_path, "outptu = 'typo'\n")
    with pytest.raises(SystemExit):
        server.parse_args(["--config", str(cfg)])


def test_custom_filename_pattern_requires_no_post_process():
    with pytest.raises(SystemExit):
        server.parse_args(["--filename-pattern", "{date}_{shortcode}"])
    args = server.parse_args(["--filename-pattern", "{date}_{shortcode}", "--no-post-process"])
    assert args.no_post_process is True


@pytest.mark.parametrize("argv", [
    ["--post-delay", "5", "2"], ["--max-batch", "0"], ["--daily-cap", "-1"], ["--sessionid", "only-one"],
])
def test_invalid_options_are_rejected(argv):
    with pytest.raises(SystemExit):
        server.parse_args(argv)


# ── login caching ────────────────────────────────────────────────────────────

def test_login_is_cached_per_cookie_and_browser_identity(tmp_path):
    dl, _ = make_downloader(tmp_path)
    loader = instaloader.Instaloader()
    calls = []
    loader.test_login = lambda: calls.append(1) or "someone"
    state = server.LoginState(loader, dl)
    sid = "123456%3Aabc%3A1%3Adef"
    ua = "Mozilla/5.0 (Windows NT 10.0)"

    assert state.login(sid, "csrf", ua) == "someone"
    state.login(sid, "csrf", ua)
    assert len(calls) == 1  # same identity: no second Instagram request
    state.login(sid, "csrf", "Mozilla/5.0 (Windows NT 11.0)")
    assert len(calls) == 2  # browser updated: re-login once
    assert state.authenticated and state.username == "someone"


# ── WebSocket protocol, end to end over a real socket ────────────────────────

def run_with_server(tmp_path, scenario, *, delay=0.0, authenticated=True, **settings):
    """Start the real handler on a free port and run `scenario(ws, downloader)` against it."""
    dl, loader = make_downloader(tmp_path, **settings)
    loader.delay = delay
    state = server.LoginState(loader, dl)
    state.authenticated = authenticated

    async def main():
        async with serve(server.make_handler(dl, state), "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                assert json.loads(await ws.recv())["type"] == "status"
                return await scenario(ws, dl)

    return asyncio.run(asyncio.wait_for(main(), timeout=30))


async def recv_until(ws, wanted):
    """Collect messages until one of type `wanted` arrives; returns (all_messages, that_message)."""
    seen = []
    while True:
        msg = json.loads(await ws.recv())
        seen.append(msg)
        if msg["type"] == wanted:
            return seen, msg


def fake_posts(monkeypatch):
    monkeypatch.setattr(instaloader.Post, "from_shortcode", staticmethod(lambda _ctx, code: make_post(code)))


def test_download_streams_progress_then_done(tmp_path, monkeypatch):
    fake_posts(monkeypatch)

    async def scenario(ws, _dl):
        await ws.send(json.dumps({"type": "download", "page_type": "collection",
                                  "identifiers": ["AAA", "BBB"], "count": 2}))
        return await recv_until(ws, "done")

    seen, done = run_with_server(tmp_path, scenario)

    assert [m["type"] for m in seen] == ["progress", "progress", "done"]
    assert [m["processed"] for m in seen[:2]] == [1, 2]
    assert done["downloaded"] == 2


def test_check_reports_what_is_on_disk(tmp_path):
    (tmp_path / "bob").mkdir()
    (tmp_path / "bob" / "bob_AAA.jpg").write_bytes(b"x")

    async def scenario(ws, _dl):
        await ws.send(json.dumps({"type": "check", "shortcodes": ["AAA", "BBB"]}))
        return (await recv_until(ws, "checked"))[1]

    assert run_with_server(tmp_path, scenario)["downloaded"] == ["AAA"]


def test_download_requires_login(tmp_path):
    async def scenario(ws, _dl):
        await ws.send(json.dumps({"type": "download", "page_type": "post", "identifier": "AAA"}))
        return await recv_until(ws, "error")

    _, err = run_with_server(tmp_path, scenario, authenticated=False)
    assert "Not authenticated" in err["error"]


def test_refused_request_comes_back_as_an_error_message(tmp_path):
    async def scenario(ws, _dl):
        await ws.send(json.dumps({"type": "download", "page_type": "post", "identifier": "AAA", "count": "x"}))
        return await recv_until(ws, "error")

    _, err = run_with_server(tmp_path, scenario)
    assert "count" in err["error"]


def test_cancel_message_stops_a_running_batch(tmp_path, monkeypatch):
    fake_posts(monkeypatch)

    async def scenario(ws, dl):
        await ws.send(json.dumps({"type": "download", "page_type": "collection",
                                  "identifiers": ["A1", "B2", "C3", "D4", "E5"], "count": 5}))
        await recv_until(ws, "progress")  # first post finished; the rest are still slow
        await ws.send(json.dumps({"type": "cancel"}))
        return (await recv_until(ws, "done"))[1]

    done = run_with_server(tmp_path, scenario, delay=0.3)

    assert done["cancelled"] is True and 1 <= done["downloaded"] < 5


def test_closing_the_connection_cancels_the_batch(tmp_path, monkeypatch):
    fake_posts(monkeypatch)
    dl, loader = make_downloader(tmp_path)
    loader.delay = 0.3
    state = server.LoginState(loader, dl)
    state.authenticated = True

    async def main():
        async with serve(server.make_handler(dl, state), "127.0.0.1", 0) as srv:
            port = srv.sockets[0].getsockname()[1]
            async with connect(f"ws://127.0.0.1:{port}") as ws:
                await ws.recv()
                await ws.send(json.dumps({"type": "download", "page_type": "collection",
                                          "identifiers": ["A1", "B2", "C3", "D4", "E5"], "count": 5}))
                await recv_until(ws, "progress")
            await asyncio.sleep(1.5)  # client gone; give the worker thread time to notice

    asyncio.run(asyncio.wait_for(main(), timeout=30))
    assert len(loader.downloaded) < 5


def test_refused_requests_are_logged_on_the_server_console(tmp_path, capsys):
    async def scenario(ws, _dl):
        await ws.send(json.dumps({"type": "download", "page_type": "collection",
                                  "identifiers": ["A", "B", "C"], "count": 3}))
        return await recv_until(ws, "error")

    _, err = run_with_server(tmp_path, scenario, max_batch=2)

    logged = capsys.readouterr()
    assert "COLLECTION" in logged.out  # the request itself is visible...
    assert "Refused" in logged.err and "max-batch" in logged.err  # ...and so is why it was refused
    assert "max-batch" in err["error"]


def test_login_relogs_when_the_device_identity_changes_but_not_for_rotating_cookies(tmp_path):
    dl, _ = make_downloader(tmp_path)
    loader = instaloader.Instaloader()
    calls = []
    loader.test_login = lambda: calls.append(1) or "someone"
    state = server.LoginState(loader, dl)
    sid, ua = "123456%3Aabc%3A1%3Adef", "Mozilla/5.0 (Windows NT 10.0)"

    state.login(sid, "csrf", ua, {}, {"mid": "M1", "ig_did": "D1", "rur": "v1"})
    state.login(sid, "csrf", ua, {}, {"mid": "M1", "ig_did": "D1", "rur": "v2"})  # rur rotates constantly
    assert len(calls) == 1
    state.login(sid, "csrf", ua, {}, {"mid": "M2", "ig_did": "D1"})  # a different device id
    assert len(calls) == 2
    assert loader.context._session.cookies.get_dict()["mid"] == "M2"


def test_safer_defaults_and_cooldown_option(tmp_path):
    empty = write_config(tmp_path, "")  # isolate from the repo's real config.toml
    args = server.parse_args(["--config", str(empty)])
    assert args.post_delay == (6.0, 15.0) and args.max_batch == 50 and args.daily_cap == 100
    assert args.cooldown_minutes == 120
    with pytest.raises(SystemExit):
        server.parse_args(["--cooldown-minutes", "0"])
