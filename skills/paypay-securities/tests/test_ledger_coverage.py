"""Synthetic ledger pagination and cash-flow coverage regressions (offline)."""
from types import SimpleNamespace
from _runner import run
from paypay_sec import cli, report
from paypay_sec.client import PayPayClient


def _page(records, next_flag):
    return {"CO_TRADE_HIST": [{"SEQ_NO": i} for i in records], "NEXT_FLG": next_flag}


def _fetch(pages, cap):
    c = object.__new__(PayPayClient)
    c.ensure_session = lambda: None
    c._cached = lambda key, fetch, cache_if: pages[int(key.split('=')[-1]) // 20]
    return c._paged_ledger('unused{off}', '/', 'test', 'TEST', cap)


def _snap(day, total, net=100, **extra):
    return {"ts": f"202606{day:02d}-120000", "grand_total": total,
            "net_deposit": net, "ledger_full": True, **extra}


def test_pagination_completion_and_public_list_contract():
    for pages, cap, expected in [
        ([_page(range(20), False)], 1, True),
        ([_page(range(20), True)], 1, False),
        ([_page(range(20), True), _page([], False)], 2, False),
        ([_page(range(20), True), _page(range(20), False)], 2, False),
        ([_page(range(20), True), _page(range(20, 25), False)], 2, True),
        ([{"CO_TRADE_HIST": [{"SEQ_NO": 1}]}], 1, False),
        ([_page([], False)], 1, False),
    ]:
        rows = _fetch(pages, cap)
        assert isinstance(rows, list) and rows.complete is expected, (pages, rows.complete)


def test_gather_uses_terminal_marker_not_record_count():
    full = _fetch([_page(range(20), False)], 1)
    partial = _fetch([_page(range(5), True)], 1)
    original = cli._fetch_account
    try:
        def gather(sec, inv):
            cli._fetch_account = lambda client, pages, inv_pages: {
                'ledger': sec, 'invledger': inv, 'rows': [], 'cash': 0,
                'cash_fresh': True, 'sources': {}}
            return cli._gather(None, pages=1)['ledger_complete']
        assert gather(full, full) is True  # exactly one full terminal page
        assert gather(partial, full) is False  # short but nonterminal
        assert gather(full, list(full)) is False  # unknown provenance
    finally:
        cli._fetch_account = original


def test_incomplete_missing_and_downward_deposit_fail_closed():
    base = _snap(1, 1000, deposits=100)
    for replacement in ({"ledger_full": False}, {"ledger_full": None},
                        {"net_deposit": None}, {"deposits": 50}):
        newer = {**_snap(2, 1100, deposits=100), **replacement}
        day = report.daily_pnl_series([base, newer])["2026-06-02"]
        trend = report.trend_summary([base, newer])
        assert day["pnl"] is None and day["net_flow"] is None and day["data_quality"] == "missing"
        assert trend["comparable"] is False and trend["pnl"] is None
        assert trend["net_flow"] is None and trend["pnl_pct"] is None
    old = dict(base); del old['ledger_full']
    assert report.daily_pnl_series([old, {**old, 'ts': '20260602-120000'}])['2026-06-02']['pnl'] is None


def test_gap_and_stale_baseline_quality():
    a = _snap(1, 1000, sources={"cash": "stale"})
    b = _snap(2, 1100, sources={"cash": "live"})
    assert report.daily_pnl_series([a, b])["2026-06-02"]["data_quality"] == "partial"
    b.update(ts="20260620-120000", ledger_full=False)
    assert report.daily_pnl_series([a, b])["2026-06-20"]["range_pnl"] is None


def test_household_null_propagation_and_renderers():
    per = {"a": [_snap(1, 1000), _snap(2, 1100)],
           "b": [_snap(1, 1000), {**_snap(2, 1100), 'ledger_full': False}]}
    household = cli._trend_payload(per, None)['household']
    assert household['change'] == 200 and household['pnl'] is None
    assert household['net_flow'] is None and household['pnl_pct'] is None
    import io, contextlib
    old_load, old_list, old_label = cli.snapshots.load_all, cli.config.list_accounts, cli.config.account_label
    try:
        cli.snapshots.load_all = lambda acct: per[acct]
        cli.config.list_accounts = lambda: ['a', 'b']
        cli.config.account_label = lambda p: p
        for fmt in ('table', 'lark', 'json'):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert cli.cmd_trend(None, SimpleNamespace(account='all', days=None, json=fmt == 'json', fmt=fmt)) == 0
            assert out.getvalue()
    finally:
        cli.snapshots.load_all, cli.config.list_accounts, cli.config.account_label = old_load, old_list, old_label


if __name__ == '__main__':
    raise SystemExit(run(globals()))
