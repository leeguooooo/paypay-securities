"""Cost analysis over the transaction ledger.

Surfaces PayPay's true trading cost, which it never itemizes:
  - explicit fees  : ledger 手数料/税 + 手数料 lines (deposit fees, etc.)
  - FX spread      : applied 為替レート vs real market USD/JPY mid (measured)
  - price spread   : 0.5%/0.7% baked into 約定価格 — NOT measured here (needs
                     intraday equity quotes); reported only as a note/estimate.
"""
from __future__ import annotations

from . import market

FEE_TYPES = ("手数料/税", "手数料")
TRADE_TYPES = ("買付", "売却")


def compute_costs(transactions: list[dict], series: dict) -> dict:
    explicit_total = sum(t["amount"] for t in transactions
                         if t["type"] in FEE_TYPES and t["amount"])   # negative
    fx_rows = []
    for t in transactions:
        if t["type"] in TRADE_TYPES and t["price"] and t["qty"] and t["fx"]:
            usd = t["price"] * t["qty"]
            mid = market.mid_for(series, t["date"])
            if not mid:
                continue
            # buy converts JPY->USD at applied>mid; sell USD->JPY at applied<mid;
            # either way the deviation in PayPay's favor is your cost.
            diff = (t["fx"] - mid) if t["type"] == "買付" else (mid - t["fx"])
            fx_rows.append({
                "date": t["date"], "side": t["type"], "brand": t["brand"],
                "usd": round(usd, 2), "applied_fx": t["fx"], "market_mid": mid,
                "spread_per_usd": round(diff, 4), "cost": round(usd * diff),
            })
    fx_total = sum(r["cost"] for r in fx_rows)
    usd_notional = round(sum(r["usd"] for r in fx_rows), 2)
    explicit = -explicit_total if explicit_total else 0
    return {
        "explicit_fees": explicit,
        "fx_spread_cost": fx_total,
        "fx_trades": len(fx_rows),
        "usd_notional": usd_notional,
        "avg_spread_per_usd": round(fx_total / usd_notional, 4) if usd_notional else None,
        "measured_total": explicit + fx_total,
        "fx_rows": fx_rows,
        "fx_available": bool(fx_rows),
    }


def price_spread_estimate(usd_notional: float, mid_fx: float, pct: float) -> int:
    """Rough price-spread cost: PayPay's disclosed % on US-stock 約定価格, applied
    to the JPY turnover. Estimate only (the % varies by market hours)."""
    return round(usd_notional * mid_fx * pct / 100.0)
