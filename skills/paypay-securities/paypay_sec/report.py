"""Review aggregation — turns the raw transaction ledger into FACTUAL metrics:
deposits/withdrawals, per-brand buy/sell/net, and realized P&L (moving-average
cost, 移動平均法 — what JP 特定口座 actually uses). No rules, thresholds,
judgments, or buy/sell advice — just the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

_FEE_TYPES = ("手数料/税", "手数料")


def _realized_moving_avg(events) -> tuple[int, bool]:
    """移動平均法 realized P&L over CHRONOLOGICAL (type, qty, signed_amount) events
    (買付 amount < 0 = cash out, 売却 amount > 0 = proceeds). Unlike a whole-window
    average this stays correct when buys and sells interleave (re-buy after a sell),
    and it matches the cost basis JP brokers report for 特定口座.

    Returns (realized_yen, ok); ok=False if a sell ever exceeds the shares held so
    far — the cost basis is incomplete in the fetched window, so realized is an
    under-estimate and callers should flag it rather than trust it blindly."""
    shares = cost = realized = 0.0
    ok = True
    for typ, qty, amt in events:
        qty = qty or 0
        amt = amt or 0
        if typ == "買付":
            shares += qty
            cost += -amt
        elif typ == "売却":
            if shares <= 1e-9:                 # sold with no basis on record
                ok = False
                realized += amt
                continue
            if qty > shares + 1e-9:            # selling more than we can account for
                ok = False
            avg = cost / shares
            sold = min(qty, shares)
            realized += amt - sold * avg
            cost -= sold * avg
            shares -= sold
    return round(realized), ok


@dataclass
class BrandFlow:
    name: str
    buy_yen: int = 0
    buy_shares: float = 0.0
    sell_yen: int = 0
    sell_shares: float = 0.0
    brand: str = ""          # bare fund name (no [口座] suffix)
    acct: str = ""           # 口座区分 (特定 / NISA成長 / …), 投信 only
    events: list = field(default_factory=list)   # chronological (type, qty, amount)

    @property
    def net_invested(self) -> int:
        return self.buy_yen - self.sell_yen

    @property
    def net_shares(self) -> float:
        return round(self.buy_shares - self.sell_shares, 10)

    @property
    def avg_buy_price(self):
        return (self.buy_yen / self.buy_shares) if self.buy_shares else None

    def realized(self) -> tuple[int, bool]:
        """(realized_yen, reconciles) via moving-average cost."""
        return _realized_moving_avg(self.events)

    @property
    def realized_pl(self) -> int:
        return self.realized()[0]

    def to_dict(self) -> dict:
        return {"name": self.name, "brand": self.brand, "acct": self.acct,
                "buy_yen": self.buy_yen, "buy_shares": round(self.buy_shares, 6),
                "sell_yen": self.sell_yen, "sell_shares": round(self.sell_shares, 6),
                "net_invested": self.net_invested, "net_shares": self.net_shares,
                "realized_pl": self.realized_pl, "reconciles": self.realized()[1]}


def _add_trade(flows: dict, key, name: str, t: dict, brand: str = "", acct: str = "") -> None:
    """Fold one 買付/売却 row into its lot, preserving chronological event order
    (caller must iterate oldest-first)."""
    f = flows.setdefault(key, BrandFlow(name, brand=brand, acct=acct))
    amt, qty = t["amount"] or 0, t["qty"] or 0
    if t["type"] == "買付":
        f.buy_yen += -amt          # buy AMOUNT is negative (cash out)
        f.buy_shares += qty
    else:
        f.sell_yen += amt
        f.sell_shares += qty
    f.events.append((t["type"], qty, amt))


def aggregate_trades(transactions: list[dict]) -> dict:
    deposits = sum(t["amount"] for t in transactions if t["type"] == "入金" and t["amount"])
    withdrawals = -sum(t["amount"] for t in transactions if t["type"] == "出金" and t["amount"])
    fees = -sum(t["amount"] for t in transactions if t["type"] in _FEE_TYPES and t["amount"])
    flows: dict[str, BrandFlow] = {}
    # ledger is newest-first; reverse so moving-average sees buys/sells in order
    for t in reversed(transactions):
        if t["type"] not in ("買付", "売却") or not t["brand"]:
            continue
        _add_trade(flows, t["brand"], t["brand"], t)
    realized_total = sum(f.realized_pl for f in flows.values())
    reconciles = all(f.realized()[1] for f in flows.values())
    dates = [t["date"] for t in transactions if t["date"]]
    return {
        "deposits": deposits, "withdrawals": withdrawals, "explicit_fees": fees,
        "realized_pl": realized_total, "reconciles": reconciles,
        "brands": [f.to_dict() for f in sorted(flows.values(), key=lambda x: -x.buy_yen)],
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
    }


def aggregate_invtrust(transactions: list[dict]) -> dict:
    """投信 (MARKET_ID=99) ledger aggregation. Moving-average realized P&L keyed by
    (brand, account_type) because 特定 / NISA成長 / NISAつみたて are separate tax lots
    with separate cost bases. 譲渡益税 (capital-gains tax withheld on 特定 sells) and
    送金手数料 (per-即時入金 transfer fees) are summed as factual costs.

    `reconciles` is False when a sell exceeds the shares bought in some lot within
    the fetched window (cost basis incomplete → realized under-estimated)."""
    tax = -sum(t["amount"] for t in transactions if t["type"] == "譲渡益税" and t["amount"])
    transfer_fees = -sum(t["amount"] for t in transactions if t["type"] == "送金手数料" and t["amount"])
    deposits_gross = sum(t["amount"] for t in transactions if t["type"] == "入金" and t["amount"])
    distributions = sum(t["amount"] for t in transactions if t["type"] == "分配金" and t["amount"])
    flows: dict[tuple, BrandFlow] = {}
    for t in reversed(transactions):           # oldest-first for moving average
        if t["type"] not in ("買付", "売却") or not t["brand"]:
            continue
        acct = t.get("account_type") or "?"
        _add_trade(flows, (t["brand"], acct), f'{t["brand"]} [{acct}]', t,
                   brand=t["brand"], acct=acct)
    realized = sum(f.realized_pl for f in flows.values())
    reconciles = all(f.realized()[1] for f in flows.values())
    dates = [t["date"] for t in transactions if t["date"]]
    return {
        "realized_pl": realized, "reconciles": reconciles,
        "capital_gains_tax": tax, "transfer_fees": transfer_fees,
        "deposits_gross": deposits_gross, "distributions": distributions,
        "buys_yen": sum(f.buy_yen for f in flows.values()),
        "sells_yen": sum(f.sell_yen for f in flows.values()),
        "brands": [f.to_dict() for f in sorted(flows.values(), key=lambda x: -x.buy_yen)],
        "date_from": min(dates) if dates else None,
        "date_to": max(dates) if dates else None,
    }
