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
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .client import state_dir

_JST = timezone(timedelta(hours=9))
TS_FMT = "%Y%m%d-%H%M%S"


def atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file in the same dir + os.replace, so a crash or a
    concurrent reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def now_ts() -> str:
    """Filesystem-safe JST timestamp used as the snapshot id/filename stem."""
    return datetime.now(timezone.utc).astimezone(_JST).strftime(TS_FMT)


def parse_ts(stem: str) -> datetime | None:
    """Parse a snapshot stem; a same-second de-dup suffix ('_1') is ignored."""
    try:
        return datetime.strptime(str(stem).split("_", 1)[0], TS_FMT).replace(tzinfo=_JST)
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
    n = 1
    while fp.exists():                 # two saves in one second → don't overwrite
        fp = d / f"{ts}_{n}.json"      # '_' sorts after '.', so it stays later
        n += 1
    atomic_write_text(fp, json.dumps(snapshot, ensure_ascii=False, indent=2))
    return fp


def list_paths(account: str | None) -> list[Path]:
    d = snap_dir(account)
    return sorted(d.glob("*.json")) if d.exists() else []


def load(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def try_load(path: Path) -> dict | None:
    """load() that warns on stderr and returns None for an unreadable/corrupt
    snapshot, so one bad file never takes down list/diff/calendar."""
    try:
        s = load(path)
    except (OSError, ValueError) as e:
        print(f"warning: skipping unreadable snapshot {path}: {type(e).__name__}", file=sys.stderr)
        return None
    if not isinstance(s, dict):
        print(f"warning: skipping malformed snapshot {path}", file=sys.stderr)
        return None
    return s


def load_all(account: str | None) -> list[dict]:
    """Every readable snapshot, oldest first (unreadable ones skipped)."""
    return [s for s in (try_load(p) for p in list_paths(account)) if s is not None]


def _newest_readable(paths: list[Path]) -> dict | None:
    for p in reversed(paths):
        s = try_load(p)
        if s is not None:
            return s
    return None


def latest(account: str | None) -> dict | None:
    return _newest_readable(list_paths(account))


def nearest_before(account: str | None, days: int) -> dict | None:
    """The most recent snapshot at least `days` days old; falls back to the
    oldest snapshot when none is that old (so diff still has a baseline)."""
    cutoff = datetime.now(timezone.utc).astimezone(_JST) - timedelta(days=days)
    paths = list_paths(account)
    if not paths:
        return None
    older = [p for p in paths if (parse_ts(p.stem) or datetime.now(_JST)) <= cutoff]
    if older:
        s = _newest_readable(older)
        if s is not None:
            return s
    for p in paths:                    # fallback: oldest readable snapshot
        s = try_load(p)
        if s is not None:
            return s
    return None
