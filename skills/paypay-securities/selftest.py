#!/usr/bin/env python3
"""Self-contained regression tests for the 投信 ledger + moving-average realized
P&L. Pure SYNTHETIC data (no account PII) so it is safe to commit — unlike the
real-fixture tests under the gitignored repo-root tests/.

Run:  uv run --project skills/paypay-securities python skills/paypay-securities/selftest.py
"""
from __future__ import annotations

import sys

from paypay_sec import parsers, report

_fails: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        _fails.append(label)


def _inv_rec(seq, date, st, amt, *, qty=None, price=None, acct=None, brand=None):
    """A CO_TRADE_HIST-shaped 投信 row (MARKET_ID=99)."""
    return {"SEQ_NO": str(seq), "BASE_D": date, "SUMMARY_TYPE": str(st),
            "AMOUNT": str(amt), "QTY": qty, "PRICE": price,
            "ACCOUNT_TYPE": acct, "BRAND_NM": brand, "CASH_BALANCE": "0"}


def test_parse_invtrust_transactions() -> None:
    print("parse_invtrust_transactions:")
    S = "Example Fund"          # synthetic — no real holding/PII
    recs = [
        _inv_rec(1, "2026-01-09", 54, "-110.00000"),                                # 送金手数料
        _inv_rec(2, "2026-01-08", 8, "-200.00000"),                                 # 譲渡益税
        _inv_rec(3, "2026-01-07", 2, "12000.00000", qty=300, price="40000", acct=2, brand=S),  # 売却 特定
        _inv_rec(4, "2026-01-05", 1, "-9000.00000", qty=200, acct=4, brand=S),      # 買付 NISAつみたて
        _inv_rec(5, "2026-01-04", 31, "0", brand=None),                             # 約定明細 → DROP
    ]
    txns = parsers.parse_invtrust_transactions(recs)
    check("type 31 dropped (row count)", len(txns), 4)
    by = {t["type"]: t for t in txns}
    check("code 8 → 譲渡益税", "譲渡益税" in by, True)
    check("code 54 → 送金手数料", "送金手数料" in by, True)
    check("ACCOUNT_TYPE 2 → 特定", by["売却"]["account_type"], "特定")
    check("ACCOUNT_TYPE 4 → NISAつみたて", by["買付"]["account_type"], "NISAつみたて")
    check("買付 amount sign preserved", by["買付"]["amount"], -9000)


def test_moving_average_vs_window() -> None:
    print("moving-average realized (interleaved buy→sell→buy):")
    S = "X"
    # newest-first ledger order: buy@2000 (newest), sell, buy@1000 (oldest)
    recs = [
        _inv_rec(3, "2026-05-20", 1, "-20000.00000", qty=10, acct=2, brand=S),  # buy 10 @2000
        _inv_rec(2, "2026-05-10", 2, "12000.00000", qty=10, acct=2, brand=S),   # sell 10 @ proceeds 12000
        _inv_rec(1, "2026-05-01", 1, "-10000.00000", qty=10, acct=2, brand=S),  # buy 10 @1000
    ]
    agg = report.aggregate_invtrust(parsers.parse_invtrust_transactions(recs))
    # moving-average: basis at sell time = 1000/unit → realized = 12000 - 10*1000 = +2000.
    # a whole-window average would mix in the later 2000/unit buy → ~+1000 (WRONG).
    check("interleaved realized = +2000 (not window-avg +1000)", agg["realized_pl"], 2000)
    check("interleaved reconciles", agg["reconciles"], True)


def test_real_round_trip_and_costs() -> None:
    print("特定 round-trip + cost line items:")
    S = "Example Fund"          # synthetic — no real holding/PII
    recs = [
        _inv_rec(5, "2026-01-08", 8, "-200.00000"),                          # 譲渡益税
        _inv_rec(4, "2026-01-09", 54, "-110.00000"),                         # 送金手数料
        _inv_rec(3, "2026-01-07", 2, "12000.00000", qty=100, acct=2, brand=S),  # 売却 100口
        _inv_rec(2, "2026-01-05", 1, "-4000.00000", qty=40, acct=2, brand=S),   # 買付
        _inv_rec(1, "2026-01-05", 1, "-6000.00000", qty=60, acct=2, brand=S),   # 買付 (40+60=100)
    ]
    agg = report.aggregate_invtrust(parsers.parse_invtrust_transactions(recs))
    check("round-trip realized = 12000-10000 = +2000", agg["realized_pl"], 2000)
    check("reconciles (sold == bought 口数)", agg["reconciles"], True)
    check("譲渡益税 summed positive", agg["capital_gains_tax"], 200)
    check("送金手数料 summed positive", agg["transfer_fees"], 110)


def test_reconciles_flag_guards_missing_basis() -> None:
    print("reconciles=False when a sell has no recorded basis:")
    recs = [_inv_rec(1, "2026-05-01", 2, "5000.00000", qty=100, acct=2, brand="Y")]
    agg = report.aggregate_invtrust(parsers.parse_invtrust_transactions(recs))
    check("no-basis sell flagged", agg["reconciles"], False)


def test_securities_moving_average() -> None:
    print("証券 aggregate_trades also uses moving-average:")
    # newest-first: buy@200, sell, buy@100  → realized = 1200 - 10*100 = +200
    txns_newest_first = [
        {"type": "買付", "brand": "T", "qty": 10, "amount": -2000, "date": "2026-05-20"},
        {"type": "売却", "brand": "T", "qty": 10, "amount": 1200, "date": "2026-05-10"},
        {"type": "買付", "brand": "T", "qty": 10, "amount": -1000, "date": "2026-05-01"},
    ]
    agg = report.aggregate_trades(txns_newest_first)
    check("証券 interleaved realized = +200", agg["realized_pl"], 200)
    check("証券 reconciles", agg["reconciles"], True)


def main() -> int:
    for t in (test_parse_invtrust_transactions, test_moving_average_vs_window,
              test_real_round_trip_and_costs, test_reconciles_flag_guards_missing_basis,
              test_securities_moving_average):
        t()
    print()
    if _fails:
        print(f"❌ {len(_fails)} FAILED: {', '.join(_fails)}")
        return 1
    print("✅ all self-tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
