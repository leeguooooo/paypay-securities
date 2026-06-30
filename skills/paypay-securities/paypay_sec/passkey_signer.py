"""Self-contained WebAuthn (FIDO2) assertion signer for PayPay証券 headless login.

We hold the passkey *private* key (extracted from Bitwarden via the rbw fido2
extractor, which emits PKCS#8 — base64url or PEM). Given a server challenge we
build a complete ``navigator.credentials.get()`` assertion and the exact submit
body PayPay expects, signing it ourselves with ES256.

All parameters here were DECODED FROM A REAL captured PayPay assertion — they are
replicated exactly, not guessed:

  - rpId                = "paypay-sec.co.jp"          (sha256 == observed rpIdHash)
  - clientDataJSON.origin = "https://cdn.cross-platform-web.devops-app.paypay-sec.co.jp"
  - clientDataJSON is COMPACT JSON (no spaces) — we sign the exact bytes we send.
  - authenticatorData (37 bytes) = sha256(rpId) || flags(0x1d) || signCount(0x00000000)
        flags 0x1d = UP | UV | BE | BS  (Bitwarden software-passkey fingerprint)
  - signature = ES256 = ECDSA(P-256, SHA-256) over authenticatorData || sha256(clientDataJSON),
        DER-encoded (what cryptography's ``sign()`` returns by default).

base64url everywhere is WITHOUT padding.
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

__all__ = [
    "b64url_encode",
    "b64url_decode",
    "load_private_key",
    "build_authenticator_data",
    "build_client_data_json",
    "build_assertion",
]

DEFAULT_RP_ID = "paypay-sec.co.jp"
DEFAULT_ORIGIN = "https://cdn.cross-platform-web.devops-app.paypay-sec.co.jp"
DEFAULT_CREDENTIAL_ID_B64URL = "U2CXcUWLsvSuccQZ_20OIQ"

# flags 0x1d = UP(0x01) | UV(0x04) | BE(0x08) | BS(0x10)  (Bitwarden software passkey)
FLAGS = 0x1D
SIGN_COUNT = 0


# --------------------------------------------------------------------------- #
# base64url (no padding)
# --------------------------------------------------------------------------- #
def b64url_encode(data: bytes) -> str:
    """Encode bytes to base64url WITHOUT padding."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(s: str) -> bytes:
    """Decode a base64url string, tolerating missing padding."""
    s = s.strip()
    pad = (-len(s)) % 4
    return base64.urlsafe_b64decode(s + ("=" * pad))


# --------------------------------------------------------------------------- #
# private-key loading
# --------------------------------------------------------------------------- #
def load_private_key(
    key: "str | bytes",
) -> ec.EllipticCurvePrivateKey:
    """Load an EC P-256 private key from any of:

      - PKCS#8 DER ``bytes``
      - PEM ``bytes`` or ``str`` (``-----BEGIN ... KEY-----``)
      - base64url-encoded PKCS#8 DER ``str``

    The rbw fido2 extractor emits the latter two.
    """
    if isinstance(key, str):
        text = key.strip()
        if "-----BEGIN" in text:
            data = text.encode("ascii")
            return _load_pem(data)
        # base64url PKCS#8 DER
        der = b64url_decode(text)
        return _load_der(der)

    # bytes: could be PEM or DER
    if b"-----BEGIN" in key:
        return _load_pem(key)
    return _load_der(key)


def _load_der(der: bytes) -> ec.EllipticCurvePrivateKey:
    priv = serialization.load_der_private_key(der, password=None)
    _check_p256(priv)
    return priv


def _load_pem(pem: bytes) -> ec.EllipticCurvePrivateKey:
    priv = serialization.load_pem_private_key(pem, password=None)
    _check_p256(priv)
    return priv


def _check_p256(priv: object) -> ec.EllipticCurvePrivateKey:
    if not isinstance(priv, ec.EllipticCurvePrivateKey):
        raise ValueError(f"expected an EC private key, got {type(priv).__name__}")
    if not isinstance(priv.curve, ec.SECP256R1):
        raise ValueError(f"expected P-256 (SECP256R1), got {priv.curve.name}")
    return priv


# --------------------------------------------------------------------------- #
# WebAuthn pieces
# --------------------------------------------------------------------------- #
def build_authenticator_data(rp_id: str = DEFAULT_RP_ID) -> bytes:
    """37-byte authenticatorData (no extensions):

    sha256(rpId)[32] || flags(1)=0x1d || signCount(4, big-endian)=0.
    """
    rp_id_hash = hashlib.sha256(rp_id.encode("utf-8")).digest()
    auth_data = rp_id_hash + bytes([FLAGS]) + struct.pack(">I", SIGN_COUNT)
    assert len(auth_data) == 37, len(auth_data)
    return auth_data


def build_client_data_json(
    challenge_b64url: str,
    origin: str = DEFAULT_ORIGIN,
) -> bytes:
    """Compact (no-space) clientDataJSON bytes — exactly what we sign and send.

    The challenge is passed through verbatim (it is already base64url, no padding,
    as the server issued it).
    """
    obj = {
        "type": "webauthn.get",
        "challenge": challenge_b64url,
        "origin": origin,
        "crossOrigin": False,
    }
    # separators avoid spaces → compact serialization. ensure_ascii keeps it byte-stable.
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sign_es256(
    private_key: ec.EllipticCurvePrivateKey,
    authenticator_data: bytes,
    client_data_json: bytes,
) -> bytes:
    """ES256 signature (DER) over authenticatorData || sha256(clientDataJSON)."""
    client_data_hash = hashlib.sha256(client_data_json).digest()
    signed_bytes = authenticator_data + client_data_hash
    return private_key.sign(signed_bytes, ec.ECDSA(hashes.SHA256()))


# --------------------------------------------------------------------------- #
# public entry point
# --------------------------------------------------------------------------- #
def build_assertion(
    *,
    private_key_pkcs8_der: bytes,
    credential_id_b64url: str,
    user_handle_b64url: str,
    challenge_b64url: str,
    rp_id: str = DEFAULT_RP_ID,
    origin: str = DEFAULT_ORIGIN,
) -> dict:
    """Build the ``{"public_key_credential": {...}}`` payload ready to POST.

    ``private_key_pkcs8_der`` is PKCS#8 DER bytes (use :func:`load_private_key`
    to obtain a key object from PEM / base64url, then re-export, OR pass DER
    directly here). The challenge / credentialId / userHandle are base64url
    (no padding) and passed through to the response unchanged.
    """
    private_key = load_private_key(private_key_pkcs8_der)

    authenticator_data = build_authenticator_data(rp_id)
    client_data_json = build_client_data_json(challenge_b64url, origin)
    signature = _sign_es256(private_key, authenticator_data, client_data_json)

    # Match the EXACT shape a real (Bitwarden) browser assertion sends to PayPay's
    # BFF — the server validates the full PublicKeyCredential JSON and rejects (with
    # a generic general_system_error) if authenticatorAttachment / clientExtensionResults
    # are missing. Confirmed by diffing a captured working assertion.
    return {
        "public_key_credential": {
            "id": credential_id_b64url,
            "rawId": credential_id_b64url,
            "response": {
                "clientDataJSON": b64url_encode(client_data_json),
                "authenticatorData": b64url_encode(authenticator_data),
                "signature": b64url_encode(signature),
                "userHandle": user_handle_b64url,
            },
            "authenticatorAttachment": "platform",
            "clientExtensionResults": {},
            "type": "public-key",
        }
    }


# --------------------------------------------------------------------------- #
# self-test (no network)
# --------------------------------------------------------------------------- #
def selftest() -> None:
    import os

    # 1. fresh P-256 key, export PKCS#8 DER.
    priv = ec.generate_private_key(ec.SECP256R1())
    pkcs8_der = priv.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # 2. build_assertion with a random 32-byte challenge.
    challenge_b64url = b64url_encode(os.urandom(32))
    user_handle_b64url = b64url_encode(os.urandom(16))
    cred_id = DEFAULT_CREDENTIAL_ID_B64URL

    payload = build_assertion(
        private_key_pkcs8_der=pkcs8_der,
        credential_id_b64url=cred_id,
        user_handle_b64url=user_handle_b64url,
        challenge_b64url=challenge_b64url,
    )

    pkc = payload["public_key_credential"]
    resp = pkc["response"]

    # structural shape
    assert pkc["id"] == cred_id
    assert pkc["rawId"] == cred_id
    assert pkc["type"] == "public-key"
    assert resp["userHandle"] == user_handle_b64url

    # decode the pieces back
    auth_data = b64url_decode(resp["authenticatorData"])
    client_data_json = b64url_decode(resp["clientDataJSON"])
    signature = b64url_decode(resp["signature"])

    # 4. authenticatorData structural asserts
    assert len(auth_data) == 37, f"authData len={len(auth_data)}"
    assert auth_data[32] == 0x1D, f"flags={auth_data[32]:#04x}"
    assert auth_data[33:37] == b"\x00\x00\x00\x00", "signCount != 0"
    assert auth_data[:32] == hashlib.sha256(DEFAULT_RP_ID.encode()).digest(), "rpIdHash mismatch"

    # 5. clientDataJSON content asserts
    cd = json.loads(client_data_json)
    assert cd["type"] == "webauthn.get", cd["type"]
    assert cd["origin"] == DEFAULT_ORIGIN, cd["origin"]
    assert cd["challenge"] == challenge_b64url, "challenge mismatch"
    assert cd["crossOrigin"] is False
    # compact: no spaces in the serialized bytes
    assert b" " not in client_data_json, "clientDataJSON is not compact"

    # 3. independently RE-VERIFY the signature with the public key.
    signed_bytes = auth_data + hashlib.sha256(client_data_json).digest()
    pub = priv.public_key()
    pub.verify(signature, signed_bytes, ec.ECDSA(hashes.SHA256()))  # raises on failure

    # also confirm a Prehashed verification path (same digest) works, for parity.
    digest = hashlib.sha256(signed_bytes).digest()
    pub.verify(signature, digest, ec.ECDSA(Prehashed(hashes.SHA256())))

    print("PASS — all asserts validated")
    print(f"  authenticatorData ({len(auth_data)}B) = {resp['authenticatorData']}")
    print(f"  flags = {auth_data[32]:#04x}  signCount = {struct.unpack('>I', auth_data[33:37])[0]}")
    print(f"  rpIdHash = {auth_data[:32].hex()}")
    print(f"  clientDataJSON = {client_data_json.decode()}")
    print(f"  signature (DER, {len(signature)}B) = {resp['signature']}")
    print(f"  signature verified against fresh public key: OK")


if __name__ == "__main__":
    selftest()
