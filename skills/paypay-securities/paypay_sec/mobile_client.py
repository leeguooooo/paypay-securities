"""Pure-Python client for the PayPay証券 **mobile** API (read-only).

Discovered by MITM-ing the Android app (jp.co.onetapbuy.jpstock 3.48.6). Unlike
the web SSR frontend (see client.py), the mobile app talks to a versioned JSON
API split across two hosts, with a third static-config host:

  AUTH host   web-trnelb.paypay-sec.co.jp          HTTP/2, Ktor, form-urlencoded
    POST /v2/common/init          device/catalog bootstrap (issues a UUID)
    POST /v2/client/me/login      → Set-Cookie: laravel_session (domain=paypay-sec.co.jp)
    POST /v2/client/notice/count

  DATA host   bff.native-devops.view-trnelb.paypay-sec.co.jp   (BFF, JSON)
    GET /top/asset/v1             total assets / portfolio overview
    GET /top/display-info/v3      home display info
    GET /favorite/v1             watchlist (お気に入り)
    GET /brand/metadata/list/v1   brand metadata
    auth: Cookie laravel_session + headers client-type / app-version

Two facts that make pure-Python login possible without reversing the app's
crypto (verified empirically):
  * ENCRYPTED_PASSWORD is DETERMINISTIC for a given account password (12/12
    captured logins were byte-identical, no nonce) — so the once-captured value
    is reusable like a credential. It is NOT computed by libsigner.so (that is
    the Adjust SDK signer) nor by javax.crypto.Cipher; treat it as opaque.
  * SMS_AUTH_STRING is the trusted-device token (same concept as the web flow's
    cookie); captured once, reused to skip SMS.

KNOWN GATE (2026-05): the BFF data endpoints return **401** even for the real
app when logged in via this 3.48.6 build. login.json returns STATUS=1 but with
``NAVIGATE_TO_PASSKEY=1`` and message code 00156 ("update to the latest version
to use passkey"). The working theory is that the server has deprecated
old-version logins to a *passkey-pending* second-class session that the BFF
rejects. So a 401 from the BFF is expected until the session is upgraded
(passkey enrolment on a current app build) or the server lifts the gate.

VERIFIED (2026-05-30): bumping the ``app-version`` header alone does NOT help —
probed 3.48.6 / 3.49 / 3.50 / 3.60 / 4.0 / 99.99.99 / "" against /top/asset/v1
with a fresh laravel_session: all 401. The gate is SESSION-STATE, not a header
version check. The real app on 3.48.6 also gets 401 on all four BFF endpoints
(captured) and never sends an Authorization/Bearer header — BFF auth is the
laravel_session cookie, and a passkey-pending cookie is categorically rejected.
A BFF-accepted session therefore requires completing passkey enrolment on a
current app build (which this version cannot do). ``app_version`` stays
configurable for completeness, but it is not the lever.

Credentials come from the account .env (see config.py), mobile-specific keys:
  PAYPAY_ENCRYPTED_PASSWORD   captured deterministic password ciphertext (hex)
  PAYPAY_SMS_AUTH_STRING      trusted-device token (hex)
  PAYPAY_MOBILE_UUID          UUID the app logs in with
  PAYPAY_APP_VERSION          default "3.48.6"
  PAYPAY_DEVICE_TOKEN         FCM token (optional; "x" works for login)
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import requests

from .config import DEFAULT_ACCOUNT, load_dotenv

AUTH_BASE = "https://web-trnelb.paypay-sec.co.jp"
BFF_BASE = "https://bff.native-devops.view-trnelb.paypay-sec.co.jp"
CONFIG_BASE = "https://config.native-app.devops-app.paypay-sec.co.jp"
_UA = "ktor-client"
_CLIENT_TYPE = "NativeMainSecurityAndroid"


class MobileLoginError(RuntimeError):
    """Mobile login failed (bad creds, expired device token, server gate…)."""


class BffUnauthorized(RuntimeError):
    """BFF returned 401 — see module docstring: old-version login is gated to a
    passkey-pending session the data API rejects."""


@dataclass(frozen=True)
class MobileSettings:
    member_id: str
    encrypted_password: str   # deterministic ciphertext (reusable credential)
    sms_auth_string: str      # trusted-device token
    uuid: str
    app_version: str = "3.48.6"
    device_token: str = "x"
    os_version: str = "Android30"
    app_id: str = "3"
    account: str = DEFAULT_ACCOUNT

    @classmethod
    def from_env(cls, account: str | None = None) -> "MobileSettings":
        account = account or os.environ.get("PAYPAY_ACCOUNT") or DEFAULT_ACCOUNT
        load_dotenv(account)
        member_id = os.environ.get("PAYPAY_MEMBER_ID", "").strip()
        enc = os.environ.get("PAYPAY_ENCRYPTED_PASSWORD", "").strip()
        sms = os.environ.get("PAYPAY_SMS_AUTH_STRING", "").strip()
        uuid = os.environ.get("PAYPAY_MOBILE_UUID", "").strip()
        missing = [k for k, v in {
            "PAYPAY_MEMBER_ID": member_id,
            "PAYPAY_ENCRYPTED_PASSWORD": enc,
            "PAYPAY_SMS_AUTH_STRING": sms,
            "PAYPAY_MOBILE_UUID": uuid,
        }.items() if not v]
        if missing:
            raise MobileLoginError(
                "Missing mobile credentials: " + ", ".join(missing) +
                f" — set them in the account .env for '{account}'. These are "
                "captured once from the Android app's login request (see "
                "research/mobile_fixtures/API_MAP.md)."
            )
        return cls(
            member_id=member_id,
            encrypted_password=enc,
            sms_auth_string=sms,
            uuid=uuid,
            app_version=os.environ.get("PAYPAY_APP_VERSION", "3.48.6").strip() or "3.48.6",
            device_token=os.environ.get("PAYPAY_DEVICE_TOKEN", "x").strip() or "x",
            account=account,
        )


class PayPayMobileClient:
    """Read-only mobile API client. Never trades."""

    def __init__(self, settings: MobileSettings | None = None):
        self.settings = settings or MobileSettings.from_env()
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": _UA,
            "Accept": "application/json",
            "Accept-Charset": "UTF-8",
            "Accept-Encoding": "gzip",
        })
        self.login_payload: dict | None = None

    # --- auth host ---
    def _device_fields(self) -> dict:
        s = self.settings
        return {
            "UUID": s.uuid, "APP_ID": s.app_id,
            "APP_VERSION": s.app_version, "OS": s.os_version,
            "DEVICE_TOKEN": s.device_token,
        }

    def init(self) -> dict:
        """POST /v2/common/init — device/catalog bootstrap. Returns parsed JSON."""
        s = self.settings
        r = self._session.post(
            f"{AUTH_BASE}/v2/common/init",
            data={
                "DEVICE_MODEL": "sdk_gphone_arm64", "DEVICE_SERIAL": "",
                "CARRIER": "Android", "USERAGENT": "Mozilla/5.0",
                "STOCK_COUNTRY_FLG[]": "9", "TOKEN": "", **self._device_fields(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()

    def login(self) -> dict:
        """POST /v2/client/me/login. Establishes laravel_session in the session
        jar. Returns the parsed payload. Raises MobileLoginError on rejection."""
        s = self.settings
        r = self._session.post(
            f"{AUTH_BASE}/v2/client/me/login",
            data={
                "MEMBER_ID": s.member_id,
                "ENCRYPTED_PASSWORD": s.encrypted_password,
                "AUTOLOGIN_FLG": "0",
                "SMS_AUTH_STRING": s.sms_auth_string,
                "TOKEN": "", **self._device_fields(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            timeout=30,
        )
        if r.status_code != 200:
            raise MobileLoginError(f"login returned HTTP {r.status_code}")
        payload = r.json()
        self.login_payload = payload
        if not payload.get("STATUS"):
            raise MobileLoginError(f"login rejected: {payload.get('MESSAGE_ARRAY')}")
        if "laravel_session" not in self._session.cookies.get_dict(domain=".paypay-sec.co.jp") \
                and not any("laravel_session" == c.name for c in self._session.cookies):
            raise MobileLoginError("login succeeded but no laravel_session was set")
        return payload

    @property
    def navigate_to_passkey(self) -> bool:
        """True when the server flags this (old-version) session as passkey-
        pending — the BFF data endpoints will 401 in that case."""
        return bool((self.login_payload or {}).get("NAVIGATE_TO_PASSKEY"))

    # --- BFF data host ---
    def _bff_get(self, path: str) -> dict:
        s = self.settings
        r = self._session.get(
            f"{BFF_BASE}{path}",
            params={"client-type": _CLIENT_TYPE, "app-version": s.app_version},
            headers={"client-type": _CLIENT_TYPE, "app-version": s.app_version},
            timeout=30,
        )
        if r.status_code == 401:
            raise BffUnauthorized(
                f"BFF {path} → 401. Old-version login is gated to a passkey-"
                "pending session the data API rejects (the real app sees this "
                "too). Upgrade the session (passkey on a current build) or wait "
                "for the server gate to lift; see module docstring."
            )
        r.raise_for_status()
        return r.json()

    def top_asset(self) -> dict:
        """GET /top/asset/v1 — total assets / portfolio overview."""
        return self._bff_get("/top/asset/v1")

    def top_display_info(self) -> dict:
        """GET /top/display-info/v3 — home display info."""
        return self._bff_get("/top/display-info/v3")

    def favorites(self) -> dict:
        """GET /favorite/v1 — watchlist (お気に入り)."""
        return self._bff_get("/favorite/v1")

    def brand_metadata(self) -> dict:
        """GET /brand/metadata/list/v1 — brand metadata."""
        return self._bff_get("/brand/metadata/list/v1")


def _demo() -> int:
    """Smoke test: init → login → try the BFF asset endpoint."""
    c = PayPayMobileClient()
    print("init   :", "OK" if c.init().get("STATUS") is not None else "?")
    p = c.login()
    print("login  : STATUS=%s LOGIN_STATUS=%s NAVIGATE_TO_PASSKEY=%s"
          % (p.get("STATUS"), p.get("LOGIN_STATUS"), p.get("NAVIGATE_TO_PASSKEY")))
    try:
        a = c.top_asset()
        print("asset  : OK", str(a)[:200])
    except BffUnauthorized as e:
        print("asset  : 401 —", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
