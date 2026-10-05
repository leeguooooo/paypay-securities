from _runner import run
from paypay_sec import report


# ---------------------------------------------------------- tsumitate_runrate
def test_tsumitate_runrate():
    txns = [
        {"type": "買付", "brand": "S&P500", "account_type": "NISAつみたて", "date": "2026-04-01", "amount": -20000},
        {"type": "買付", "brand": "S&P500", "account_type": "NISAつみたて", "date": "2026-05-01", "amount": -20000},
        {"type": "買付", "brand": "S&P500", "account_type": "NISA成長", "date": "2026-05-02", "amount": -100000},  # not つみたて
        {"type": "買付", "brand": "Gold", "account_type": "NISAつみたて", "date": "2026-05-01", "amount": -5000},
        {"type": "売却", "brand": "S&P500", "account_type": "NISAつみたて", "date": "2026-05-03", "amount": 1000},
    ]
    rr = {r["brand"]: r for r in report.tsumitate_runrate(txns)}
    assert set(rr) == {"S&P500", "Gold"}                 # only つみたて buys, sells/成長 excluded
    assert rr["S&P500"]["months"] == 2 and rr["S&P500"]["total_invested"] == 40000
    assert rr["S&P500"]["monthly_estimate"] == 20000     # 40000 / 2 months
    assert rr["S&P500"]["annualized"] == 240000
    assert rr["Gold"]["monthly_estimate"] == 5000
    # sorted by total invested desc
    assert report.tsumitate_runrate(txns)[0]["brand"] == "S&P500"


# ---------------------------------------------------------- xirr
def test_xirr_simple_10pct():
    # deposit 100 (money IN = negative) one year before a 110 value (positive)
    r = report.xirr([("2025-01-01", -100), ("2026-01-01", 110)])
    assert r is not None and abs(r - 0.10) < 0.005


def test_xirr_multi_deposit_positive():
    # two deposits then a higher current value → positive money-weighted return
    r = report.xirr([("2025-01-01", -100), ("2025-07-01", -100), ("2026-01-01", 230)])
    assert r is not None and r > 0


def test_xirr_needs_both_signs():
    assert report.xirr([("2025-01-01", -100), ("2026-01-01", -50)]) is None  # all inflow
    assert report.xirr([("2025-01-01", -100)]) is None                        # too few


# ---------------------------------------------------------- tax_summary
def test_tax_summary_by_year():
    inv = [
        {"type": "売却", "brand": "X", "date": "2025-03-01", "amount": 50000},
        {"type": "譲渡益税", "date": "2025-03-01", "amount": -1000},
        {"type": "分配金", "date": "2025-06-01", "amount": 800},
        {"type": "売却", "brand": "X", "date": "2026-02-01", "amount": 30000},
    ]
    sec = [
        {"type": "売却", "brand": "TSLA", "date": "2026-01-15", "amount": 70000},
        {"type": "買付", "brand": "TSLA", "date": "2026-01-10", "amount": -70000},  # ignored
    ]
    rows = {r["year"]: r for r in report.tax_summary(sec, inv)}
    assert set(rows) == {"2025", "2026"}
    assert rows["2025"]["inv_sell"] == 50000 and rows["2025"]["capital_gains_tax"] == 1000
    assert rows["2025"]["distributions"] == 800
    assert rows["2026"]["inv_sell"] == 30000 and rows["2026"]["sec_sell"] == 70000


def test_partial_sell_unrealized_uses_moving_average_cost():
    """Buy 100口 @¥10,000, buy 100口 @¥14,000 (avg ¥12,000), sell 100口 for ¥15,000.
    Remaining cost is 100 × 12,000 = 1,200,000 — NOT buy−sell (2.4M − 1.5M = 0.9M)."""
    from types import SimpleNamespace
    from paypay_sec import cli

    recs = [  # newest first, like the feed
        {"BASE_D": "2026.06.03", "SUMMARY_TYPE": "2", "BRAND_NM": "テストファンド",
         "ACCOUNT_TYPE": "2", "QTY": "100", "AMOUNT": "1500000"},
        {"BASE_D": "2026.06.02", "SUMMARY_TYPE": "1", "BRAND_NM": "テストファンド",
         "ACCOUNT_TYPE": "2", "QTY": "100", "AMOUNT": "-1400000"},
        {"BASE_D": "2026.06.01", "SUMMARY_TYPE": "1", "BRAND_NM": "テストファンド",
         "ACCOUNT_TYPE": "2", "QTY": "100", "AMOUNT": "-1000000"},
    ]
    fake = SimpleNamespace(
        invtrust_settlement_records=lambda max_pages: recs,
        invtrust_top=lambda: {"INVEST_BRAND_ARRAY": [
            {"BRAND_ID": "9", "SECURITIES_VALUE": "1300000", "SUM_GROSS_PROFIT": "100000"}]},
        invtrust_brands=lambda: {"9": "テストファンド"})
    holdings, agg, _ = cli._invtrust_lots(fake, 1)
    (h,) = holdings
    assert agg["realized_pl"] == 300000            # 1.5M − 100 × 12,000
    assert h["cost"] == 1200000
    assert h["unrealized_pl"] == 100000            # 1.3M − 1.2M (not 1.3M − 0.9M)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
