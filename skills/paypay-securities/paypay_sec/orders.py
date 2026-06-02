"""Order model + placement pipeline for PayPay証券 (US stocks first).

Pipeline: build(args) -> OrderRequest ; then dry_run()/place() run
guard -> confirm -> [interactive] -> submit. Writes never retry; confirm
tokens are single-use. The agent never runs the live submit (human only).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from .client import normalize_market
from .guards import check_guards, TradeConfig, GuardContext


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
    account_type: int = 2          # 2=特定(taxable, cash) 3=成長投資枠NISA 4=つみたて


# brokerage account-type aliases (the web's ACCOUNT_TYPE field)
ACCOUNT_TYPES = {"特定": 2, "tokutei": 2, "taxable": 2, "cash": 2,
                 "nisa": 3, "成長": 3, "growth": 3,
                 "つみたて": 4, "tsumitate": 4}


def build(*, market: str, symbol: str, side: str, qty: Optional[float] = None,
          amount_jpy: Optional[int] = None, limit: Optional[float] = None,
          market_order: bool = False, account: Optional[str] = None,
          account_type: int = 2) -> OrderRequest:
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
    # PayPay US orders are 金額指定/株数指定 executed at the prevailing quote — a per-
    # order limit price is OPTIONAL (give --limit only for a 指値 order; otherwise the
    # order fills at market). --market-order forces 成行 and forbids a limit price.
    ot = OrderType.MARKET if market_order else OrderType.LIMIT
    if ot is OrderType.MARKET and limit is not None:
        raise OrderError("a market order must not carry a limit price")
    if limit is not None and limit <= 0:
        raise OrderError("--limit must be positive")
    if account_type not in (2, 3, 4):
        raise OrderError("account_type must be 2(特定) | 3(成長投資枠) | 4(つみたて)")
    return OrderRequest(market=normalize_market(market), symbol=sym, side=side_e,
                        order_type=ot, qty=qty, amount_jpy=amount_jpy,
                        limit_price=limit, account=account, account_type=account_type)


@dataclass
class PipelineResult:
    ok: bool                       # guards passed (and, for place(), eligible to submit)
    violations: List[str] = field(default_factory=list)
    preview: Optional[dict] = None
    submitted: bool = False
    order_id: Optional[str] = None
    aborted_reason: Optional[str] = None


def _run_guards_and_confirm(req: OrderRequest, cfg: TradeConfig, ctx: GuardContext,
                            confirm: Callable[[OrderRequest], dict]) -> PipelineResult:
    pre = check_guards(req, cfg, ctx, preview=None)
    if pre:
        return PipelineResult(ok=False, violations=pre)
    preview = confirm(req)                                  # network or mock; NO retry inside
    post = check_guards(req, cfg, ctx, preview=preview)
    if post:
        return PipelineResult(ok=False, violations=post, preview=preview)
    return PipelineResult(ok=True, violations=[], preview=preview)


def dry_run(req: OrderRequest, cfg: TradeConfig, ctx: GuardContext,
            confirm: Callable[[OrderRequest], dict]) -> PipelineResult:
    """build -> guards -> confirm. Never submits."""
    return _run_guards_and_confirm(req, cfg, ctx, confirm)


def place(req: OrderRequest, cfg: TradeConfig, ctx: GuardContext,
          confirm: Callable[[OrderRequest], dict],
          submit: Callable[[str, OrderRequest], dict],
          confirmer: Callable[[OrderRequest, dict], bool]) -> PipelineResult:
    """Full path. Submits only if guards pass AND confirmer() returns True.
    `submit` is called at most once and is never retried by this layer."""
    res = _run_guards_and_confirm(req, cfg, ctx, confirm)
    if not res.ok:
        return res
    if not confirmer(req, res.preview):
        res.aborted_reason = "user did not confirm"
        return res
    token = (res.preview or {}).get("token")
    if not token:
        res.ok = False
        res.aborted_reason = "confirm returned no token"
        return res
    out = submit(token, req)                                 # single-shot, no retry
    res.submitted = True
    res.order_id = (out or {}).get("order_id")
    res.preview = {**(res.preview or {}), "submit_response": out}
    return res
