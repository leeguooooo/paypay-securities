"""Order model + placement pipeline for PayPay証券 (US stocks first).

Pipeline: build(args) -> OrderRequest ; then dry_run()/place() run
guard -> confirm -> [interactive] -> submit. Writes never retry; confirm
tokens are single-use. The agent never runs the live submit (human only).

The web order flow only supports 金額指定 (a JPY amount, amountTyp=0) and the
order fills at the prevailing quote (成行相当) — there is no share-count or
limit-price order here, so the model carries only the yen amount.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional

from .client import normalize_market, OrderOutcomeUnknown
from .guards import check_guards, TradeConfig, GuardContext


class OrderError(ValueError):
    """The order request is malformed or a guard rejected it."""


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OrderRequest:
    market: str
    symbol: str
    side: Side
    amount_jpy: int
    account: Optional[str] = None
    account_type: int = 2          # 2=特定(taxable, cash) 3=成長投資枠NISA 4=つみたて


# brokerage account-type aliases (the web's ACCOUNT_TYPE field)
ACCOUNT_TYPES = {"特定": 2, "tokutei": 2, "taxable": 2, "cash": 2,
                 "nisa": 3, "成長": 3, "growth": 3,
                 "つみたて": 4, "tsumitate": 4}
ACCOUNT_TYPE_NAMES = {2: "特定", 3: "成長投資枠NISA", 4: "つみたて"}


def account_type_name(code: int) -> str:
    return ACCOUNT_TYPE_NAMES.get(code, str(code))


def _amount(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise OrderError("--amount must be a yen integer")
    if not math.isfinite(v) or v <= 0:
        raise OrderError("--amount must be a positive, finite yen amount")
    if isinstance(v, float) and not v.is_integer():
        raise OrderError("--amount must be a whole yen amount")
    return int(v)


def build(*, market: str, symbol: str, side: str, amount_jpy,
          account: Optional[str] = None, account_type: int = 2) -> OrderRequest:
    try:
        side_e = Side(side.strip().lower())
    except ValueError as e:
        raise OrderError(f"unknown side {side!r} (use buy|sell)") from e
    sym = (symbol or "").strip().upper()
    if not sym:
        raise OrderError("symbol is required")
    if amount_jpy is None:
        raise OrderError("--amount (JPY, 金額指定) is required")
    amt = _amount(amount_jpy)
    if account_type not in ACCOUNT_TYPE_NAMES:
        raise OrderError("account_type must be 2(特定) | 3(成長投資枠) | 4(つみたて)")
    return OrderRequest(market=normalize_market(market), symbol=sym, side=side_e,
                        amount_jpy=amt, account=account, account_type=account_type)


@dataclass
class PipelineResult:
    ok: bool                       # guards passed (and, for place(), eligible to submit)
    violations: List[str] = field(default_factory=list)
    preview: Optional[dict] = None
    submitted: bool = False
    order_id: Optional[str] = None
    aborted_reason: Optional[str] = None
    outcome_unknown: bool = False  # submit was sent but its result could not be read


def dry_run(req: OrderRequest, cfg: TradeConfig, ctx: GuardContext,
            confirm: Callable[[OrderRequest], dict]) -> PipelineResult:
    """build -> guards -> confirm. Never submits."""
    pre = check_guards(req, cfg, ctx, preview=None)
    if pre:
        return PipelineResult(ok=False, violations=pre)
    preview = confirm(req)                                  # network or mock; NO retry inside
    post = check_guards(req, cfg, ctx, preview=preview)
    if post:
        return PipelineResult(ok=False, violations=post, preview=preview)
    return PipelineResult(ok=True, violations=[], preview=preview)


def place(req: OrderRequest, cfg: TradeConfig, ctx: GuardContext,
          confirm: Callable[[OrderRequest], dict],
          submit: Callable[[str, OrderRequest], dict],
          confirmer: Callable[[OrderRequest, dict], bool]) -> PipelineResult:
    """Full path. Submits only if guards pass AND confirmer() returns True.
    `submit` is called at most once and is never retried by this layer."""
    res = dry_run(req, cfg, ctx, confirm)
    if not res.ok:
        return res
    if not confirmer(req, res.preview):
        res.ok = False
        res.aborted_reason = "confirmation phrase did not match"
        return res
    # a 予約注文 preview has no server ORDER_CONFIRM_NO (assigned at complete); the
    # client stashes it under a synthetic token, so an empty token is always an error
    token = (res.preview or {}).get("token")
    if not token:
        res.ok = False
        res.aborted_reason = "confirm returned no token"
        return res
    try:
        out = submit(token, req)                             # single-shot, no retry
    except OrderOutcomeUnknown as e:
        res.ok = False
        res.outcome_unknown = True
        res.aborted_reason = str(e)
        return res
    res.submitted = True
    res.order_id = (out or {}).get("order_id") or None
    res.preview = {**(res.preview or {}), "submit_response": out}
    return res
