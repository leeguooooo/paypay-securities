"""Account snapshot persistence — the CLI's own long-term time series.

`paypay snapshot save` writes a JSON snapshot of the account's headline numbers
(assets / cash / holdings / realized / deposits) under
``<state_dir>/snapshots/<YYYYmmdd-HHMMSS>.json``. `paypay diff` then compares a
live read against a saved baseline so you can see "this week's change".

This module is pure persistence: it never fetches. The CLI builds the snapshot
dict (so all network/aggregation logic stays in one place) and hands it here.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .client import state_dir

_JST = timezone(timedelta(hours=9))
TS_FMT = "%Y%m%d-%H%M%S"


def now_ts() -> str:
    """Filesystem-safe JST timestamp used as the snapshot id/filename stem."""
    return datetime.now(timezone.utc).astimezone(_JST).strftime(TS_FMT)


def parse_ts(stem: str) -> datetime | None:
    try:
        return datetime.strptime(stem, TS_FMT).replace(tzinfo=_JST)
    except (ValueError, TypeError):
        return None


def snap_dir(account: str | None) -> Path:
    return state_dir(account) / "snapshots"


def save(account: str | None, snapshot: dict) -> Path:
    d = snap_dir(account)
    d.mkdir(parents=True, exist_ok=True)
    ts = snapshot.get("ts") or now_ts()
    snapshot = {**snapshot, "ts": ts}
    fp = d / f"{ts}.json"
    fp.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return fp


def list_paths(account: str | None) -> list[Path]:
    d = snap_dir(account)
    return sorted(d.glob("*.json")) if d.exists() else []


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def latest(account: str | None) -> dict | None:
    paths = list_paths(account)
    return load(paths[-1]) if paths else None


def nearest_before(account: str | None, days: int) -> dict | None:
    """The most recent snapshot at least `days` days old; falls back to the
    oldest snapshot when none is that old (so diff still has a baseline)."""
    cutoff = datetime.now(timezone.utc).astimezone(_JST) - timedelta(days=days)
    paths = list_paths(account)
    if not paths:
        return None
    older = [p for p in paths if (parse_ts(p.stem) or datetime.now(_JST)) <= cutoff]
    return load(older[-1] if older else paths[0])
