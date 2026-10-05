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
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import requests

from .config import HOME, DEFAULT_ACCOUNT, Settings, write_private

BASE = "https://www.paypay-sec.co.jp"
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")
_DOMAIN = "www.paypay-sec.co.jp"
SESSION_FILE = HOME / "session.json"   # default account (back-compat)
# Bump when cached response SHAPE changes (or to force-invalidate stale caches on a
# skill update). Mixed into every cache key alongside the account name.
CACHE_VERSION = "2"
_BRAND_HREF_RE = re.compile(r"/trade/brand/(\d+)/0")
_BRAND_LOGO_RE = re.compile(r"304x304_([a-z0-9._-]+)\.(?:png|jpe?g)", re.I)


def state_dir(account: str | None) -> Path:
    """Per-account dir holding session.json + cache/. The default account uses
    ~/.paypay-sec/ directly (back-compat); named accounts get a subdirectory so
    their sessions/caches never collide."""
    return HOME if account in (None, DEFAULT_ACCOUNT) else HOME / account

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


class OrderOutcomeUnknown(RuntimeError):
    """The order submit was sent but its result could not be read (timeout,
    dropped connection, 5xx, unparsable body). The order MAY have been placed —
    check `paypay orders` before retrying; never resend automatically."""


def _is_transient(e: Exception) -> bool:
    """Worth retrying: network blips / timeouts / 5xx. NOT 4xx or a bad JSON body
    (requests' JSONDecodeError is also a RequestException) — those won't heal."""
    if isinstance(e, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(e, requests.HTTPError):
        resp = getattr(e, "response", None)
        return resp is not None and resp.status_code >= 500
    return False


class PayPayClient:
    def __init__(self, settings: Settings | None = None, *,
                 session_file: Path | None = None, use_cache: bool = True,
                 cache_ttl: int | None = None):
        self.settings = settings or Settings.from_env()
        self.token: str | None = None
        # per-account state dir (default account → ~/.paypay-sec/, named → subdir)
        sd = state_dir(self.settings.account)
        self.session_file = session_file or (sd / "session.json")
        self._from_cache = False
        # response cache: avoids re-hitting (and throttling) the API when several
        # commands / analyses want the same data within a short window.
        self._cache_dir = self.session_file.parent / "cache"
        self._cache_ttl = (cache_ttl if cache_ttl is not None
                           else int(os.environ.get("PAYPAY_CACHE_TTL", "120")))
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _UA, "Accept-Language": "ja,en;q=0.8"})
        self._seed_cookies(self.settings.cookie)   # device token, always
        if use_cache:
            self._load_session()                    # reuse a prior session if present

    def _cached(self, key: str, producer, cache_if=None):
        """Return a fresh-enough cached response for `key`, else call producer()
        and cache its (JSON-serializable) result. ttl<=0 disables caching.
        `cache_if(result)` (optional) gates whether the result is worth caching —
        used to avoid caching empty/throttled responses."""
        if self._cache_ttl <= 0:
            return producer()
        # Namespace the cache key by CACHE_VERSION + account, so (a) a skill update
        # bumping CACHE_VERSION invalidates stale-shaped entries, and (b) one account's
        # cache can never satisfy another's lookup even if dirs were ever shared.
        full_key = f"{CACHE_VERSION}|{self.settings.account}|{key}"
        fp = self._cache_dir / (hashlib.sha1(full_key.encode("utf-8")).hexdigest() + ".json")
        try:
            blob = json.loads(fp.read_text(encoding="utf-8"))
            if time.time() - blob["ts"] <= self._cache_ttl:
                return blob["data"]
        except (OSError, ValueError, KeyError):
            pass
        data = producer()
        if cache_if is None or cache_if(data):
            try:
                write_private(fp, json.dumps({"ts": int(time.time()), "data": data},
                                             ensure_ascii=False))
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

    def _cookie(self, name: str, default: str | None = None) -> str | None:
        """Cookie value by name. Unlike jar.get() this never raises
        CookieConflictError when the same name exists on several domains/paths —
        the most specific (longest domain, then path) wins, last one on a tie."""
        best, best_key = default, None
        for c in self._session.cookies:
            if c.name != name:
                continue
            key = (len((c.domain or "").lstrip(".")), len(c.path or ""))
            if best_key is None or key >= best_key:
                best, best_key = c.value, key
        return best

    def _load_session(self) -> None:
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cookies = data.get("cookies") or []
        if isinstance(cookies, dict):          # old format {name: value} → www
            cookies = [{"name": k, "value": v} for k, v in cookies.items()]
        for c in cookies:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            self._session.cookies.set(c["name"], c.get("value"),
                                      domain=c.get("domain") or _DOMAIN,
                                      path=c.get("path") or "/")
        self.token = data.get("token")
        self._from_cache = bool(cookies)

    def _save_session(self) -> None:
        payload = {
            # keep domain/path — the passkey flow sets same-named cookies on the
            # BFF host and www; flattening to {name: value} collapsed them onto www
            "cookies": [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
                        for c in self._session.cookies],
            "token": self.token,
            "ts": int(time.time()),
        }
        # 0600 + atomic replace — the session cookies are credential-equivalent
        write_private(self.session_file, json.dumps(payload))

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
        # If a passkey is cached, go straight to the headless FIDO2 flow — skip the
        # password POST entirely. (The password /login.json sets an UNAUTHORIZED
        # laravel_session that pollutes the session so the passkey callback can't
        # mint a clean one — and the password path is pointless on a passkey-locked
        # account anyway.)
        from . import passkey_login as pk
        blob = pk.load_passkey(self.settings.account)
        if blob:
            return self._passkey_login(blob=blob)
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
        if payload.get("PASSKEY_REQUIRED"):
            # STATUS=True here is a TRAP: login.json accepts the password but the
            # account is policy-locked to passkey (FIDO2) auth, so the laravel_session
            # it mints is unauthorized — every /trade fetch then bounces to /login.
            # Drive the headless passkey flow (key from the Keychain, signed locally)
            # to mint a real authorized session; fall back to a loud error if no key.
            return self._passkey_login(passkey_message=payload.get("MESSAGE"))
        self.token = payload.get("TOKEN")
        self._from_cache = True
        self._save_session()
        return payload

    def _passkey_login(self, *, passkey_message: str | None = None,
                       blob: dict | None = None) -> dict:
        """Headless FIDO2 passkey login (private key from the Keychain, signed
        locally). Populates self._session with the www laravel_session and caches
        it. Raises LoginError if no passkey is set up or the server rejects us."""
        from . import passkey_login as pk
        try:
            result = pk.passkey_login(self._session, self.settings.account,
                                      self.session_file.parent, blob=blob)
        except pk.PasskeyLoginError as e:
            raise LoginError(
                f"passkey login failed: {e}"
                + (f" (server: {passkey_message})" if passkey_message else "")
            ) from e
        self.token = None
        self._from_cache = True
        self._save_session()
        return {"STATUS": True, "PASSKEY": True, "result": result}

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
                if urlparse(r.url).path.lower().startswith("/login"):
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

    def _paged_ledger(self, url: str, referer: str, label: str, cache_tag: str,
                      max_pages: int) -> list:
        """Shared pager for the CO_TRADE_HIST ledgers. `url` takes {off}.

        NOTE: PAGE_NUM is a RECORD OFFSET, not a page index (PAGE_NUM=1 overlaps
        PAGE_NUM=0 by 19/20). So we step by the page size and de-dup by SEQ_NO."""
        self.ensure_session()
        out: list = []
        seen: set = set()
        for i in range(max_pages):
            offset = i * self._SETTLEMENT_PAGE_SIZE

            def do(off=offset) -> dict:
                r = self._session.get(
                    BASE + url.format(off=off),
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": f"{BASE}{referer}"},
                    timeout=30, allow_redirects=False)
                if r.status_code in (301, 302):
                    raise SessionExpired(label)
                r.raise_for_status()
                return r.json()

            def fetch_page(off=offset) -> dict:
                # an empty page 0 is almost always throttling, not end-of-data —
                # retry with backoff before giving up.
                j = self._run_resilient(do)
                tries = 1
                while off == 0 and not (j.get("CO_TRADE_HIST") or []) and tries < 4:
                    time.sleep(1.5 * tries)
                    tries += 1
                    j = self._run_resilient(do)
                return j

            # never cache an empty page (any offset): a throttled empty response
            # would otherwise silently truncate the ledger for the whole TTL
            j = self._cached(f"{cache_tag} off={offset}", fetch_page,
                             cache_if=lambda r: bool(r.get("CO_TRADE_HIST")))
            recs = j.get("CO_TRADE_HIST") or []
            fresh = [x for x in recs if x.get("SEQ_NO") not in seen]
            for x in fresh:
                seen.add(x.get("SEQ_NO"))
            out.extend(fresh)
            if not j.get("NEXT_FLG") or not fresh:
                break
        return out

    def settlement_records(self, max_pages: int = 3) -> list:
        """Account transaction ledger (買付/売却/入金/手数料 …) with running
        CASH_BALANCE, newest first. Paged by record offset (see _paged_ledger)."""
        return self._paged_ledger("/trade/history/ajax_settlement.json?PAGE_NUM={off}",
                                  "/trade/history/settlements/usa", "ajax_settlement",
                                  "SETTLE", max_pages)

    def invtrust_settlement_records(self, max_pages: int = 3) -> list:
        """投信 (mutual-fund) transaction ledger via the SPA's settlements feed
        (MARKET_ID=99) — a SEPARATE market view from the 証券 ajax ledger, holding
        the 買付/売却/入金/譲渡益税/送金手数料 rows for 投資信託. Same CO_TRADE_HIST
        shape AND the same gotcha: PAGE_NUM is a RECORD OFFSET (PAGE_NUM=n returns
        records n..n+19), not a page index — so we step by the page size and de-dup
        by SEQ_NO, exactly like settlement_records. MINI_CLIENT_SEQ_NO is NOT
        required. The 入金/送金手数料 rows are account-wide (identical to the 証券
        ledger); only 買付/売却/譲渡益税 are 投信-specific."""
        return self._paged_ledger("/v0/history/settlements.json?MARKET_ID=99&PAGE_NUM={off}&OS=pc",
                                  "/investment_trust/", "invtrust_settlements",
                                  "INVSETTLE", max_pages)

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
            except requests.RequestException as e:
                if not _is_transient(e):             # 4xx / bad JSON: retrying won't help
                    raise
                last = e                             # timeout / 5xx / conn reset
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
                write_private(brands_file, json.dumps(names, ensure_ascii=False))
            except OSError:
                pass
        return names

    # ---- WRITE methods (orders). NO _run_resilient, NO _cached. Single-shot. ----
    # Confirm is a server-side preview only. It does not include TRADE_PASSWORD
    # and it must not call ajax_buy_complete / ajax_sell_complete.
    def _order_json_get(self, path: str, referer: str) -> dict:
        self.ensure_session()
        r = self._session.get(
            f"{BASE}{path}",
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": f"{BASE}{referer}"},
            timeout=30,
            allow_redirects=False,
        )
        if r.status_code in (301, 302):
            raise SessionExpired(path)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(f"{path} did not return JSON") from e

    def _order_json_post(self, path: str, data: dict, referer: str) -> dict:
        self.ensure_session()
        r = self._session.post(
            f"{BASE}{path}",
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Origin": BASE,
                "Referer": f"{BASE}{referer}",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            timeout=30,
            allow_redirects=False,
        )
        if r.status_code in (301, 302):
            raise SessionExpired(path)
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise RuntimeError(f"{path} did not return JSON") from e

    def _brand_id_for_symbol(self, symbol: str, market: str = "usa") -> str:
        """Resolve a US ticker to PayPay's internal BRAND_ID.

        The summary page exposes `/trade/brand/<id>/0` links. For US stocks the
        logo asset filename also carries the ticker, e.g. `304x304_aapl.png`.
        """
        sym = (symbol or "").strip().upper()
        if not sym:
            raise RuntimeError("symbol is required")
        if sym.isdigit():
            return sym
        html = self.summary_html(market)
        soup = BeautifulSoup(html, "lxml")
        for card in soup.select("div.mypage_brand_icon"):
            a = card.find("a")
            href = a.get("href", "") if a else ""
            id_match = _BRAND_HREF_RE.search(href)
            logo_match = _BRAND_LOGO_RE.search(str(card))
            if id_match and logo_match and logo_match.group(1).upper() == sym:
                return id_match.group(1)
        # the cards no longer always carry the logo <img> — fall back to each linked
        # brand's ajax_detail BRAND_CD (the ticker; verified live 2026-10-05)
        ids = dict.fromkeys(m.group(1) for m in _BRAND_HREF_RE.finditer(html))
        for bid in ids:
            try:
                j = self._order_json_get(f"/trade/brand/ajax_detail/{bid}/0",
                                         referer=f"/trade/brand/buy/{bid}")
            except (RuntimeError, requests.RequestException):
                continue
            b = j.get("brand") or (j.get("DATA") or {}).get("brand") or {}
            if str(b.get("BRAND_CD") or "").upper() == sym:
                return bid
        raise RuntimeError(f"could not resolve ticker {sym!r} to a PayPay BRAND_ID "
                           "(pass the numeric BRAND_ID from /trade/brand/<id>/0 instead)")

    @staticmethod
    def _order_amount_jpy(req, brand: dict) -> int:
        # The web flow is 金額指定 only (amountTyp=0): the request carries the yen amount.
        amount = getattr(req, "amount_jpy", None)
        if amount is None or int(amount) <= 0:
            raise RuntimeError("confirm requires a positive amount_jpy (金額指定)")
        return int(amount)

    @staticmethod
    def _message(resp: dict) -> str:
        msgs = resp.get("MESSAGE_ARRAY") or resp.get("MESSAGE")
        if isinstance(msgs, list):
            return "; ".join(str(x) for x in msgs)
        return str(msgs or "unknown error")

    @staticmethod
    def _status_ok(resp) -> bool:
        # strict: a truthy "0" / "false" / {} must never read as success
        return isinstance(resp, dict) and resp.get("STATUS") in (True, 1, "1")

    def _csrf(self) -> str:
        return self._cookie("fuel_csrf_token") or ""

    # The web order flow (FuelPHP, www.paypay-sec.co.jp — NOT cert-pinned, NO passkey),
    # VERIFIED live 2026-06-04 / 2026-06-08:
    #   CSRF seed: the fuel_csrf_token cookie is ONLY set by a full-page GET of
    #     /trade/brand/(buy|sell)/<id> — never by the ajax endpoints. Without it the
    #     submit goes out with an empty CSRF_TOKEN and gets a generic エラーが発生しました.
    #   confirm/見積 (preview only): POST /trade/brand/ajax_(buy|sell)_popup.json
    #     preorder=0 (market open) → {STATUS, HTML}: hidden inputs incl. ORDER_CONFIRM_NO.
    #     Market closed / pre-market: the immediate order is rejected (買付可能金額を超える…
    #     regardless of cash) → preorder=1 (成行予約, fills next session) →
    #     {STATUS, DATA:{brand, PREORDERABLE:1, PREORDER_EXECUTE_DATE}}, ORDER_CONFIRM_NO
    #     empty (the server assigns it at complete).
    #   submit (two-step, as trade.js buy()):
    #     1. POST ajax_(buy|sell)_complete with the #buy_form fields — THE order.
    #     2. POST ajax_(buy|sell)_complete_popup.json echoing step 1's response
    #        (+BRAND_ID/PREORDER/CSRF_TOKEN) — renders the receipt only.
    # #buy_form fields: BRAND_ID, AMOUNT_TYP, ORDER_CONFIRM_NO, ORDER_AMOUNT, ORDER_QTY,
    # ORDER_PRICE, ORDER_EXCHANGE_RATE, PREORDER, CSRF_TOKEN,
    # IS_NON_INSIDER_TRADING_CONFIRMED, PLAN_TYPE, ACCOUNT_TYPE, IGNORE_NEXT_TIME,
    # TRADE_PASSWORD (plaintext). open_orders columns were finalized against /trade/preorder/.
    @staticmethod
    def _hidden_fields(html: str) -> dict:
        soup = BeautifulSoup(html or "", "lxml")
        out = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if name and inp.get("value") is not None:
                out[name] = inp.get("value")
        return out

    @classmethod
    def _parse_popup(cls, popup: dict) -> dict:
        """Normalize both popup shapes: immediate {HTML} and preorder {DATA}."""
        fields = cls._hidden_fields(popup.get("HTML") or popup.get("html") or "")
        data = popup.get("DATA") if isinstance(popup.get("DATA"), dict) else {}
        brand = data.get("brand") if isinstance(data.get("brand"), dict) else {}
        return {"fields": fields, "data": data, "brand": brand,
                "confirm_no": str(fields.get("ORDER_CONFIRM_NO") or ""),
                "execute_date": data.get("PREORDER_EXECUTE_DATE")}

    @staticmethod
    def _is_preorder_popup(popup: dict) -> bool:
        """A preorder=1 popup answers {DATA:{PREORDERABLE:1, PREORDER_EXECUTE_DATE}}.
        (ajax_detail's own PREORDERABLE reads 0 while closed — don't gate on it.)"""
        data = popup.get("DATA") if isinstance(popup.get("DATA"), dict) else {}
        return str(data.get("PREORDERABLE")) == "1" or bool(data.get("PREORDER_EXECUTE_DATE"))

    def _seed_csrf(self, ref: str) -> None:
        """Full-page GET of the order page — the only thing that Set-Cookies
        fuel_csrf_token. Direct and uncached (a cached page sets no cookie)."""
        self.ensure_session()
        r = self._session.get(
            f"{BASE}{ref}",
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                     "Referer": f"{BASE}/trade?country=usa"},
            timeout=30,
            allow_redirects=False,
        )
        if r.status_code in (301, 302):
            raise SessionExpired(ref)
        r.raise_for_status()

    def _confirm(self, req, side: str) -> dict:
        market = normalize_market(getattr(req, "market", "usa"))
        if market != "usa":
            raise NotImplementedError("order confirm currently supports US stocks only")
        brand_id = self._brand_id_for_symbol(req.symbol, market)
        account_type = int(getattr(req, "account_type", None) or 2)
        ref = f"/trade/brand/{side}/{brand_id}"
        self._seed_csrf(ref)
        live = self._order_json_get(f"/trade/brand/ajax_detail/{brand_id}/0", referer=ref)
        brand = live.get("brand") or (live.get("DATA") or {}).get("brand") or {}
        amount_jpy = self._order_amount_jpy(req, brand)

        def popup_for(preorder: int) -> dict:
            return self._order_json_post(
                f"/trade/brand/ajax_{side}_popup.json",
                data={"amountTyp": 0, "val": amount_jpy, "BRAND_ID": brand_id, "buyAllChange": 0,
                      "globalOrderAmountLower": brand.get("ORDER_AMOUNT_LOWER") or 1,
                      "preorder": preorder, "PLAN_TYPE": 0, "KEY": "", "PAYPAY_FLG": 0,
                      "ACCOUNT_TYPE": account_type},
                referer=ref)

        preorder = 0
        popup = popup_for(0)
        if not self._status_ok(popup):
            msg = self._message(popup) if isinstance(popup, dict) else "unexpected response"
            # market closed / pre-market: the immediate order is refused regardless of
            # cash — try as 成行予約 (executes at the next session's open quote). Still a
            # preview; the confirm prompt labels it 予約注文 + execute date.
            pre = popup_for(1)
            if not (self._status_ok(pre) and self._is_preorder_popup(pre)):
                pmsg = self._message(pre) if isinstance(pre, dict) else "unexpected response"
                raise RuntimeError(f"order confirm rejected: {msg} "
                                   f"(予約注文 also refused: {pmsg})")
            preorder, popup = 1, pre
        p = self._parse_popup(popup)
        fields, pbrand = p["fields"], p["brand"] or brand
        if preorder:
            # no ORDER_CONFIRM_NO until complete — stash under a synthetic id
            token = "preorder:" + os.urandom(8).hex()
        else:
            token = p["confirm_no"]
        # stash the exact fields the matching submit must echo back, keyed by token
        self._pending_orders = getattr(self, "_pending_orders", {})
        self._pending_orders[token] = {
            "side": side, "brand_id": brand_id, "fields": fields,
            "account_type": account_type, "referer": ref, "preorder": preorder,
            "confirm_no": p["confirm_no"], "amount_jpy": amount_jpy,
            "price": pbrand.get("PRICE") or brand.get("PRICE"),
            "fx": pbrand.get("EXCHANGE_RATE") or brand.get("EXCHANGE_RATE")}
        return {
            "token": token,
            "preorder": bool(preorder),
            "execute_date": p["execute_date"],
            # the server's ORDER_AMOUNT is what will actually be ordered — guards use it
            "total_jpy": int(float(fields.get("ORDER_AMOUNT") or amount_jpy)),
            "est_price": fields.get("ORDER_PRICE") or pbrand.get("PRICE") or brand.get("PRICE"),
            "fee_jpy": int(float(fields.get("ORDER_FEE") or fields.get("FEE") or 0)),
            "brand_id": brand_id, "symbol": req.symbol, "account_type": account_type,
            "raw": {"fields": fields, "data": p["data"], "message": self._message(popup)},
        }

    def order_confirm(self, req) -> dict:
        side = getattr(getattr(req, "side", None), "value", getattr(req, "side", None))
        return self._confirm(req, "sell" if side == "sell" else "buy")

    @staticmethod
    def _submit_form(pending: dict, trade_password: str, csrf: str) -> dict:
        f = pending["fields"]
        form = {"BRAND_ID": pending["brand_id"], "AMOUNT_TYP": "0",
                "ORDER_AMOUNT": str(pending["amount_jpy"]),
                "ORDER_PRICE": "" if pending.get("price") is None else str(pending["price"]),
                "ORDER_EXCHANGE_RATE": "" if pending.get("fx") is None else str(pending["fx"]),
                "ORDER_QTY": "", "PLAN_TYPE": "0", "IS_NON_INSIDER_TRADING_CONFIRMED": "1",
                "ACCOUNT_TYPE": str(pending["account_type"])}
        form.update(f)                           # echo back exactly what confirm returned
        form["BRAND_ID"] = str(form.get("BRAND_ID") or pending["brand_id"])
        form["PREORDER"] = "1" if pending["preorder"] else str(f.get("PREORDER") or "0")
        form["ORDER_CONFIRM_NO"] = pending["confirm_no"]   # "" for a preorder
        form["CSRF_TOKEN"] = csrf or f.get("CSRF_TOKEN", "")
        form["TRADE_PASSWORD"] = trade_password
        return form

    def order_submit(self, token: str, req, trade_password: str = "") -> dict:
        """Place the order. Single-shot, NO retry. Requires the TRADE_PASSWORD and a
        prior order_confirm (whose preview token == token).

        Raises OrderOutcomeUnknown if the order POST was sent but no readable answer
        came back; RuntimeError if the server explicitly rejected it (or before sending)."""
        if not trade_password:
            raise RuntimeError("order_submit requires the TRADE_PASSWORD (取引パスワード)")
        pending = getattr(self, "_pending_orders", {}).pop(token, None)   # single-use token
        if not pending:
            raise RuntimeError("no confirmed order for this token — call order_confirm first")
        side = pending["side"]
        csrf = self._csrf()
        if not csrf:
            raise RuntimeError("no fuel_csrf_token cookie (order page not seeded) — "
                               "nothing was sent; re-run the order")
        form = self._submit_form(pending, trade_password, csrf)
        self.ensure_session()                    # a login failure here is before sending
        # step 1 — the actual order
        try:
            resp = self._order_json_post(f"/trade/brand/ajax_{side}_complete", data=form,
                                         referer=pending["referer"])
        except Exception as e:  # noqa: BLE001 — anything after the send is ambiguous
            raise OrderOutcomeUnknown(f"order submit outcome unknown: {e}") from e
        if not isinstance(resp, dict):
            raise OrderOutcomeUnknown("order submit outcome unknown: unexpected response shape")
        if not self._status_ok(resp):
            raise RuntimeError(f"order submit rejected: {self._message(resp)}")
        order_no = self._order_no(resp)
        # step 2 — receipt only; its failure is NOT an order failure
        receipt = None
        echo = {k: v for k, v in resp.items() if isinstance(v, (str, int, float))}
        if isinstance(resp.get("DATA"), dict):
            echo.update({k: v for k, v in resp["DATA"].items() if isinstance(v, (str, int, float))})
        echo.update({"BRAND_ID": form["BRAND_ID"], "PREORDER": form["PREORDER"], "CSRF_TOKEN": csrf})
        try:
            receipt = self._order_json_post(f"/trade/brand/ajax_{side}_complete_popup.json",
                                            data=echo, referer=pending["referer"])
            if isinstance(receipt, dict):
                order_no = order_no or self._order_no(receipt)
        except Exception:  # noqa: BLE001 — the order already went through step 1
            receipt = None
        return {"order_id": order_no, "preorder": bool(pending["preorder"]),
                "raw": resp, "receipt": receipt}

    @classmethod
    def _order_no(cls, resp: dict):
        data = resp.get("DATA") if isinstance(resp.get("DATA"), dict) else {}
        no = (cls._hidden_fields(resp.get("HTML") or "").get("ORDER_NO")
              or resp.get("ORDER_NO") or data.get("ORDER_NO"))
        return str(no) if no else None

    def open_orders(self, market: str = "usa") -> list:
        """未約定/予約注文 from /trade/preorder/ (parsed in parsers.parse_open_orders)."""
        from . import parsers
        return parsers.parse_open_orders(self.fetch("/trade/preorder/"))

    def order_cancel(self, order_id: str, market: str = "usa") -> dict:
        """Cancel a pending order. The cancel endpoint/fields are finalized against a
        live open order (needs an actual pending order to capture)."""
        resp = self._order_json_post(
            "/trade/preorder/ajax_cancel",
            data={"ORDER_NO": order_id, "CSRF_TOKEN": self._csrf()},
            referer="/trade/preorder/")
        if not self._status_ok(resp):
            msg = self._message(resp) if isinstance(resp, dict) else "unexpected response"
            raise RuntimeError(f"cancel rejected: {msg}")
        return {"order_id": order_id, "raw": resp}
