"""Offline tests for client session persistence, cookie lookup, retry
classification, login-wall detection and .env parsing. No network."""
import json
import os
import stat
import tempfile
from pathlib import Path

import requests

from _runner import run
from paypay_sec import client as client_mod
from paypay_sec import config
from paypay_sec.client import PayPayClient, SessionExpired, _is_transient
from paypay_sec.config import Settings

_SETTINGS = Settings(member_id="m", password="p", cookie="SMS_AUTH_STRING=x")


def _client(session_file: Path, use_cache=True) -> PayPayClient:
    return PayPayClient(_SETTINGS, session_file=session_file, use_cache=use_cache, cache_ttl=0)


def test_session_roundtrip_keeps_domains():
    d = Path(tempfile.mkdtemp())
    sf = d / "state" / "session.json"
    c = _client(sf, use_cache=False)
    c._session.cookies.set("laravel_session", "www-val", domain="www.paypay-sec.co.jp")
    c._session.cookies.set("laravel_session", "bff-val", domain=".paypay-sec.co.jp")
    c.token = "tok"
    c._save_session()

    assert stat.S_IMODE(sf.stat().st_mode) == 0o600, oct(sf.stat().st_mode)
    assert stat.S_IMODE(sf.parent.stat().st_mode) == 0o700, oct(sf.parent.stat().st_mode)
    assert not [p for p in sf.parent.iterdir() if p.name.endswith(".tmp")], "temp file left behind"
    saved = json.loads(sf.read_text())
    assert isinstance(saved["cookies"], list)

    c2 = _client(sf)
    assert c2._from_cache and c2.token == "tok"
    got = {(ck.name, ck.domain): ck.value for ck in c2._session.cookies}
    assert got[("laravel_session", "www.paypay-sec.co.jp")] == "www-val"
    assert got[("laravel_session", ".paypay-sec.co.jp")] == "bff-val"


def test_session_load_old_dict_format():
    d = Path(tempfile.mkdtemp())
    sf = d / "session.json"
    sf.write_text(json.dumps({"cookies": {"fuelrid": "abc", "CLIENT_SEQ_NO": "1"}, "token": None}))
    c = _client(sf)
    assert c._from_cache
    ck = [x for x in c._session.cookies if x.name == "fuelrid"]
    assert len(ck) == 1 and ck[0].domain == client_mod._DOMAIN and ck[0].value == "abc"


def test_session_load_empty_is_not_cached():
    d = Path(tempfile.mkdtemp())
    sf = d / "session.json"
    sf.write_text(json.dumps({"cookies": [], "token": None}))
    assert not _client(sf)._from_cache


def test_cookie_duplicate_domains_no_conflict():
    c = _client(Path(tempfile.mkdtemp()) / "s.json", use_cache=False)
    c._session.cookies.set("fuel_csrf_token", "parent", domain=".paypay-sec.co.jp")
    c._session.cookies.set("fuel_csrf_token", "www", domain="www.paypay-sec.co.jp")
    try:
        c._session.cookies.get("fuel_csrf_token")
        raised = False
    except requests.cookies.CookieConflictError:
        raised = True
    assert raised, "precondition: jar.get should conflict on duplicate names"
    assert c._cookie("fuel_csrf_token") == "www"
    assert c._cookie("missing") is None
    assert c._cookie("missing", "") == ""


def _http_error(status: int) -> requests.HTTPError:
    r = requests.Response()
    r.status_code = status
    return requests.HTTPError(f"HTTP {status}", response=r)


def test_transient_classification():
    assert _is_transient(requests.ConnectionError("reset"))
    assert _is_transient(requests.Timeout("slow"))
    assert _is_transient(requests.exceptions.ReadTimeout("slow"))
    assert _is_transient(_http_error(503))
    assert not _is_transient(_http_error(404))
    assert not _is_transient(_http_error(403))
    assert not _is_transient(requests.HTTPError("no response"))
    assert not _is_transient(requests.exceptions.InvalidURL("bad"))
    if hasattr(requests.exceptions, "JSONDecodeError"):
        assert not _is_transient(requests.exceptions.JSONDecodeError("x", "doc", 0))


def test_run_resilient_does_not_retry_4xx():
    c = _client(Path(tempfile.mkdtemp()) / "s.json", use_cache=False)
    calls = []

    def once():
        calls.append(1)
        raise _http_error(400)

    try:
        c._run_resilient(once)
        assert False, "expected HTTPError"
    except requests.HTTPError:
        pass
    assert len(calls) == 1


def test_run_resilient_retries_5xx():
    c = _client(Path(tempfile.mkdtemp()) / "s.json", use_cache=False)
    calls = []
    orig_sleep = client_mod.time.sleep
    client_mod.time.sleep = lambda _s: None
    try:
        def once():
            calls.append(1)
            if len(calls) < 3:
                raise _http_error(502)
            return "ok"
        assert c._run_resilient(once) == "ok"
    finally:
        client_mod.time.sleep = orig_sleep
    assert len(calls) == 3


class _Resp:
    def __init__(self, url, status=200, text="<html/>"):
        self.url, self.status_code, self.text = url, status, text

    def raise_for_status(self):
        pass


class _GetSession:
    def __init__(self, url):
        self.url = url
        self.cookies = requests.cookies.RequestsCookieJar()

    def get(self, *_a, **_k):
        return _Resp(self.url)


def _get_page(final_url):
    c = _client(Path(tempfile.mkdtemp()) / "s.json", use_cache=False)
    c._session = _GetSession(final_url)
    return c.get_page("/trade/portfolio/usa")


def test_login_wall_detection():
    for url in ("https://www.paypay-sec.co.jp/login/",
                "https://www.paypay-sec.co.jp/login/?redirect=/trade/portfolio/usa",
                "https://www.paypay-sec.co.jp/LOGIN"):
        try:
            _get_page(url)
            assert False, f"expected SessionExpired for {url}"
        except SessionExpired:
            pass
    # an authenticated page whose query mentions /login is NOT the login wall
    assert _get_page("https://www.paypay-sec.co.jp/trade/portfolio/usa?from=/login") == "<html/>"
    assert _get_page("https://www.paypay-sec.co.jp/trade/history/usa") == "<html/>"


def test_dotenv_unquote():
    u = config._unquote
    assert u('"abc"') == "abc"
    assert u("'abc'") == "abc"
    assert u('"a\'b"') == "a'b"
    assert u('abc"') == 'abc"'            # unmatched → untouched
    assert u('"abc\'') == '"abc\''
    assert u('""') == ""
    assert u('"') == '"'
    assert u("'it''s'") == "it''s"        # only ONE surrounding pair stripped


def test_from_account_file_quotes_and_mode_warning():
    import contextlib
    import io
    d = Path(tempfile.mkdtemp())
    env = d / ".env"
    env.write_text('PAYPAY_MEMBER_ID="m1"\nPAYPAY_PASSWORD=\'pa"ss\'\nPAYPAY_COOKIE="a=1; b=2"\n')
    os.chmod(env, 0o644)
    orig = config.env_file_for
    config.env_file_for = lambda _a: env
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            s = Settings.from_account_file("default")
    finally:
        config.env_file_for = orig
    assert s.member_id == "m1" and s.password == 'pa"ss' and s.cookie == "a=1; b=2"
    assert "chmod 600" in err.getvalue()


if __name__ == "__main__":
    raise SystemExit(run(globals()))
