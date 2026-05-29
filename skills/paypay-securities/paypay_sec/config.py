"""Configuration & credential loading.

Credentials are read from environment variables, optionally seeded from a
.env file. They are NEVER hard-coded and NEVER logged. Search order for the
.env file: $PAYPAY_ENV, ./.env, ./spike/.env (first that exists wins).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

_ENV_CANDIDATES = ("PAYPAY_ENV",)


def _env_file() -> Path | None:
    explicit = os.environ.get("PAYPAY_ENV")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    # canonical user location — works regardless of cwd, lives OUTSIDE the repo /
    # installed skill dir (recommended when installed via `npx skills add`).
    home = Path.home() / ".paypay-sec" / ".env"
    if home.exists():
        return home
    here = Path.cwd()
    for rel in (".env", "spike/.env"):
        p = here / rel
        if p.exists():
            return p
    # finally, alongside the package root (dev checkout)
    root = Path(__file__).resolve().parent.parent
    for rel in (".env", "spike/.env"):
        p = root / rel
        if p.exists():
            return p
    return None


def load_dotenv() -> None:
    """Seed os.environ from the discovered .env (without overriding real env)."""
    path = _env_file()
    if not path:
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    member_id: str
    password: str
    cookie: str          # full Cookie header string; must hold the device token
    uuid: str = "uuid_pc"

    @property
    def has_device_token(self) -> bool:
        return "SMS_AUTH_STRING" in self.cookie

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        member_id = os.environ.get("PAYPAY_MEMBER_ID", "").strip()
        password = os.environ.get("PAYPAY_PASSWORD", "").strip()
        if not member_id or not password:
            raise RuntimeError(
                "Missing credentials: set PAYPAY_MEMBER_ID and PAYPAY_PASSWORD "
                "(in .env or environment). See .env.example."
            )
        return cls(
            member_id=member_id,
            password=password,
            cookie=os.environ.get("PAYPAY_COOKIE", "").strip(),
            uuid=os.environ.get("PAYPAY_UUID", "uuid_pc").strip() or "uuid_pc",
        )
