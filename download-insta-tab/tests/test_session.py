import instaloader
import pytest
import requests

import session

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/142.0.0.0 Safari/537.36")
HINTS = {
    "sec-ch-ua": '"Chromium";v="142", "Google Chrome";v="142"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "accept-language": "en-US,en;q=0.9",
}
SESSIONID = "123456%3Aabc%3A1%3Adef"


def make_loader(username="someone"):
    loader = instaloader.Instaloader()
    loader.test_login = lambda: username  # no network
    return loader


def test_user_id_parses_encoded_and_plain_cookies():
    assert session.user_id_from_sessionid("123456%3Aabc%3A1%3Adef") == 123456
    assert session.user_id_from_sessionid("123456:abc:1:def") == 123456
    assert session.user_id_from_sessionid("garbage") is None


@pytest.mark.parametrize("value, ok", [
    (UA, True),
    (None, False), (42, False), ("curl/8.0", False),
    ("Mozilla/5.0 x\r\nX-Evil: 1", False),
    ("Mozilla/5.0 " + "a" * 400, False),
])
def test_valid_user_agent(value, ok):
    assert session.valid_user_agent(value) is ok


def test_clean_client_headers_whitelists_and_validates():
    cleaned = session.clean_client_headers({**HINTS, "x-evil": "nope", "sec-ch-ua-mobile": 5})
    assert set(cleaned) == {"sec-ch-ua", "sec-ch-ua-platform", "accept-language"}
    assert session.clean_client_headers({"sec-ch-ua": "a\r\nX: 1", "accept-language": "x" * 500}) == {}
    assert session.clean_client_headers("not a dict") == {}


def test_authenticate_applies_browser_identity_to_www_requests_only():
    loader = make_loader()

    assert session.authenticate(loader, SESSIONID, "csrf", UA, HINTS) == "someone"

    sess = loader.context._session
    prepare = lambda url: sess.prepare_request(requests.Request("GET", url)).headers
    www = prepare("https://www.instagram.com/graphql/query")
    assert www["User-Agent"] == UA
    assert www["sec-ch-ua-platform"] == '"Windows"' and www["accept-language"] == "en-US,en;q=0.9"
    assert (www["sec-fetch-site"], www["sec-fetch-mode"], www["sec-fetch-dest"]) == ("same-origin", "cors", "empty")
    for other in ("https://i.instagram.com/api/v1/x", "https://scontent.cdninstagram.com/a.jpg"):
        headers = prepare(other)
        assert "sec-fetch-site" not in headers and "sec-ch-ua" not in headers
    assert loader.context.user_id == 123456


def test_authenticate_without_browser_identity_keeps_instaloader_defaults():
    loader = make_loader()
    session.authenticate(loader, SESSIONID, "csrf")
    headers = loader.context._session.prepare_request(
        requests.Request("GET", "https://www.instagram.com/x")).headers
    assert "Linux" in headers["User-Agent"]  # instaloader's generic UA
    assert headers["sec-fetch-site"] == "same-origin"


def test_authenticate_rejects_dead_cookies_and_malformed_sessionid():
    with pytest.raises(ValueError, match="invalid or expired"):
        session.authenticate(make_loader(username=None), SESSIONID, "csrf")
    with pytest.raises(ValueError, match="user id"):
        session.authenticate(make_loader(), "garbage", "csrf")


def test_clean_cookies_keeps_only_device_cookies():
    cleaned = session.clean_cookies({"mid": "M1", "ig_did": "D1", "sessionid": "SECRET", "evil": "x",
                                     "rur": 'a"b,c', "datr": "bad\nvalue", "wd": ""})
    assert cleaned == {"mid": "M1", "ig_did": "D1", "rur": 'a"b,c'}
    assert session.clean_cookies(None) == {}


def test_authenticate_loads_device_cookies_into_the_session():
    loader = make_loader()
    session.authenticate(loader, SESSIONID, "csrf", UA, HINTS, {"mid": "M1", "ig_did": "D1"})
    jar = loader.context._session.cookies.get_dict()
    assert jar["mid"] == "M1" and jar["ig_did"] == "D1"
    assert jar["sessionid"] == SESSIONID and jar["csrftoken"] == "csrf"
