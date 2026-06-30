"""Headless PayPay証券 passkey (FIDO2) login.

PayPay locked this account to passkey auth (PASSKEY_REQUIRED) — password login no
longer mints an authorized session. The passkey's private key lives in the user's
self-hosted Bitwarden/Vaultwarden, so we can drive the WebAuthn assertion ourselves
instead of tapping Touch ID in a browser every ~60 min.

Flow (all plain HTTP, no browser, reverse-engineered from passkey-v2-*.js + a HAR
of a real login — see the project memory):

    prepare  POST   {BFF}/api/passkey/prepare/v1?action=VERIFY   {redirect_url} -> temp_id
    options  DELETE {BFF}/api/passkey/options/v1?action=VERIFY   {temp_id}      -> publicKey.challenge
    (sign the challenge locally with the passkey private key — passkey_signer.py)
    credential POST {BFF}/api/passkey/credential/v1?action=VERIFY {public_key_credential, temp_id}
    complete   POST {BFF}/api/passkey/complete/v1?action=VERIFY   {passkey_id, temp_id} -> redirect
    callback   GET  {WWW}/login/passkey/callback?passkey_status=success...  -> mints laravel_session

The two load-bearing headers are `app-id: NATIVE_PC` and `device-id: <uuid>`; no
cookie / CSRF / Origin gating server-side. discoverable-credential flow (no
allowCredentials), so we supply our own credentialId.

Secret model (extract-once): the PayPay passkey private key is pulled ONCE from
Bitwarden via the forked `rbw fido2 get` and cached in the macOS Keychain — the
Bitwarden master password never lands on this box, and only this one passkey does.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import requests

from .passkey_signer import b64url_decode, b64url_encode, build_assertion


def _credential_id_to_b64url(cid: str) -> str:
    """Bitwarden stores a passkey credentialId as a hyphenated hex GUID
    (e.g. 53609771-458b-b2f4-ae71-c419ff6d0e21), but WebAuthn / PayPay want the
    raw 16 bytes as base64url (U2CXcUWLsvSuccQZ_20OIQ). Convert hex-GUIDs;
    pass through anything already base64url."""
    raw = cid.strip()
    hexish = raw.replace("-", "")
    if "-" in raw and len(hexish) == 32 and all(c in "0123456789abcdefABCDEF" for c in hexish):
        return b64url_encode(bytes.fromhex(hexish))
    return raw

BFF = "https://cross-platform-web-bff.devops-app.paypay-sec.co.jp"
WWW = "https://www.paypay-sec.co.jp"
ORIGIN = "https://cdn.cross-platform-web.devops-app.paypay-sec.co.jp"
RP_ID = "paypay-sec.co.jp"
APP_ID = "NATIVE_PC"
KEYCHAIN_SERVICE = "paypay-passkey"


class PasskeyLoginError(RuntimeError):
    """Headless passkey login failed (no cached key, server rejection, etc.)."""


# --------------------------------------------------------------------------- #
# device-id — stable per account, minted once and persisted in the state dir   #
# --------------------------------------------------------------------------- #
def _device_id(state_dir: Path) -> str:
    f = state_dir / "passkey_device_id"
    try:
        v = f.read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    v = str(uuid.uuid4())
    state_dir.mkdir(parents=True, exist_ok=True)
    f.write_text(v, encoding="utf-8")
    return v


def _bff_headers(device_id: str) -> dict:
    return {
        "content-type": "application/json",
        "app-id": APP_ID,
        "device-id": device_id,
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
    }


# --------------------------------------------------------------------------- #
# macOS Keychain — store/load the extracted passkey (extract-once secret model) #
# --------------------------------------------------------------------------- #
def _keychain_account(account: str) -> str:
    return f"paypay:{account}"


def store_passkey(account: str, blob: dict) -> None:
    """Persist {credential_id, user_handle, rp_id, private_key_b64url} in the
    macOS login Keychain (item is credential-equivalent; -U overwrites)."""
    payload = json.dumps(blob, separators=(",", ":"))
    subprocess.run(
        ["security", "add-generic-password", "-U",
         "-a", _keychain_account(account), "-s", KEYCHAIN_SERVICE,
         "-D", "paypay passkey", "-w", payload],
        check=True, capture_output=True, text=True,
    )


def load_passkey(account: str) -> dict | None:
    """Read the cached passkey blob from the Keychain, or None if not set."""
    r = subprocess.run(
        ["security", "find-generic-password",
         "-a", _keychain_account(account), "-s", KEYCHAIN_SERVICE, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# one-time extraction from Bitwarden via the forked `rbw fido2 get`             #
# --------------------------------------------------------------------------- #
def extract_via_rbw(rp_id: str = RP_ID, rbw_bin: str = "bitwarden-use") -> dict:
    """Run `bitwarden-use fido2 get <rp_id>` and parse credentialId / userHandle /
    private key. The private key is parsed in-process and NEVER printed by the caller.
    Requires bitwarden-use on PATH and the vault unlocked (prompts via pinentry).
    """
    r = subprocess.run([rbw_bin, "fido2", "get", rp_id],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise PasskeyLoginError(
            f"`{rbw_bin} fido2 get {rp_id}` failed (unlock rbw first?): "
            f"{r.stderr.strip() or r.stdout.strip()}")
    fields: dict[str, str] = {}
    for line in r.stdout.splitlines():
        for label, key in (("credentialId:", "credential_id"),
                           ("rpId:", "rp_id"),
                           ("userHandle:", "user_handle"),
                           ("privateKey (base64url):", "private_key_b64url")):
            if line.startswith(label):
                fields[key] = line[len(label):].strip()
    missing = [k for k in ("credential_id", "user_handle", "private_key_b64url")
               if not fields.get(k)]
    if missing:
        raise PasskeyLoginError(
            f"rbw fido2 get output missing {missing} — check the entry has a passkey")
    # Bitwarden stores credentialId as a hex GUID; WebAuthn wants base64url bytes.
    fields["credential_id"] = _credential_id_to_b64url(fields["credential_id"])
    fields.setdefault("rp_id", rp_id)
    return fields


def setup(account: str, rp_id: str = RP_ID, rbw_bin: str = "bitwarden-use") -> dict:
    """Extract the passkey from Bitwarden and cache it in the Keychain. Returns a
    SAFE summary (credentialId/rpId only — never the private key)."""
    blob = extract_via_rbw(rp_id=rp_id, rbw_bin=rbw_bin)
    store_passkey(account, blob)
    return {"credential_id": blob["credential_id"], "rp_id": blob["rp_id"],
            "stored": True}


# --------------------------------------------------------------------------- #
# the headless login                                                           #
# --------------------------------------------------------------------------- #
def _post_json(session: requests.Session, url: str, headers: dict, body: dict) -> dict:
    r = session.post(url, headers=headers, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("business_error_code")
    if err:
        raise PasskeyLoginError(f"{url.split('/api/')[-1]}: {err}")
    return data


def _delete_json(session: requests.Session, url: str, headers: dict, body: dict) -> dict:
    r = session.request("DELETE", url, headers=headers, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("business_error_code")
    if err:
        raise PasskeyLoginError(f"{url.split('/api/')[-1]}: {err}")
    return data


def passkey_login(session: requests.Session, account: str, state_dir: Path,
                  *, action: str = "VERIFY", debug: bool = False) -> dict:
    """Run the full headless passkey login on `session` (a requests.Session whose
    cookies we want populated with the www laravel_session). Returns a result dict.
    Raises PasskeyLoginError if no cached passkey or the server rejects us."""
    blob = load_passkey(account)
    if not blob:
        raise PasskeyLoginError(
            "no cached passkey for this account — run `paypay passkey-setup` once "
            "(unlock rbw, extracts the PayPay passkey from Bitwarden into the Keychain)")

    # Bootstrap a FRESH www session the way the browser does: drop any stale
    # laravel_session (seeded from .env / a prior login) and GET /login/ so the
    # server mints a clean unauthenticated laravel_session. The passkey callback
    # then AUTHORIZES that session (PayPay re-authorizes the existing cookie rather
    # than issuing a new one), so we end up owning a valid session that doesn't
    # depend on the expiring seeded cookie.
    for ck in list(session.cookies):
        if ck.name == "laravel_session":
            session.cookies.clear(ck.domain, ck.path, ck.name)
    try:
        session.get(f"{WWW}/login/", timeout=30)
    except requests.RequestException:
        pass

    device_id = _device_id(state_dir)
    headers = _bff_headers(device_id)
    q = f"?action={action}"

    # 1. prepare -> temp_id
    prep = _post_json(session, f"{BFF}/api/passkey/prepare/v1{q}", headers,
                      {"redirect_url": f"{WWW}/login/passkey/callback"})
    temp_id = prep.get("temp_id")
    if not temp_id:
        raise PasskeyLoginError(f"prepare returned no temp_id: {prep}")

    # 2. options -> challenge
    opts = _delete_json(session, f"{BFF}/api/passkey/options/v1{q}", headers,
                        {"temp_id": temp_id})
    public_key = (opts.get("options") or {}).get("publicKey") or {}
    challenge = public_key.get("challenge")
    if not challenge:
        raise PasskeyLoginError(f"options returned no challenge: {opts}")
    rp_id = public_key.get("rpId") or blob.get("rp_id") or RP_ID

    # 3. sign locally
    assertion = build_assertion(
        private_key_pkcs8_der=b64url_decode(blob["private_key_b64url"]),
        credential_id_b64url=blob["credential_id"],
        user_handle_b64url=blob["user_handle"],
        challenge_b64url=challenge,
        rp_id=rp_id,
        origin=ORIGIN,
    )

    # 4. credential — submit the assertion (temp_id alongside)
    cred_body = {**assertion, "temp_id": temp_id}
    cred = _post_json(session, f"{BFF}/api/passkey/credential/v1{q}", headers, cred_body)

    # 5. complete
    comp = _post_json(session, f"{BFF}/api/passkey/complete/v1{q}", headers,
                      {"passkey_id": blob["credential_id"], "temp_id": temp_id})

    # 6. callback on www -> mints laravel_session on `session`. The exact handoff
    #    (does complete hand back a redirect_url with a one-time code, or is
    #    temp_id enough?) is confirmed on the first live run; handle both.
    redirect_url = (comp.get("redirect_url") or comp.get("redirectUrl")
                    or comp.get("url") or comp.get("location"))
    if redirect_url and redirect_url.startswith(WWW):
        cb = redirect_url
    else:
        cb = f"{WWW}/login/passkey/callback?passkey_status=success&temp_id={temp_id}"
    cbr = session.get(cb, timeout=30, allow_redirects=True)

    # Success = the callback landed on an authenticated /trade page, NOT bounced to
    # /login with a passkey_status=error. (Checking cookie presence is a false
    # positive — a stale laravel_session is always in the jar.)
    final = cbr.url
    # Success = the callback followed its redirects to an authenticated /trade page;
    # a rejected assertion bounces to /login?passkey_status=error instead. (Do NOT
    # gate on a laravel_session cookie — that was a false NEGATIVE on a fresh session
    # with no seeded cookie, even though /trade/ loaded fine. Landing on /trade after
    # redirects already proves the session is authorized.)
    ok = ("/trade" in final) and ("/login" not in final) \
        and ("passkey_status=error" not in final)
    err_code = ""
    if "error_code=" in final:
        err_code = final.split("error_code=", 1)[1].split("&", 1)[0]
    result = {
        "ok": ok,
        "temp_id": temp_id,
        "credential_response": cred,
        "complete_response": comp,
        "callback_url": cb,
        "callback_final_url": final,
        "error_code": err_code,
        "has_laravel_session": "laravel_session" in session.cookies,
    }
    if debug:
        return result
    if not ok:
        raise PasskeyLoginError(
            f"passkey assertion not accepted (callback: {final})"
            + (f" [{err_code}]" if err_code else ""))
    return result
