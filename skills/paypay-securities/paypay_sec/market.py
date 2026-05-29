"""Real market USD/JPY mid rates (ECB daily reference via frankfurter.dev).

Used to benchmark PayPay's applied exchange rate and surface the hidden FX
spread. Historical rates are immutable, so they are cached forever on disk.
Network failures degrade gracefully (returns whatever is cached).
"""
from __future__ import annotations

import json
from pathlib import Path

import requests

from .client import SESSION_FILE

_FX_CACHE = SESSION_FILE.parent / "cache" / "usdjpy.json"
_SERIES_API = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=JPY"


def _load() -> dict:
    try:
        return json.loads(_FX_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def usdjpy_series(start: str, end: str) -> dict:
    """{date: usd/jpy} for the range, merging fresh ECB data into the disk cache.
    On a network failure, returns the cached data alone."""
    cache = _load()
    try:
        r = requests.get(_SERIES_API.format(start=start, end=end), timeout=20)
        r.raise_for_status()
        fresh = {d: v["JPY"] for d, v in (r.json().get("rates") or {}).items() if "JPY" in v}
        if fresh:
            cache.update(fresh)
            _FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            _FX_CACHE.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    except (requests.RequestException, ValueError, KeyError):
        pass  # offline / blocked → use cache only
    return cache


def mid_for(series: dict, date: str):
    """Exact rate for the date, else the nearest preceding business-day rate
    (ECB omits weekends/holidays)."""
    if date in series:
        return series[date]
    earlier = [d for d in series if d <= date]
    return series[max(earlier)] if earlier else None
