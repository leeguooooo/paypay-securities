"""Trade configuration + operational safety guards (pure logic, no network).

These guards prevent fat-finger / over-limit mistakes. They are NOT investment
rules or advice. Config lives at ~/.paypay-sec/[<account>/]trade.json and is
never committed. Missing/broken config -> conservative defaults that reject.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .client import state_dir

DEFAULT_TRADE_CONFIG = {
    "max_order_jpy": 50000,
    "max_pct_of_portfolio": 10,
    "allow_symbols": [],          # empty = no symbol restriction
    "allow_markets": ["usa"],
    "daily_order_cap": 5,
    "price_collar_pct": 5,
    "allow_market_order": False,
    "trading_hours": None,        # null = unrestricted; else {market: {tz, windows}}
}


@dataclass(frozen=True)
class TradeConfig:
    max_order_jpy: int = DEFAULT_TRADE_CONFIG["max_order_jpy"]
    max_pct_of_portfolio: float = DEFAULT_TRADE_CONFIG["max_pct_of_portfolio"]
    allow_symbols: List[str] = field(default_factory=lambda: list(DEFAULT_TRADE_CONFIG["allow_symbols"]))
    allow_markets: List[str] = field(default_factory=lambda: list(DEFAULT_TRADE_CONFIG["allow_markets"]))
    daily_order_cap: int = DEFAULT_TRADE_CONFIG["daily_order_cap"]
    price_collar_pct: float = DEFAULT_TRADE_CONFIG["price_collar_pct"]
    allow_market_order: bool = DEFAULT_TRADE_CONFIG["allow_market_order"]
    trading_hours: Optional[dict] = None


def _config_path(account: Optional[str]) -> Path:
    return state_dir(account) / "trade.json"


def load_trade_config(account: Optional[str] = None, *, config_path: Optional[Path] = None) -> TradeConfig:
    path = config_path or _config_path(account)
    data = dict(DEFAULT_TRADE_CONFIG)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data.update({k: v for k, v in loaded.items() if k in DEFAULT_TRADE_CONFIG})
    except (OSError, ValueError):
        pass  # fail-safe: keep conservative defaults
    return TradeConfig(
        max_order_jpy=int(data["max_order_jpy"]),
        max_pct_of_portfolio=float(data["max_pct_of_portfolio"]),
        allow_symbols=[str(s).upper() for s in (data["allow_symbols"] or [])],
        allow_markets=[str(s).lower() for s in (data["allow_markets"] or [])],
        daily_order_cap=int(data["daily_order_cap"]),
        price_collar_pct=float(data["price_collar_pct"]),
        allow_market_order=bool(data["allow_market_order"]),
        trading_hours=data["trading_hours"],
    )


def write_default_trade_config(account: Optional[str] = None, *, config_path: Optional[Path] = None) -> Path:
    path = config_path or _config_path(account)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_TRADE_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


from dataclasses import dataclass as _dc

@_dc(frozen=True)
class GuardContext:
    """External facts the guards need; injected so the guards stay pure/testable."""
    now: Optional[datetime] = None
    today_order_count: int = 0
    current_quote: Optional[float] = None        # for the price collar (same currency as limit)
    portfolio_total_jpy: Optional[int] = None     # for max_pct check (with a preview)


def _within_hours(now: datetime, market: str, trading_hours: dict) -> Optional[bool]:
    """True/False if a window is defined for the market, else None (no opinion)."""
    spec = trading_hours.get(market)
    if not spec or now is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(spec.get("tz", "UTC")))
    except Exception:  # noqa: BLE001 — tz lookup failure -> don't block
        return None
    hm = local.strftime("%H:%M")
    for start, end in spec.get("windows", []):
        if start <= hm <= end:
            return True
    return False


def check_guards(req, cfg: TradeConfig, ctx: GuardContext, preview: Optional[dict] = None) -> List[str]:
    """Return a list of violation strings (empty = pass). Runs every check whose
    inputs are available; checks needing a `preview` (amount/pct) are skipped when
    preview is None (pre-confirm pass) and enforced when it is given (post-confirm)."""
    from .orders import OrderType
    v: List[str] = []

    if cfg.allow_markets and req.market not in cfg.allow_markets:
        v.append(f"market '{req.market}' not in allow_markets {cfg.allow_markets}")
    if cfg.allow_symbols and req.symbol not in cfg.allow_symbols:
        v.append(f"symbol '{req.symbol}' not in allow_symbols")
    if req.order_type is OrderType.MARKET and not cfg.allow_market_order:
        v.append("market order blocked (set allow_market_order=true to permit)")
    if ctx.today_order_count >= cfg.daily_order_cap:
        v.append(f"daily order cap reached ({ctx.today_order_count}/{cfg.daily_order_cap})")

    # price collar — only if we have both a limit and a current quote
    if req.limit_price is not None and ctx.current_quote:
        dev = abs(req.limit_price - ctx.current_quote) / ctx.current_quote * 100.0
        if dev > cfg.price_collar_pct:
            v.append(f"price collar: limit deviates {dev:.1f}% from quote "
                     f"{ctx.current_quote} (> {cfg.price_collar_pct}%)")

    # trading hours
    if cfg.trading_hours:
        ok = _within_hours(ctx.now, req.market, cfg.trading_hours)
        if ok is False:
            v.append(f"outside configured trading hours for {req.market}")

    # amount / pct — authoritative only with a confirm preview
    if preview is not None:
        total = preview.get("total_jpy")
        if total is not None:
            if total > cfg.max_order_jpy:
                v.append(f"order total ¥{total:,} exceeds max_order_jpy ¥{cfg.max_order_jpy:,}")
            if ctx.portfolio_total_jpy:
                pct = 100.0 * total / ctx.portfolio_total_jpy
                if pct > cfg.max_pct_of_portfolio:
                    v.append(f"order is {pct:.1f}% of portfolio (> max {cfg.max_pct_of_portfolio}%)")
    return v
