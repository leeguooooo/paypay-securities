"""Portfolio review aggregation — turns the raw ledger + holdings into review
metrics. FACTUAL only: deposits, per-brand buy/sell/net, realized P&L (average-
cost basis), unrealized P&L, costs, concentration, and PASS/over-limit checks
against USER-DEFINED rules. It does NOT give buy/sell advice or evaluate strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json

from .config import HOME

# risk thresholds (% of total assets). Override via ~/.paypay-sec/rules.json.
DEFAULT_RULES = {
    "single_stock_max_pct": 15.0,    # any one individual stock
    "leveraged_max_pct": 5.0,        # leveraged/inverse ETFs (TQQQ etc.)
    "us_equity_max_pct": 90.0,       # total US-equity exposure
    "cash_min_pct": 0.0,             # minimum cash as % of total
}
# name fragments that mark a leveraged / inverse product
_LEVERAGED = ("ウルトラ", "プロシェアーズ", "3倍", "2倍", "レバ", "ブル", "ベア",
              "Direxion", "TQQQ", "SQQQ", "ProShares")
_FEE_TYPES = ("手数料/税", "手数料")


def load_rules() -> dict:
    rules = dict(DEFAULT_RULES)
    try:
        rules.update(json.loads((HOME / "rules.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return rules


_ETF = ("ETF", "QQQ", "インベスコ", "バンガード", "iシェアーズ", "SPDR", "ヴァンエック",
        "State Street", "Direxion", "プロシェアーズ")


def is_leveraged(name: str) -> bool:
    return any(k in (name or "") for k in _LEVERAGED)


def is_etf(name: str) -> bool:
    return any(k in (name or "") for k in _ETF)


def classify(name: str, category: str) -> str:
    """fund (投信) | leveraged | etf | stock — for concentration rules."""
    if category == "投信":
        return "fund"
    if is_leveraged(name):
        return "leveraged"
    if is_etf(name):
        return "etf"
    return "stock"


@dataclass
class BrandFlow:
    name: str
    buy_yen: int = 0
    buy_shares: float = 0.0
    sell_yen: int = 0
    sell_shares: float = 0.0

    @property
    def net_invested(self) -> int:
        return self.buy_yen - self.sell_yen

    @property
    def net_shares(self) -> float:
        return round(self.buy_shares - self.sell_shares, 10)

    @property
    def avg_buy_price(self):
        return (self.buy_yen / self.buy_shares) if self.buy_shares else None

    @property
    def realized_pl(self):
        """Average-cost realized P&L on the shares sold."""
        if not self.sell_shares or not self.avg_buy_price:
            return 0
        return round(self.sell_yen - self.sell_shares * self.avg_buy_price)

    def to_dict(self) -> dict:
        return {"name": self.name, "buy_yen": self.buy_yen, "buy_shares": round(self.buy_shares, 6),
                "sell_yen": self.sell_yen, "sell_shares": round(self.sell_shares, 6),
                "net_invested": self.net_invested, "net_shares": self.net_shares,
                "realized_pl": self.realized_pl}


def aggregate_trades(transactions: list[dict]) -> dict:
    deposits = sum(t["amount"] for t in transactions if t["type"] == "入金" and t["amount"])
    withdrawals = -sum(t["amount"] for t in transactions if t["type"] == "出金" and t["amount"])
    fees = -sum(t["amount"] for t in transactions if t["type"] in _FEE_TYPES and t["amount"])
    flows: dict[str, BrandFlow] = {}
    for t in transactions:
        if t["type"] not in ("買付", "売却") or not t["brand"]:
            continue
        f = flows.setdefault(t["brand"], BrandFlow(t["brand"]))
        amt, qty = t["amount"] or 0, t["qty"] or 0
        if t["type"] == "買付":
            f.buy_yen += -amt          # buy AMOUNT is negative (cash out)
            f.buy_shares += qty
        else:
            f.sell_yen += amt
            f.sell_shares += qty
    realized_total = sum(f.realized_pl for f in flows.values())
    dates = [t["date"] for t in transactions if t["date"]]
    return {
        "deposits": deposits, "withdrawals": withdrawals, "explicit_fees": fees,
        "realized_pl": realized_total,
        "brands": [f.to_dict() for f in sorted(flows.values(), key=lambda x: -x.buy_yen)],
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
    }


def evaluate_risk(holdings: list[dict], total_assets: int, cash: int, rules: dict) -> dict:
    """holdings: [{name, category, valuation, is_stock}]. Returns factual metrics +
    PASS/over-limit checks vs the user's rules. total_assets includes cash."""
    base = total_assets or 1
    def pct(v):
        return round(100.0 * v / base, 1)

    stocks = [(h["name"], h["valuation"] or 0) for h in holdings if h.get("is_stock")]
    top_stock = max(stocks, key=lambda x: x[1], default=(None, 0))
    leveraged_val = sum((h["valuation"] or 0) for h in holdings if is_leveraged(h["name"]))
    us_equity_val = sum((h["valuation"] or 0) for h in holdings)  # all holdings are US equity here
    cash_pct = pct(cash)

    checks = [
        {"rule": "単一個別株 上限", "metric": f"{top_stock[0]} {pct(top_stock[1])}%",
         "limit": f"{rules['single_stock_max_pct']}%",
         "ok": pct(top_stock[1]) <= rules["single_stock_max_pct"]},
        {"rule": "レバレッジ商品 上限", "metric": f"{pct(leveraged_val)}%",
         "limit": f"{rules['leveraged_max_pct']}%",
         "ok": pct(leveraged_val) <= rules["leveraged_max_pct"]},
        {"rule": "米国株式エクスポージャー 上限", "metric": f"{pct(us_equity_val)}%",
         "limit": f"{rules['us_equity_max_pct']}%",
         "ok": pct(us_equity_val) <= rules["us_equity_max_pct"]},
        {"rule": "現金 下限", "metric": f"{cash_pct}%",
         "limit": f"{rules['cash_min_pct']}%",
         "ok": cash_pct >= rules["cash_min_pct"]},
    ]
    return {
        "top_stock": {"name": top_stock[0], "pct": pct(top_stock[1])},
        "leveraged_pct": pct(leveraged_val),
        "us_equity_pct": pct(us_equity_val),
        "cash_pct": cash_pct,
        "checks": checks,
        "breaches": [c for c in checks if not c["ok"]],
    }
