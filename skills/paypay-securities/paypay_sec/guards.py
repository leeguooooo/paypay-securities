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
