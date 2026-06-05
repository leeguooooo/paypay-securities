"""P&L calendar: daily mark-to-market series from snapshots + payload assembly.

The calendar's numbers come from snapshot diffs: a day's gain/loss is the change
in total assets MINUS that day's external cash flow, so deposits never read as
profit. These tests pin that math + the edge cases (first day, missing snapshot,
gap spanning).
"""
from _runner import run
from paypay_sec import report, cli


def _snap(ts, gt, nd=0, rz=0, **extra):
    return {"ts": ts, "as_of": ts, "grand_total": gt, "net_deposit": nd,
            "realized_total": rz, **extra}


def test_daily_pnl_excludes_cash_flow():
    snaps = [
        _snap("20260601-120000", 500000, nd=480000),
        _snap("20260602-120000", 505000, nd=480000),          # +5000 pure market
        _snap("20260603-120000", 560000, nd=510000, rz=200),  # +55000 assets, +30000 deposit → pnl +25000
    ]
    s = report.daily_pnl_series(snaps)
    assert "2026-06-01" not in s                # first day = baseline, no pnl
    assert s["2026-06-02"]["pnl"] == 5000
    assert s["2026-06-02"]["net_flow"] == 0
    # 2026-06-03: assets +55000, but 30000 was a deposit → market pnl = 25000
    assert s["2026-06-03"]["pnl"] == 25000
    assert s["2026-06-03"]["net_flow"] == 30000
    assert s["2026-06-03"]["note"] == "当日有现金流,盈亏已剔除"
    assert s["2026-06-03"]["realized"] == 200


def test_pnl_pct_against_prior_total():
    snaps = [_snap("20260601-120000", 100000), _snap("20260602-120000", 101500)]
    s = report.daily_pnl_series(snaps)
    assert s["2026-06-02"]["pnl"] == 1500
    assert s["2026-06-02"]["pnl_pct"] == 1.5


def test_missing_snapshot_and_gap_spanning():
    snaps = [
        _snap("20260601-120000", 500000),
        {"ts": "20260602-120000", "as_of": "20260602", "grand_total": None,
         "net_deposit": 0, "realized_total": None},            #抓取失败
        _snap("20260603-120000", 506000),
    ]
    s = report.daily_pnl_series(snaps)
    assert s["2026-06-02"]["data_quality"] == "missing"
    assert s["2026-06-02"]["pnl"] is None
    # the missing day must NOT advance the baseline → 06-03 spans 06-01→06-03
    assert s["2026-06-03"]["pnl"] == 6000


def test_last_snapshot_of_day_wins():
    snaps = [
        _snap("20260601-090000", 500000),
        _snap("20260602-090000", 503000),
        _snap("20260602-160000", 505000),    # closing snapshot of 06-02
    ]
    s = report.daily_pnl_series(snaps)
    assert s["2026-06-02"]["total_assets"] == 505000
    assert s["2026-06-02"]["pnl"] == 5000


def test_failed_source_marks_partial():
    snaps = [
        _snap("20260601-120000", 500000),
        _snap("20260602-120000", 502000, sources={"securities": "ok", "invtrust": "failed"}),
    ]
    s = report.daily_pnl_series(snaps)
    assert s["2026-06-02"]["data_quality"] == "partial"


def test_calendar_payload_shape_and_total_aggregation_inputs():
    leo = [_snap("20260601-120000", 500000), _snap("20260602-120000", 506000)]
    lisa = [_snap("20260601-120000", 300000), _snap("20260602-120000", 299000)]
    payload = cli._calendar_payload({"leo": leo, "lisa": lisa})
    assert payload["currency"] == "JPY"
    assert payload["accounts"] == ["leo", "lisa"]
    assert payload["schema_version"]
    days = {d["date"]: d for d in payload["days"]}
    assert "2026-06-01" not in days                  # both accounts' baseline day
    d2 = days["2026-06-02"]["accounts"]
    assert d2["leo"]["pnl"] == 6000
    assert d2["lisa"]["pnl"] == -1000
    # 合计 is computed in the viewer JS (6000 + -1000 = 5000) — payload stays per-account


def test_empty_snapshots_give_empty_calendar():
    payload = cli._calendar_payload({"leo": [], "lisa": []})
    assert payload["days"] == []
    assert payload["accounts"] == ["leo", "lisa"]


if __name__ == "__main__":
    raise SystemExit(run(globals()))
