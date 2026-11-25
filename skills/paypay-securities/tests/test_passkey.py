"""Offline tests for the headless passkey login helpers. No network, no Keychain."""
import subprocess

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from _runner import run
from paypay_sec import passkey_login as pl
from paypay_sec.passkey_signer import b64url_encode


def _jar_with_stale_cookies() -> requests.Session:
    s = requests.Session()
    s.cookies.set("laravel_session", "old", domain="www.paypay-sec.co.jp")
    s.cookies.set("__Secure-client-auth-state-prod", "stale", domain=".paypay-sec.co.jp")
    s.cookies.set("DEVICE_ID", "keep", domain=".paypay-sec.co.jp")
    s.cookies.set("fuelrid", "keep", domain=".paypay-sec.co.jp")
    return s


def test_drop_stale_auth_cookies_removes_bff_auth_state():
    # A stale __Secure-client-auth-state-prod seeded from PAYPAY_COOKIE made the
    # passkey callback bounce with general_system_error (found 2026-11-25).
    s = _jar_with_stale_cookies()
    dropped = pl._drop_stale_auth_cookies(s)
    names = {c.name for c in s.cookies}
    assert "__Secure-client-auth-state-prod" not in names, names
    assert "laravel_session" not in names, names
    assert {"DEVICE_ID", "fuelrid"} <= names, names
    assert sorted(dropped) == ["__Secure-client-auth-state-prod", "laravel_session"], dropped


def _rbw_output(private_key: str) -> subprocess.CompletedProcess:
    out = (
        "name: paypay-sec.co.jp\n"
        "credentialId: 00112233-4455-6677-8899-aabbccddeeff\n"
        "rpId: paypay-sec.co.jp\n"
        "userHandle: dXNlcg\n"
        "keyType: public-key\n"
        "keyCurve: P-256\n"
        f"privateKey (base64url): {private_key}\n"
    )
    return subprocess.CompletedProcess(["bitwarden-use"], 0, stdout=out, stderr="")


def _fresh_key_b64url() -> str:
    k = ec.generate_private_key(ec.SECP256R1())
    der = k.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption())
    return b64url_encode(der)


def test_parse_rbw_accepts_real_key_and_converts_credential_id():
    f = pl._parse_rbw_fido2(_rbw_output(_fresh_key_b64url()), rbw_bin="bitwarden-use",
                            rp_id="paypay-sec.co.jp")
    assert f["credential_id"] == "ABEiM0RVZneImaq7zN3u_w", f["credential_id"]
    assert f["rp_id"] == "paypay-sec.co.jp"


def test_parse_rbw_rejects_masked_placeholder():
    # bitwarden-use >= 0.12 prints a placeholder instead of the key without --reveal;
    # caching that would silently break every later login.
    try:
        pl._parse_rbw_fido2(_rbw_output("<hidden, pass `--reveal` to print>"),
                            rbw_bin="bitwarden-use", rp_id="paypay-sec.co.jp")
    except pl.PasskeyLoginError as e:
        assert "hidden" not in str(e), "error must not echo the field value"
        return
    raise AssertionError("masked placeholder was accepted as a private key")


def test_extract_falls_back_when_reveal_unsupported():
    calls = []
    key = _fresh_key_b64url()

    def fake_run(argv):
        calls.append(argv)
        if "--reveal" in argv:
            return subprocess.CompletedProcess(argv, 2, stdout="",
                                               stderr="error: unexpected argument '--reveal' found")
        return _rbw_output(key)

    orig = pl._run_rbw
    pl._run_rbw = fake_run
    try:
        f = pl.extract_via_rbw(rbw_bin="rbw")
    finally:
        pl._run_rbw = orig
    assert len(calls) == 2 and "--reveal" not in calls[1], calls
    assert f["credential_id"] == "ABEiM0RVZneImaq7zN3u_w"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
