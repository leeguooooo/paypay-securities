"""Append-only audit log for order activity (dry-run + submit + cancel).

One JSON object per line at ~/.paypay-sec/[<account>/]orders.log. Also the
source of truth for the daily order count guard. Never contains the password.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional, Union

from .client import state_dir

# Kinds that consume the daily order cap: a submit whose outcome is unknown may
# have been placed, so it counts too.
CAP_KINDS = ("submit", "submit_unknown")
# count_today() result when the log exists but can't be read — always >= any cap,
# so the daily-cap guard blocks (fail closed).
COUNT_UNREADABLE = 10 ** 9


def _log_path(account: Optional[str]) -> Path:
    return state_dir(account) / "orders.log"


def record(event: dict, *, account: Optional[str] = None,
           log_path: Optional[Path] = None, now: Optional[datetime] = None) -> bool:
    """Append one row. Returns False if it could not be written (never raises)."""
    path = log_path or _log_path(account)
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = {"ts": ts.isoformat(), "date": ts.strftime("%Y-%m-%d"), **event}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        return False  # auditing must never break the command
    return True


def count_today(*, account: Optional[str] = None, log_path: Optional[Path] = None,
                now: Optional[datetime] = None,
                kind: Union[str, Iterable[str]] = CAP_KINDS) -> int:
    path = log_path or _log_path(account)
    kinds = {kind} if isinstance(kind, str) else set(kind)
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):       # unreadable / undecodable -> fail closed
        return COUNT_UNREADABLE
    for line in text.splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r, dict) and r.get("kind") in kinds and r.get("date") == day:
            n += 1
    return n
