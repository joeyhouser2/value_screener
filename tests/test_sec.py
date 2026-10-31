"""SEC client: User-Agent check, caching (warm cache = 0 calls), 404 caching, retries."""

import pytest
import requests

from value_screener.sec import RateLimiter, SecClient

UA = "Test User test@example.com"


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.urls = []

    def get(self, url, timeout):
        self.urls.append(url)
        return self.responses.pop(0)


def _client(tmp_path, responses):
    return SecClient(UA, tmp_path, rps=1000, base_delay=0, session=_Session(responses))


def test_rejects_placeholder_user_agent(tmp_path):
    with pytest.raises(ValueError):
        SecClient("Joey Houser <email>", tmp_path)


def test_user_agent_header_set(tmp_path):
    c = _client(tmp_path, [])
    assert c.session.headers["User-Agent"] == UA


def test_warm_cache_makes_no_calls(tmp_path):
    c = _client(tmp_path, [_Resp(200, {"a": 1})])
    assert c.get_json("https://x/1", "one") == {"a": 1}
    c2 = _client(tmp_path, [])                       # would fail if it hit the network
    assert c2.get_json("https://x/1", "one") == {"a": 1}
    assert c.calls == 1 and c2.calls == 0


def test_404_cached_as_missing(tmp_path):
    c = _client(tmp_path, [_Resp(404)])
    assert c.get_json("https://x/2", "two") is None
    c2 = _client(tmp_path, [])
    assert c2.get_json("https://x/2", "two") is None and c2.calls == 0


def test_retries_transient_errors(tmp_path):
    c = _client(tmp_path, [_Resp(429), _Resp(503), _Resp(200, {"ok": True})])
    assert c.get_json("https://x/3", "three") == {"ok": True}
    assert c.calls == 3


def test_transform_is_what_gets_cached(tmp_path):
    c = _client(tmp_path, [_Resp(200, {"big": list(range(100)), "keep": 7})])
    assert c.get_json("https://x/4", "four", transform=lambda p: p["keep"]) == 7
    assert _client(tmp_path, []).get_json("https://x/4", "four") == 7


def test_rate_limiter_spacing(monkeypatch):
    clock = {"t": 0.0}
    sleeps = []
    monkeypatch.setattr("value_screener.sec.time.monotonic", lambda: clock["t"])
    monkeypatch.setattr("value_screener.sec.time.sleep", lambda s: sleeps.append(s))
    rl = RateLimiter(8)
    for _ in range(3):
        rl.wait()
    assert sleeps == pytest.approx([0.125, 0.25])


def test_local_settings_and_env_override(tmp_path, monkeypatch):
    from value_screener.config import load_settings

    (tmp_path / "settings.yaml").write_text('sec_user_agent: "Your Name <email>"\nmin_peers: 8\n')
    (tmp_path / "settings.local.yaml").write_text('sec_user_agent: "Me me@example.com"\n')
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    assert load_settings(tmp_path / "settings.yaml").sec_user_agent == "Me me@example.com"
    monkeypatch.setenv("SEC_USER_AGENT", "Env env@example.com")
    assert load_settings(tmp_path / "settings.yaml").sec_user_agent == "Env env@example.com"
