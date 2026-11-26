"""Review aggregation — turns the raw transaction ledger into FACTUAL metrics:
deposits/withdrawals, per-brand buy/sell/net, and realized P&L (moving-average
cost, 移動平均法 — what JP 特定口座 actually uses). No rules, thresholds,
judgments, or buy/sell advice — just the numbers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_FEE_TYPES = ("手数料/税", "手数料")


def _year(date: str) -> str | None:
    """Calendar year from a ledger date ('2026.05.27' or '2026-05-27')."""
    m = re.match(r"(\d{4})", date or "")
    return m.group(1) if m else None


def _ym(date: str) -> str | None:
    """Year-month bucket ('2026.05' or '2026-05') from a ledger date."""
    m = re.match(r"(\d{4})[.\-/](\d{2})", date or "")
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _realized_moving_avg(events) -> tuple[int, bool, int]:
    """移動平均法 realized P&L over CHRONOLOGICAL (type, qty, signed_amount) events
    (買付 amount < 0 = cash out, 売却 amount > 0 = proceeds). Unlike a whole-window
    average this stays correct when buys and sells interleave (re-buy after a sell),
    and it matches the cost basis JP brokers report for 特定口座.

    Returns (realized_yen, ok, remaining_cost_yen); ok=False if a sell ever exceeds
    the shares held so far — the cost basis is incomplete in the fetched window, so
    realized is an under-estimate and callers should flag it rather than trust it
    blindly. remaining_cost is the moving-average cost of the shares still held
    (NOT buy−sell, which goes wrong after a partial sell at a gain/loss)."""
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
    return round(realized), ok, round(cost) if shares > 1e-9 else 0


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
        return _realized_moving_avg(self.events)[:2]

    @property
    def remaining_cost(self) -> int:
        """Moving-average cost basis of the shares still held."""
        return _realized_moving_avg(self.events)[2]

    @property
    def realized_pl(self) -> int:
        return self.realized()[0]

    def to_dict(self) -> dict:
        return {"name": self.name, "brand": self.brand, "acct": self.acct,
                "buy_yen": self.buy_yen, "buy_shares": round(self.buy_shares, 6),
                "sell_yen": self.sell_yen, "sell_shares": round(self.sell_shares, 6),
                "net_invested": self.net_invested, "net_shares": self.net_shares,
                "realized_pl": self.realized_pl, "reconciles": self.realized()[1],
                "remaining_cost": self.remaining_cost}


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


def tsumitate_runrate(invtrust_txns: list[dict]) -> list[dict]:
    """Infer the 定投/つみたて run-rate per fund from EXECUTED buys (the configured
    amount/cycle is not exposed by the web API). Looks at 買付 rows whose 口座 is a
    つみたて 枠. monthly_estimate = total ÷ distinct year-months seen (つみたて is
    monthly). FACTS from real executions — low confidence when months is small."""
    by: dict[str, list] = {}
    for t in invtrust_txns:
        if t.get("type") == "買付" and "つみたて" in (t.get("account_type") or "") and t.get("brand"):
            by.setdefault(t["brand"], []).append((t.get("date") or "", abs(t.get("amount") or 0)))
    out = []
    for brand, rows in by.items():
        rows = sorted(r for r in rows if r[0])
        if not rows:
            continue
        months = {_ym(d) for d, _ in rows if _ym(d)}
        total = sum(a for _, a in rows)
        monthly = round(total / len(months)) if months else 0
        out.append({
            "brand": brand, "buys": len(rows), "months": len(months),
            "total_invested": total, "monthly_estimate": monthly,
            "annualized": monthly * 12,
            "last_date": rows[-1][0], "last_amount": rows[-1][1],
        })
    return sorted(out, key=lambda x: -x["total_invested"])


def xirr(cashflows: list[tuple], guess: float = 0.1):
    """Money-weighted annualized return (XIRR) for [(date_str, amount)]. Sign: money
    INTO the portfolio (deposits) NEGATIVE, money OUT (withdrawals) + final current
    value POSITIVE. Returns the annual rate as a fraction (0.12 = +12%/yr) or None
    if unsolvable / too little data. Newton's method + bisection fallback."""
    from datetime import date as _date

    def parse(d):
        m = re.match(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})", d or "")
        return _date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

    flows = [(parse(d), float(a)) for d, a in cashflows if parse(d) and a]
    if len(flows) < 2:
        return None
    t0 = min(d for d, _ in flows)
    yrs = [((d - t0).days / 365.0, a) for d, a in flows]
    if not (any(a < 0 for _, a in yrs) and any(a > 0 for _, a in yrs)):
        return None

    def npv(r):
        return sum(a / (1.0 + r) ** t for t, a in yrs)

    r = guess
    for _ in range(100):
        f = npv(r)
        d = sum(-t * a / (1.0 + r) ** (t + 1) for t, a in yrs)
        if abs(d) < 1e-9:
            break
        nr = r - f / d
        if nr <= -0.999999:
            nr = (r - 0.999999) / 2
        if abs(nr - r) < 1e-7:
            return round(nr, 4)
        r = nr
    lo, hi = -0.9999, 10.0
    flo, fhi = npv(lo), npv(hi)
    if flo * fhi > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        fm = npv(mid)
        if abs(fm) < 1e-6:
            return round(mid, 4)
        if flo * fm < 0:
            hi = mid
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2, 4)


def tax_summary(sec_txns: list[dict], inv_txns: list[dict]) -> list[dict]:
    """Per calendar-year tax view (FACTS): 売却 proceeds, 譲渡益税 withheld, 分配金.
    Tax year = calendar year of the ledger date. No advice — just the figures the
    年間取引報告書 would show."""
    years: dict[str, dict] = {}

    def row(y):
        return years.setdefault(y, {"year": y, "sec_sell": 0, "inv_sell": 0,
                                    "capital_gains_tax": 0, "distributions": 0})

    for t in inv_txns:
        y = _year(t.get("date"))
        if not y:
            continue
        if t["type"] == "譲渡益税" and t.get("amount"):
            row(y)["capital_gains_tax"] += -t["amount"]
        elif t["type"] == "分配金" and t.get("amount"):
            row(y)["distributions"] += t["amount"]
        elif t["type"] == "売却" and t.get("amount"):
            row(y)["inv_sell"] += abs(t["amount"])
    for t in sec_txns:
        y = _year(t.get("date"))
        if not y:
            continue
        if t["type"] == "売却" and t.get("amount"):
            row(y)["sec_sell"] += abs(t["amount"])
    return [years[y] for y in sorted(years)]


def _snap_date(snap: dict) -> str | None:
    """ISO date (YYYY-MM-DD) of a snapshot, from its ts stem ('20260604-120608')
    or, failing that, the as_of string ('2026-06-04 12:06 JST')."""
    ts = str(snap.get("ts") or "")
    if len(ts) >= 8 and ts[:8].isdigit():
        return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
    a = str(snap.get("as_of") or "")[:10]
    return a if re.match(r"\d{4}-\d{2}-\d{2}", a) else None


def _dietz_pct(pnl: int, start_total: int, net_flow: int) -> float | None:
    denom = (start_total or 0) + (net_flow or 0) / 2
    return round(pnl / denom * 100, 2) if denom > 0 else None


def _cash_flow_comparable(a: dict, b: dict) -> bool:
    """Only verified full ledgers with known cumulative cash flow are comparable."""
    return (a.get("ledger_full") is True and b.get("ledger_full") is True
            and a.get("net_deposit") is not None and b.get("net_deposit") is not None
            and not (a.get("deposits") is not None and b.get("deposits") is not None
                     and b["deposits"] < a["deposits"]))


def trend_summary(snaps: list[dict], since: str | None = None) -> dict:
    """Facts-only trend over a snapshot series: start→end change with deposits
    stripped out, plus daily/weekly stats from daily_pnl_series.

    `since` (YYYY-MM-DD) bounds the window; the baseline is the last usable
    snapshot on or before it (else the first one after it). Unusable snapshots
    (failed sources / no total) are ignored. Pure: no I/O."""
    from datetime import date

    def usable(s):
        return (isinstance(s, dict) and s.get("grand_total") is not None
                and not any(v == "failed" for v in (s.get("sources") or {}).values()))

    good = sorted((s for s in snaps or [] if usable(s) and _snap_date(s)),
                  key=lambda s: str(s.get("ts") or ""))
    if not good:
        return {"start": None, "end": None, "days": [], "weeks": [], "stats": None}
    base_idx = 0
    if since:
        # Baseline = last usable snapshot on/before `since` — but only if it is close
        # to it; a months-old one would turn "last 30 days" into "since the outage".
        before = [i for i, s in enumerate(good) if _snap_date(s) <= since]
        after = [i for i, s in enumerate(good) if _snap_date(s) > since]
        if before and _gap_days(_snap_date(good[before[-1]]), since) <= MAX_GAP_DAYS:
            base_idx = before[-1]
        elif after:
            base_idx = after[0]
        else:
            base_idx = before[-1]
    window = good[base_idx:]
    start, end = window[0], window[-1]

    def point(s):
        return {"date": _snap_date(s), "total": s.get("grand_total"),
                "net_deposit": s.get("net_deposit"), "realized_total": s.get("realized_total")}

    comparable = _cash_flow_comparable(start, end)
    net_flow = end["net_deposit"] - start["net_deposit"] if comparable else None
    change = end["grand_total"] - start["grand_total"]
    pnl = change - net_flow if comparable else None
    start_total = start.get("grand_total") or 0

    series = daily_pnl_series(window)
    start_d = _snap_date(start)
    days = [{"date": d, **v} for d, v in sorted(series.items()) if d > start_d]
    counted = [x for x in days if x.get("pnl") is not None]
    weeks: dict[str, dict] = {}
    for x in counted:
        y, w, _ = date.fromisoformat(x["date"]).isocalendar()
        k = f"{y}-W{w:02d}"
        wk = weeks.setdefault(k, {"week": k, "pnl": 0, "days": 0})
        wk["pnl"] += x["pnl"]
        wk["days"] += 1
    best = max(counted, key=lambda x: x["pnl"], default=None)
    worst = min(counted, key=lambda x: x["pnl"], default=None)
    return {
        "start": point(start), "end": point(end),
        "change": change, "net_flow": net_flow, "pnl": pnl,
        # simple Dietz: deposits during the window count as invested for half of it,
        # so a big mid-window 入金 doesn't inflate/deflate the percentage
        "pnl_pct": _dietz_pct(pnl, start_total, net_flow) if comparable else None,
        "comparable": comparable,
        "days": days,
        "weeks": [weeks[k] for k in sorted(weeks)],
        "stats": {
            "counted_days": len(counted),
            "up_days": sum(1 for x in counted if x["pnl"] > 0),
            "down_days": sum(1 for x in counted if x["pnl"] < 0),
            "missing_days": sum(1 for x in days if x.get("pnl") is None),
            "gaps": [{"from": x.get("range_from"), "to": x["date"], "range_pnl": x.get("range_pnl")}
                     for x in days if x.get("range_from")],
            "best": {"date": best["date"], "pnl": best["pnl"]} if best else None,
            "worst": {"date": worst["date"], "pnl": worst["pnl"]} if worst else None,
        },
    }


def _missing_day(note: str) -> dict:
    return {"pnl": None, "pnl_pct": None, "total_assets": None, "net_flow": None,
            "realized": None, "data_quality": "missing", "note": note}


# A gap longer than this between two usable snapshots (calendar days) is too long
# to book as one day's P&L: weekends + a holiday stay under it, an outage doesn't.
MAX_GAP_DAYS = 7


def _gap_days(d_from: str, d_to: str) -> int:
    from datetime import date
    try:
        return (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days
    except (TypeError, ValueError):
        return 0


def daily_pnl_series(snapshots: list[dict]) -> dict[str, dict]:
    """Per-day mark-to-market P&L from a series of account snapshots.

    Each snapshot carries cumulative figures (grand_total, net_deposit,
    realized_total). The day's *investment* gain/loss is the change in total
    assets MINUS the day's external cash flow, so deposits/withdrawals don't read
    as profit:

        pnl[d]      = (grand_total[d] - grand_total[prev]) - net_flow[d]
        net_flow[d] =  net_deposit[d] - net_deposit[prev]
        realized[d] =  realized_total[d] - realized_total[prev]

    Multiple snapshots on one calendar day collapse to the last (closing) one.
    A snapshot with no grand_total, or with any source "failed", is emitted as
    data_quality="missing" and does NOT advance the baseline, so the next good
    day's pnl spans the gap. The first good day has no baseline and is omitted.

    net_flow is only meaningful when both snapshots summed the SAME ledger span.
    An incomplete/unknown ledger or cumulative deposits going DOWN makes the
    cash flow unreliable — so the day is emitted as missing and the newer
    snapshot becomes the baseline. Incomplete or unmarked ledgers and missing
    net deposits are not sufficient evidence to calculate cash-adjusted P&L.

    A gap of more than MAX_GAP_DAYS since the previous usable snapshot (e.g. a
    months-long login outage) is NOT booked as that day's P&L: the day is emitted
    as missing with the whole span's change in `range_pnl` / `range_from`, and
    becomes the new baseline.

    Returns {date: {pnl, pnl_pct, total_assets, net_flow, realized,
    data_quality, note}} — the per-account shape the calendar consumes.
    """
    by_date: dict[str, dict] = {}
    for s in snapshots or []:
        d = _snap_date(s)
        if not d:
            continue
        # last snapshot of the day wins (closing); tie-break by ts string
        if d not in by_date or str(s.get("ts") or "") >= str(by_date[d].get("ts") or ""):
            by_date[d] = s

    out: dict[str, dict] = {}
    prev = None  # last snapshot with a usable grand_total
    for d in sorted(by_date):
        s = by_date[d]
        gt = s.get("grand_total")
        src = s.get("sources") or {}
        if gt is None:
            out[d] = _missing_day(s.get("note") or "当天数据抓取失败")
            continue
        if any(v == "failed" for v in src.values()):
            out[d] = _missing_day("部分数据源抓取失败,当天总资产不可信")
            continue
        if prev is None:
            prev = s            # baseline only — no pnl for the first good day
            continue
        pd = _snap_date(prev)
        gap = _gap_days(pd, d)
        if gap > MAX_GAP_DAYS:
            net_flow = (s.get("net_deposit") or 0) - (prev.get("net_deposit") or 0)
            dep, pdep = s.get("deposits"), prev.get("deposits")
            span_ok = _cash_flow_comparable(prev, s)
            note = f"距上次有效快照({pd}){gap} 天,区间盈亏不计入单日"
            if not span_ok:
                note += ";账本覆盖范围变化,区间盈亏无法剔除现金流"
            out[d] = {**_missing_day(note), "range_from": pd, "total_assets": gt,
                      "range_pnl": ((gt - (prev.get("grand_total") or 0)) - net_flow) if span_ok else None}
            prev = s
            continue
        dep, pdep = s.get("deposits"), prev.get("deposits")
        if not _cash_flow_comparable(prev, s):
            out[d] = _missing_day("账本覆盖范围变化,当天盈亏无法剔除现金流")
            prev = s            # comparable from here on
            continue
        pgt = prev.get("grand_total") or 0
        net_flow = (s.get("net_deposit") or 0) - (prev.get("net_deposit") or 0)
        pnl = (gt - pgt) - net_flow
        realized = None
        if s.get("realized_total") is not None and prev.get("realized_total") is not None:
            realized = s["realized_total"] - prev["realized_total"]
        quality = "full"
        if any(v not in ("ok", "live", None)
               for snap in (prev, s) for v in (snap.get("sources") or {}).values()):
            quality = "partial"
        out[d] = {
            "pnl": pnl,
            "pnl_pct": round(pnl / pgt * 100, 2) if pgt else None,
            "total_assets": gt,
            "net_flow": net_flow,
            "realized": realized,
            "data_quality": quality,
            "note": "当日有现金流,盈亏已剔除" if net_flow else None,
        }
        prev = s
    return out
