"""Real market USD/JPY mid rates (ECB daily reference via frankfurter.dev).

Used to benchmark PayPay's applied exchange rate and surface the hidden FX
spread. Historical rates are immutable, so they are cached forever on disk.
Network failures degrade gracefully (returns whatever is cached).
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import requests

from .client import SESSION_FILE
from .snapshots import atomic_write_text

_FX_CACHE = SESSION_FILE.parent / "cache" / "usdjpy.json"
_SERIES_API = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=JPY"


def iso_date(d) -> str:
    """Ledger dates come as '2026.05.26'; frankfurter + the cache use ISO
    '2026-05-26'. Normalize so lookups and API ranges agree."""
    return str(d or "").strip().replace(".", "-").replace("/", "-")


def _load() -> tuple[dict, list]:
    """(rates, covered) from the disk cache. `covered` lists [start, end] ISO
    ranges already fetched; the pre-coverage format was a bare {date: rate}."""
    try:
        raw = json.loads(_FX_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, []
    if isinstance(raw, dict) and isinstance(raw.get("rates"), dict):
        return ({iso_date(k): v for k, v in raw["rates"].items()},
                [list(r) for r in raw.get("covered") or [] if len(r) == 2])
    if isinstance(raw, dict):
        return {iso_date(k): v for k, v in raw.items()}, []
    return {}, []


def _missing(start: str, end: str, covered: list) -> tuple[str, str] | None:
    """The smallest [start, end] hull not covered by `covered`, or None."""
    days, d0, d1 = [], date.fromisoformat(start), date.fromisoformat(end)
    while d0 <= d1:
        ds = d0.isoformat()
        if not any(a <= ds <= b for a, b in covered):
            days.append(ds)
        d0 += timedelta(days=1)
    return (days[0], days[-1]) if days else None


def usdjpy_series(start: str, end: str) -> dict:
    """{date: usd/jpy} (ISO keys), fetching ONLY the part of the range the disk
    cache hasn't covered yet. Days before today are final and are recorded as
    covered; today stays open so a later run can pick up its rate. On a network
    failure, returns the cached data alone."""
    rates, covered = _load()
    start, end = iso_date(start), iso_date(end)
    try:
        gap = _missing(start, end, covered)
    except ValueError:                # unparseable date → nothing sensible to fetch
        return rates
    if gap is None:
        return rates
    try:
        r = requests.get(_SERIES_API.format(start=gap[0], end=gap[1]), timeout=20)
        r.raise_for_status()
        fresh = {iso_date(d): v["JPY"] for d, v in (r.json().get("rates") or {}).items() if "JPY" in v}
    except (requests.RequestException, ValueError, KeyError):
        return rates                  # offline / blocked → use cache only
    rates.update(fresh)
    final_end = min(gap[1], (date.today() - timedelta(days=1)).isoformat())
    if gap[0] <= final_end:
        covered.append([gap[0], final_end])
    try:
        atomic_write_text(_FX_CACHE, json.dumps({"rates": rates, "covered": covered},
                                                ensure_ascii=False))
    except OSError:
        pass
    return rates


def mid_for(series: dict, date: str):
    """Exact rate for the date, else the nearest preceding business-day rate
    (ECB omits weekends/holidays). Dates in either '2026.05.26' or ISO form."""
    d = iso_date(date)
    norm = {iso_date(k): v for k, v in series.items()}
    if d in norm:
        return norm[d]
    earlier = [k for k in norm if k <= d]
    return norm[max(earlier)] if earlier else None
