"""Order model + placement pipeline for PayPay証券 (US stocks first).

Pipeline: build(args) -> OrderRequest ; then dry_run()/place() run
guard -> confirm -> [interactive] -> submit. Writes never retry; confirm
tokens are single-use. The agent never runs the live submit (human only).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from .client import normalize_market


class OrderError(ValueError):
    """The order request is malformed or a guard rejected it."""


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"


@dataclass(frozen=True)
class OrderRequest:
    market: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: Optional[float] = None
    amount_jpy: Optional[int] = None
    limit_price: Optional[float] = None
    account: Optional[str] = None


def build(*, market: str, symbol: str, side: str, qty: Optional[float] = None,
          amount_jpy: Optional[int] = None, limit: Optional[float] = None,
          market_order: bool = False, account: Optional[str] = None) -> OrderRequest:
    try:
        side_e = Side(side.strip().lower())
    except ValueError as e:
        raise OrderError(f"unknown side {side!r} (use buy|sell)") from e
    sym = (symbol or "").strip().upper()
    if not sym:
        raise OrderError("symbol is required")
    if (qty is None) == (amount_jpy is None):
        raise OrderError("specify exactly one of --qty or --amount")
    if qty is not None and qty <= 0:
        raise OrderError("--qty must be positive")
    if amount_jpy is not None and amount_jpy <= 0:
        raise OrderError("--amount must be positive")
    ot = OrderType.MARKET if market_order else OrderType.LIMIT
    if ot is OrderType.MARKET:
        if limit is not None:
            raise OrderError("a market order must not carry a limit price")
    else:
        if limit is None:
            raise OrderError("a limit order requires --limit PRICE (or pass --market-order)")
        if limit <= 0:
            raise OrderError("--limit must be positive")
    return OrderRequest(market=normalize_market(market), symbol=sym, side=side_e,
                        order_type=ot, qty=qty, amount_jpy=amount_jpy,
                        limit_price=limit, account=account)
