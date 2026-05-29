"""Configuration & credential loading (multi-account).

Credentials are read from environment variables, optionally seeded from a per-
account .env file under ~/.paypay-sec/. They are NEVER hard-coded / logged.

  default account → ~/.paypay-sec/.env   (also ./.env, ./spike/.env for dev)
  account "<name>" → ~/.paypay-sec/<name>.env

Select an account with `--account <name>` (CLI) or the PAYPAY_ACCOUNT env var.
Each account keeps its own session + response cache (see client.state_dir).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home() / ".paypay-sec"
DEFAULT_ACCOUNT = "default"


def env_file_for(account: str | None) -> Path | None:
    """Locate the .env for an account. Named accounts live only under ~/.paypay-sec/;
    the default account also falls back to cwd / package-root for dev checkouts."""
    if account and account != DEFAULT_ACCOUNT:
        p = HOME / f"{account}.env"
        return p if p.exists() else None
    explicit = os.environ.get("PAYPAY_ENV")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.exists() else None
    candidates = [HOME / ".env", Path.cwd() / ".env", Path.cwd() / "spike" / ".env"]
    root = Path(__file__).resolve().parent.parent
    candidates += [root / ".env", root / "spike" / ".env"]
    return next((p for p in candidates if p.exists()), None)


def load_dotenv(account: str | None = None) -> None:
    """Seed os.environ from the account's .env (without overriding real env vars)."""
    path = env_file_for(account)
    if not path:
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def list_accounts() -> list[str]:
    """Configured account names (default if ~/.paypay-sec/.env exists, plus each
    <name>.env)."""
    out = []
    if (HOME / ".env").exists():
        out.append(DEFAULT_ACCOUNT)
    if HOME.exists():
        out += sorted(p.stem for p in HOME.glob("*.env") if p.name != ".env")
    return out


@dataclass(frozen=True)
class Settings:
    member_id: str
    password: str
    cookie: str          # full Cookie header string; must hold the device token
    uuid: str = "uuid_pc"
    account: str = DEFAULT_ACCOUNT

    @property
    def has_device_token(self) -> bool:
        return "SMS_AUTH_STRING" in self.cookie

    @classmethod
    def from_env(cls, account: str | None = None) -> "Settings":
        account = account or os.environ.get("PAYPAY_ACCOUNT") or DEFAULT_ACCOUNT
        load_dotenv(account)
        member_id = os.environ.get("PAYPAY_MEMBER_ID", "").strip()
        password = os.environ.get("PAYPAY_PASSWORD", "").strip()
        if not member_id or not password:
            where = (f"~/.paypay-sec/{account}.env" if account != DEFAULT_ACCOUNT
                     else "~/.paypay-sec/.env")
            raise RuntimeError(
                f"Missing credentials for account '{account}': set PAYPAY_MEMBER_ID "
                f"and PAYPAY_PASSWORD in {where} (see .env.example)."
            )
        return cls(
            member_id=member_id,
            password=password,
            cookie=os.environ.get("PAYPAY_COOKIE", "").strip(),
            uuid=os.environ.get("PAYPAY_UUID", "uuid_pc").strip() or "uuid_pc",
            account=account,
        )
