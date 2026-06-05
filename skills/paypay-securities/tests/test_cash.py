"""Shared-cash-pool recency: 証券 + 投信 ledgers must not double-count cash.

Regression for the 2026-06 double-count: an 投信-only buy debits the shared cash
pool, the 投信 ledger reflects the post-buy balance, but the 証券 ledger lags a day
still showing the pre-buy cash. Asset totals must take the *most-recent* balance
across both ledgers — not blindly the 証券 one — or the spent cash is counted twice
(once inside the now-larger fund holding, once as still-held cash).
"""
from _runner import run
from paypay_sec import parsers


# 投信 buy of ¥47,539 on 06-04 drained the shared pool to ¥0. The 証券 ledger's
# newest row is still 06-03 (the deposit that funded it), so it lags at ¥47,539.
SEC = [
    {"BASE_D": "2026.06.03", "SEQ_NO": 100, "SUMMARY_TYPE": "3", "CASH_BALANCE": 47539},
    {"BASE_D": "2026.06.01", "SEQ_NO": 90, "SUMMARY_TYPE": "3", "CASH_BALANCE": 0},
]
INV = [
    {"BASE_D": "2026.06.04", "SEQ_NO": 105, "SUMMARY_TYPE": "1", "CASH_BALANCE": 0},      # 買付
    {"BASE_D": "2026.06.03", "SEQ_NO": 100, "SUMMARY_TYPE": "3", "CASH_BALANCE": 47539},  # shared 入金 row
]


def test_old_single_ledger_reads_stale_cash():
    # documents the bug: 証券-only read trusts the lagging ledger.
    assert parsers.current_cash(SEC) == 47539


def test_combined_takes_most_recent_across_ledgers():
    # the fix: the 06-04 投信 row is newest → true current cash is ¥0.
    assert parsers.current_cash_combined(SEC, INV) == 0


def test_securities_wins_when_it_is_newer():
    # a recent 証券 buy with no newer 投信 activity → 証券 balance is current.
    sec = [{"BASE_D": "2026.06.05", "SEQ_NO": 120, "SUMMARY_TYPE": "1", "CASH_BALANCE": 3000}]
    inv = [{"BASE_D": "2026.06.04", "SEQ_NO": 105, "SUMMARY_TYPE": "1", "CASH_BALANCE": 0}]
    assert parsers.current_cash_combined(sec, inv) == 3000


def test_same_day_tie_broken_by_seq_no():
    # both ledgers stamped 06-04; the higher SEQ_NO (account-wide sequence) is later.
    sec = [{"BASE_D": "2026.06.04", "SEQ_NO": 110, "SUMMARY_TYPE": "1", "CASH_BALANCE": 5000}]
    inv = [{"BASE_D": "2026.06.04", "SEQ_NO": 112, "SUMMARY_TYPE": "1", "CASH_BALANCE": 0}]
    assert parsers.current_cash_combined(sec, inv) == 0


def test_skips_rows_without_balance_and_handles_empty():
    assert parsers.current_cash_combined([], []) is None
    assert parsers.current_cash_combined(None) is None
    recs = [{"BASE_D": "2026.06.04", "SEQ_NO": 1, "CASH_BALANCE": ""},
            {"BASE_D": "2026.06.03", "SEQ_NO": 2, "CASH_BALANCE": 999}]
    assert parsers.current_cash_combined(recs) == 999


if __name__ == "__main__":
    raise SystemExit(run(globals()))
