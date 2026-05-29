"""Review aggregation — turns the raw transaction ledger into FACTUAL metrics:
deposits/withdrawals, per-brand buy/sell/net, and realized P&L (average-cost
basis). No rules, thresholds, judgments, or buy/sell advice — just the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass

_FEE_TYPES = ("手数料/税", "手数料")


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
