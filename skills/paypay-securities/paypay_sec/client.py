"""Authenticated HTTP client for the PayPay証券 web frontend (read-only).

Login flow (verified):
  POST /login.json with MEMBER_ID/PASSWORD/UUID/REFERRER and the trusted device
  cookie (..._SMS_AUTH_STRING) -> JSON {STATUS, TOKEN, IF_NEED_SMS_FLG}. The
  response sets session cookies (CLIENT_SEQ_NO, fuelrid); afterwards the same
  session can GET the SSR pages under /trade/*.

Session reuse:
  The session cookies are cached to ~/.paypay-sec/session.json after login and
  reused on later runs, so /login.json is only hit when the session has actually
  expired (detected by a redirect to /login/). This avoids hammering the login
  endpoint on every command.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import requests

from .config import Settings

BASE = "https://www.paypay-sec.co.jp"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_DOMAIN = "www.paypay-sec.co.jp"
SESSION_FILE = Path.home() / ".paypay-sec" / "session.json"

# user-facing market name -> path slug used by /trade/portfolio/<slug> etc.
MARKET_ALIASES = {
    "usa": "usa", "us": "usa", "米国": "usa", "米国株": "usa", "america": "usa",
    "japan": "japan", "jp": "japan", "jpn": "japan", "日本": "japan", "日本株": "japan",
}


def normalize_market(market: str) -> str:
    return MARKET_ALIASES.get(market.strip().lower(), market.strip().lower())


class LoginError(RuntimeError):
    """Login did not succeed (bad creds, expired device token, SMS required…)."""


class SessionExpired(RuntimeError):
    """A page fetch was bounced to the login wall — the session needs refresh."""


class PayPayClient:
    def __init__(self, settings: Settings | None = None, *,
                 session_file: Path = SESSION_FILE, use_cache: bool = True,
                 cache_ttl: int | None = None):
        self.settings = settings or Settings.from_env()
        self.token: str | None = None
        self.session_file = session_file
        self._from_cache = False
        # response cache: avoids re-hitting (and throttling) the API when several
        # commands / analyses want the same data within a short window.
        self._cache_dir = session_file.parent / "cache"
        self._cache_ttl = (cache_ttl if cache_ttl is not None
                           else int(os.environ.get("PAYPAY_CACHE_TTL", "120")))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _UA, "Accept-Language": "ja,en;q=0.8"})
        self._seed_cookies(self.settings.cookie)   # device token, always
        if use_cache:
            self._load_session()                    # reuse a prior session if present

    def _cached(self, key: str, producer):
        """Return a fresh-enough cached response for `key`, else call producer()
        and cache its (JSON-serializable) result. ttl<=0 disables caching."""
        if self._cache_ttl <= 0:
            return producer()
        fp = self._cache_dir / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")
        try:
            blob = json.loads(fp.read_text(encoding="utf-8"))
            if time.time() - blob["ts"] <= self._cache_ttl:
                return blob["data"]
        except (OSError, ValueError, KeyError):
            pass
        data = producer()
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            fp.write_text(json.dumps({"ts": int(time.time()), "data": data}, ensure_ascii=False),
                          encoding="utf-8")
        except OSError:
            pass
        return data

    def clear_cache(self) -> int:
        n = 0
        try:
            for f in self._cache_dir.glob("*.json"):
                f.unlink()
                n += 1
        except OSError:
            pass
        return n

    # ---- cookies / session persistence ----
    def _seed_cookies(self, cookie_str: str) -> None:
        for part in cookie_str.split(";"):
            part = part.strip()
            if part and "=" in part:
                k, _, v = part.partition("=")
                self._session.cookies.set(k.strip(), v.strip(), domain=_DOMAIN)

    def _load_session(self) -> None:
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for name, value in (data.get("cookies") or {}).items():
            self._session.cookies.set(name, value, domain=_DOMAIN)
        self.token = data.get("token")
        self._from_cache = bool(data.get("cookies"))

    def _save_session(self) -> None:
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookies": {c.name: c.value for c in self._session.cookies},
            "token": self.token,
            "ts": int(time.time()),
        }
        # 0600 — the session cookies are credential-equivalent
        fd = os.open(self.session_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def clear_session(self) -> bool:
        self.token = None
        self._from_cache = False
        try:
            self.session_file.unlink()
            return True
        except OSError:
            return False

    # ---- auth ----
    def login(self) -> dict:
        """Force a fresh /login.json. Returns the parsed payload; saves session."""
        r = self._session.post(
            f"{BASE}/login.json",
            data={
                "MEMBER_ID": self.settings.member_id,
                "PASSWORD": self.settings.password,
                "UUID": self.settings.uuid,
                "REFERRER": "/trade",
            },
            headers={
                "Origin": BASE,
                "Referer": f"{BASE}/login/",
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=30,
        )
        if r.status_code != 200:
            raise LoginError(f"login.json returned HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError as e:
            raise LoginError(f"login.json did not return JSON: {e}") from e
        if not payload.get("STATUS"):
            raise LoginError(f"login rejected: {payload.get('MESSAGE_ARRAY') or 'unknown error'}")
        if payload.get("IF_NEED_SMS_FLG"):
            raise LoginError(
                "server demands SMS verification (IF_NEED_SMS_FLG=1) — this "
                "device/cookie is not trusted; refresh PAYPAY_COOKIE from a "
                "logged-in browser."
            )
        self.token = payload.get("TOKEN")
        self._from_cache = True
        self._save_session()
        return payload

    def ensure_session(self) -> None:
        """Log in only if we don't already have a (cached) session."""
        if not self._from_cache:
            self.login()

    # ---- pages ----
    def get_page(self, path: str, _tries: int = 3) -> str:
        def fetch_once() -> str:
            last_exc = None
            for attempt in range(_tries):
                try:
                    r = self._session.get(
                        f"{BASE}{path}",
                        headers={
                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                            "Referer": f"{BASE}/trade?country=usa",
                            "Upgrade-Insecure-Requests": "1",
                        },
                        timeout=30,
                        allow_redirects=True,
                    )
                except requests.RequestException as e:           # transient network/timeout
                    last_exc = e
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if "/login" in r.url.lower() and "/trade" not in r.url.lower():
                    raise SessionExpired(path)
                if r.status_code in (502, 503, 504):              # transient server hiccup
                    last_exc = requests.HTTPError(f"HTTP {r.status_code}", response=r)
                    time.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r.text
            raise last_exc

        return self._cached(f"GET {path}", fetch_once)

    def fetch(self, path: str) -> str:
        """Fetch a page, reusing the cached session and re-logging in at most
        once if the session has expired."""
        self.ensure_session()
        try:
            return self.get_page(path)
        except SessionExpired:
            self.login()
            return self.get_page(path)

    # ---- convenience fetchers (market: 'usa' | 'japan' | aliases 'jp'/'us'/…) ----
    def summary_html(self, market: str = "usa") -> str:
        return self.fetch(f"/trade?country={normalize_market(market)}")

    def portfolio_html(self, market: str = "usa") -> str:
        return self.fetch(f"/trade/portfolio/{normalize_market(market)}")

    def brands_html(self, market: str = "usa") -> str:
        """Per-holding detail table (quantity / cost / per-position P&L)."""
        return self.fetch(f"/trade/portfolio/brands/{normalize_market(market)}")

    def history_html(self, market: str = "usa") -> str:
        return self.fetch(f"/trade/history/{normalize_market(market)}")

    def moneyschedule_html(self) -> str:
        return self.fetch("/trade/client/moneyschedule")

    _SETTLEMENT_PAGE_SIZE = 20

    def settlement_records(self, max_pages: int = 3) -> list:
        """Account transaction ledger (買付/売却/入金/手数料 …) with running
        CASH_BALANCE, newest first.

        NOTE: PAGE_NUM is a RECORD OFFSET, not a page index (PAGE_NUM=1 overlaps
        PAGE_NUM=0 by 19/20). So we step by the page size and de-dup by SEQ_NO."""
        self.ensure_session()
        out: list = []
        seen: set = set()
        for i in range(max_pages):
            offset = i * self._SETTLEMENT_PAGE_SIZE

            def do(off=offset) -> dict:
                r = self._session.get(
                    f"{BASE}/trade/history/ajax_settlement.json?PAGE_NUM={off}",
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": f"{BASE}/trade/history/settlements/usa"},
                    timeout=30, allow_redirects=False)
                if r.status_code in (301, 302):
                    raise SessionExpired("ajax_settlement")
                r.raise_for_status()
                return r.json()

            j = self._cached(f"SETTLE off={offset}", lambda: self._run_resilient(do))
            recs = j.get("CO_TRADE_HIST") or []
            fresh = [x for x in recs if x.get("SEQ_NO") not in seen]
            for x in fresh:
                seen.add(x.get("SEQ_NO"))
            out.extend(fresh)
            if not j.get("NEXT_FLG") or not fresh:
                break
        return out

    # ---- 投信 (mutual funds): Vue SPA backed by a JSON API ----
    # The SPA posts these exact FormData fields; an empty body makes the
    # server hang, so they are required.
    _INVEST_BODY = {"APP_VERSION": "", "DEVICE_TOKEN": "device_token", "OS": "pc", "APP_ID": "3"}

    def _run_resilient(self, once, tries: int = 3):
        """Run a request thunk with one transparent re-login on SessionExpired
        and short-backoff retries on transient network / 5xx errors."""
        last = None
        relogged = False
        for _ in range(tries):
            try:
                return once()
            except SessionExpired:
                if relogged:
                    raise
                self.login()
                relogged = True
            except requests.RequestException as e:   # timeout / 5xx (HTTPError) / conn reset
                last = e
                time.sleep(1.2)
        raise last if last else SessionExpired("retries exhausted")

    def _post_invest(self, path: str) -> dict:
        self.ensure_session()
        body = dict(self._INVEST_BODY, UUID=self.settings.uuid or "uuid_pc")

        def do() -> dict:
            r = self._session.post(
                f"{BASE}{path}", data=body,
                headers={"X-Requested-With": "XMLHttpRequest", "Origin": BASE,
                         "Referer": f"{BASE}/investment_trust/"},
                timeout=40, allow_redirects=False)
            if r.status_code in (301, 302) or r.status_code == 200 and not r.text.strip().startswith("{"):
                raise SessionExpired(path)
            r.raise_for_status()
            j = r.json()
            if j.get("LOGIN_STATUS") not in (0, None):   # 0 = authenticated
                raise SessionExpired(path)
            return j

        return self._cached(f"POST {path}", lambda: self._run_resilient(do))

    def invtrust_top(self) -> dict:
        """投信 portfolio summary + per-fund holdings (JSON)."""
        return self._post_invest("/v2/invest/brand/pc_invest_top")

    def invtrust_brands(self, force: bool = False) -> dict:
        """Map of {brand_id(str): fund name}. Cached to disk (names rarely change).
        The master list endpoint only fills in reliably after pc_invest_info, so
        we call them in the SPA's order."""
        brands_file = self.session_file.parent / "invtrust_brands.json"
        if not force:
            try:
                cached = json.loads(brands_file.read_text(encoding="utf-8"))
                if cached:
                    return cached
            except (OSError, ValueError):
                pass
        self._post_invest("/v2/invest/brand/pc_invest_info")   # establishes state
        init = self._post_invest("/v2/invest/brand/pc_invest_init")
        arr = init.get("INVEST_BRAND_ARRAY") or {}
        rows = arr.values() if isinstance(arr, dict) else arr
        names = {str(h.get("BRAND_ID")): h.get("BRAND_NM")
                 for h in rows if h.get("BRAND_ID") is not None}
        if names:
            try:
                brands_file.parent.mkdir(parents=True, exist_ok=True)
                brands_file.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
        return names
