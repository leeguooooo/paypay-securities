"""Trade configuration + operational safety guards (pure logic, no network).

These guards prevent fat-finger / over-limit mistakes. They are NOT investment
rules or advice. Config lives at ~/.paypay-sec/[<account>/]trade.json and is
never committed. Missing/broken config -> conservative defaults that reject.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .audit import COUNT_UNREADABLE
from .client import state_dir

DEFAULT_TRADE_CONFIG = {
    "max_order_jpy": 50000,
    "max_pct_of_portfolio": 10,
    "allow_symbols": [],          # empty = no symbol restriction
    "allow_markets": ["usa"],
    "daily_order_cap": 5,
    "trading_hours": None,        # null = unrestricted; else {market: {tz, windows}}
}
# Retired keys (price_collar_pct, allow_market_order) are ignored when an old
# trade.json still carries them: orders are 金額指定 filled at the quote, so there
# is no limit price to collar and no separate market-order mode to allow.


@dataclass(frozen=True)
class TradeConfig:
    max_order_jpy: int = DEFAULT_TRADE_CONFIG["max_order_jpy"]
    max_pct_of_portfolio: float = DEFAULT_TRADE_CONFIG["max_pct_of_portfolio"]
    allow_symbols: List[str] = field(default_factory=lambda: list(DEFAULT_TRADE_CONFIG["allow_symbols"]))
    allow_markets: List[str] = field(default_factory=lambda: list(DEFAULT_TRADE_CONFIG["allow_markets"]))
    daily_order_cap: int = DEFAULT_TRADE_CONFIG["daily_order_cap"]
    trading_hours: Optional[dict] = None


def _config_path(account: Optional[str]) -> Path:
    return state_dir(account) / "trade.json"


def _pos_num(v, default, cast):
    """A finite, positive number (bools and strings rejected) cast with `cast`,
    else `default` — a typo in trade.json must never loosen a guard."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return default
    v = cast(v)
    return v if v > 0 else default


def _str_list(v, default, norm):
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        return list(default)
    return [norm(x) for x in v]


def load_trade_config(account: Optional[str] = None, *, config_path: Optional[Path] = None) -> TradeConfig:
    path = config_path or _config_path(account)
    d = DEFAULT_TRADE_CONFIG
    data = dict(d)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in d})
    except (OSError, ValueError):
        pass  # fail-safe: keep conservative defaults
    return TradeConfig(
        max_order_jpy=_pos_num(data["max_order_jpy"], d["max_order_jpy"], int),
        max_pct_of_portfolio=_pos_num(data["max_pct_of_portfolio"], d["max_pct_of_portfolio"], float),
        allow_symbols=_str_list(data["allow_symbols"], d["allow_symbols"], str.upper),
        allow_markets=_str_list(data["allow_markets"], d["allow_markets"], str.lower),
        daily_order_cap=_pos_num(data["daily_order_cap"], d["daily_order_cap"], int),
        trading_hours=data["trading_hours"],   # validated (fail-closed) in check_guards
    )


def write_default_trade_config(account: Optional[str] = None, *, config_path: Optional[Path] = None) -> Path:
    path = config_path or _config_path(account)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_TRADE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@dataclass(frozen=True)
class GuardContext:
    """External facts the guards need; injected so the guards stay pure/testable."""
    now: Optional[datetime] = None
    today_order_count: int = 0
    portfolio_total_jpy: Optional[int] = None     # None = unknown -> max_pct fails closed


def _within_hours(now: datetime, market: str, trading_hours: dict) -> Optional[bool]:
    """True/False if a window is defined for the market, else None (no opinion).
    A malformed window spec is False (fail closed).

    Only same-day windows are supported; a cross-midnight window like
    ["22:00", "02:00"] is NOT handled by the string HH:MM comparison.
    """
    spec = trading_hours.get(market)
    if not spec or now is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(spec.get("tz", "UTC")))
        hm = local.strftime("%H:%M")
        for start, end in spec.get("windows", []):
            if str(start) <= hm <= str(end):
                return True
    except Exception:  # noqa: BLE001 — bad tz / malformed windows -> block
        return False
    return False


def check_guards(req, cfg: TradeConfig, ctx: GuardContext, preview: Optional[dict] = None) -> List[str]:
    """Return a list of violation strings (empty = pass). Runs every check on both
    passes. The order amount is the request's yen amount pre-confirm (preview None)
    and the server's ORDER_AMOUNT (preview total_jpy) post-confirm."""
    v: List[str] = []

    if cfg.allow_markets and req.market not in cfg.allow_markets:
        v.append(f"market '{req.market}' not in allow_markets {cfg.allow_markets}")
    if cfg.allow_symbols and req.symbol not in cfg.allow_symbols:
        v.append(f"symbol '{req.symbol}' not in allow_symbols")
    if ctx.today_order_count >= COUNT_UNREADABLE:
        v.append("daily order count unavailable (audit log unreadable) — blocking")
    elif ctx.today_order_count >= cfg.daily_order_cap:
        v.append(f"daily order cap reached ({ctx.today_order_count}/{cfg.daily_order_cap})")

    # trading hours
    if cfg.trading_hours is not None and not isinstance(cfg.trading_hours, dict):
        v.append("trading_hours in trade.json is malformed — blocking")
    elif cfg.trading_hours:
        ok = _within_hours(ctx.now, req.market, cfg.trading_hours)
        if ok is False:
            v.append(f"outside configured trading hours for {req.market}")

    # amount / pct
    total = (preview or {}).get("total_jpy")
    if total is None:
        total = req.amount_jpy
    if total is not None:
        if total > cfg.max_order_jpy:
            v.append(f"order total ¥{total:,} exceeds max_order_jpy ¥{cfg.max_order_jpy:,}")
        if ctx.portfolio_total_jpy is None or ctx.portfolio_total_jpy <= 0:
            v.append("portfolio total unavailable — cannot check max_pct_of_portfolio")
        else:
            pct = 100.0 * total / ctx.portfolio_total_jpy
            if pct > cfg.max_pct_of_portfolio:
                v.append(f"order is {pct:.1f}% of portfolio (> max {cfg.max_pct_of_portfolio}%)")
    return v
