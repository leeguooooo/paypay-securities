"""Append-only audit log for order activity (dry-run + submit + cancel).

One JSON object per line at ~/.paypay-sec/[<account>/]orders.log. Also the
source of truth for the daily order count guard. Never contains the password.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .client import state_dir


def _log_path(account: Optional[str]) -> Path:
    return state_dir(account) / "orders.log"


def record(event: dict, *, account: Optional[str] = None,
           log_path: Optional[Path] = None, now: Optional[datetime] = None) -> None:
    path = log_path or _log_path(account)
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    row = {"ts": ts.isoformat(), "date": ts.strftime("%Y-%m-%d"), **event}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass  # auditing must never break the command


def count_today(*, account: Optional[str] = None, log_path: Optional[Path] = None,
                now: Optional[datetime] = None, kind: str = "submit") -> int:
    path = log_path or _log_path(account)
    day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d")
    n = 0
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == kind and r.get("date") == day:
                n += 1
    except OSError:
        return 0
    return n
