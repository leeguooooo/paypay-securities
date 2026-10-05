import tempfile
from pathlib import Path
from types import SimpleNamespace

from _runner import run
from paypay_sec import snapshots, cli


def _tmp_redirect():
    """Point snapshots at a throwaway dir so tests never touch ~/.paypay-sec."""
    d = Path(tempfile.mkdtemp(prefix="pp_snap_test_"))
    snapshots.state_dir = lambda account: d  # monkeypatch
    return d


def test_ts_roundtrip():
    ts = snapshots.now_ts()
    dt = snapshots.parse_ts(ts)
    assert dt is not None and dt.strftime(snapshots.TS_FMT) == ts
    assert snapshots.parse_ts("not-a-ts") is None


def test_save_list_load_latest():
    _tmp_redirect()
    assert snapshots.list_paths("default") == []
    assert snapshots.latest("default") is None
    p1 = snapshots.save("default", {"ts": "20260101-090000", "grand_total": 100})
    p2 = snapshots.save("default", {"ts": "20260201-090000", "grand_total": 200})
    paths = snapshots.list_paths("default")
    assert len(paths) == 2 and paths[0] == p1 and paths[1] == p2  # sorted ascending
    assert snapshots.load(p1)["grand_total"] == 100
    assert snapshots.latest("default")["grand_total"] == 200  # most recent


def test_nearest_before():
    _tmp_redirect()
    snapshots.save("default", {"ts": "20260101-090000", "grand_total": 1})
    snapshots.save("default", {"ts": "20260601-090000", "grand_total": 2})
    # there is no helper to fabricate "now"; nearest_before uses real now, and both
    # snapshots are in the past, so days=1 returns the most recent past one.
    got = snapshots.nearest_before("default", days=1)
    assert got is not None and got["grand_total"] in (1, 2)


def test_diff_payload_math():
    base = {"ts": "A", "as_of": "t0", "grand_total": 1000, "invested": 800, "cash": 200,
            "net_deposit": 500, "unrealized_pl": 50, "realized_total": 10,
            "holdings": [{"name": "TSLA", "valuation": 300}, {"name": "QQQ", "valuation": 500}]}
    cur = {"ts": "B", "as_of": "t1", "grand_total": 1200, "invested": 1050, "cash": 150,
           "net_deposit": 600, "unrealized_pl": 70, "realized_total": 25,
           "holdings": [{"name": "TSLA", "valuation": 450}, {"name": "QQQ", "valuation": 500},
                        {"name": "NVDA", "valuation": 100}],
           "sources": {"securities": "ok"}}
    d = cli._diff_payload(base, cur)
    assert d["delta"]["grand_total"] == 200
    assert d["delta"]["cash"] == -50
    assert d["delta"]["net_deposit"] == 100
    assert d["delta"]["realized_total"] == 15
    # holdings: TSLA changed 300→450, NVDA new 0→100; QQQ unchanged (omitted)
    changes = {h["name"]: h for h in d["holdings_changed"]}
    assert changes["TSLA"]["delta"] == 150
    assert changes["NVDA"]["delta"] == 100
    assert "QQQ" not in changes
    assert d["from"]["ts"] == "A" and d["to"]["ts"] == "B"


def test_pages_helper():
    assert cli._pages(SimpleNamespace(fetch_all=False, pages=3), 8) == 3
    assert cli._pages(SimpleNamespace(fetch_all=False), 8) == 8  # default
    assert cli._pages(SimpleNamespace(fetch_all=True, pages=3), 8) == cli._ALL_PAGES_CAP


def test_basis_hint():
    assert cli._basis_hint(True, False) == ""
    h = cli._basis_hint(False, fetched_all=False)
    assert "--all" in h and "取得原価が不足" in h
    h2 = cli._basis_hint(False, fetched_all=True)
    assert "--all" not in h2  # already fetched everything; don't suggest it


def test_freshness_block():
    lines = []
    cli._freshness_block({"as_of": "2026-06-04 09:00 JST",
                          "sources": {"securities": "ok", "cash": "stale", "invtrust": "failed"}}, lines)
    text = "\n".join(lines)
    assert "查询时间 2026-06-04 09:00 JST" in text
    assert "stale" in text and "失敗" in text  # warnings surfaced, not silent


def test_cmd_snapshot_save_and_diff_smoke():
    """Full snapshot-save + diff path with the gather mocked (no network)."""
    import contextlib
    import io
    import json

    d = _tmp_redirect()
    fake_snap = {"grand_total": 1000, "cash": 200, "invested": 800, "unrealized_pl": 50,
                 "realized_total": 10, "net_deposit": 500, "deposits": 500, "withdrawals": 0,
                 "holdings": [{"name": "TSLA", "category": "証券", "valuation": 800, "unrealized_pl": 50}],
                 "sources": {"securities": "ok"}}

    orig = cli._build_snapshot
    cli._build_snapshot = lambda client, args: {**fake_snap, "ts": snapshots.now_ts(),
                                                "as_of": cli._now_jst_str()}
    try:
        # save
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_snapshot(None, SimpleNamespace(account="default", snap_cmd="save",
                                                        json=True, fmt=None, lang="ja", pages=8))
        saved = json.loads(buf.getvalue())
        assert rc == 0 and "saved" in saved and saved["grand_total"] == 1000
        assert len(snapshots.list_paths("default")) == 1

        # diff a (slightly larger) live read against that baseline
        cli._build_snapshot = lambda client, args: {**fake_snap, "grand_total": 1300,
                                                    "ts": snapshots.now_ts(), "as_of": cli._now_jst_str(),
                                                    "holdings": [{"name": "TSLA", "category": "証券",
                                                                  "valuation": 1100, "unrealized_pl": 80}]}
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = cli.cmd_diff(None, SimpleNamespace(account="default", days=None, since=None,
                                                     json=True, fmt=None, lang="ja", pages=8))
        diff = json.loads(buf2.getvalue())
        assert rc2 == 0
        assert diff["delta"]["grand_total"] == 300
        assert diff["holdings_changed"][0]["name"] == "TSLA"
        assert diff["holdings_changed"][0]["delta"] == 300
    finally:
        cli._build_snapshot = orig


def test_cmd_diff_no_baseline():
    import contextlib
    import io
    _tmp_redirect()  # empty snapshot dir
    buf = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
        rc = cli.cmd_diff(None, SimpleNamespace(account="default", days=None, since=None,
                                                json=False, fmt=None, lang="ja", pages=8))
    assert rc == 1  # no baseline → clean error, exit 1


def test_same_second_save_does_not_overwrite():
    _tmp_redirect()
    p1 = snapshots.save("default", {"ts": "20260301-090000", "grand_total": 1})
    p2 = snapshots.save("default", {"ts": "20260301-090000", "grand_total": 2})
    assert p1 != p2 and p1.exists() and p2.exists()
    assert snapshots.latest("default")["grand_total"] == 2      # suffix sorts later
    assert snapshots.parse_ts(p2.stem) == snapshots.parse_ts(p1.stem)
    # atomic write leaves no temp files behind
    assert not [p for p in p1.parent.iterdir() if p.name.endswith(".tmp")]


def test_unreadable_snapshot_is_skipped_not_fatal():
    import contextlib
    import io
    d = _tmp_redirect()
    snapshots.save("default", {"ts": "20260101-090000", "grand_total": 1})
    (d / "snapshots" / "20260201-090000.json").write_text("{truncated", encoding="utf-8")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert snapshots.latest("default")["grand_total"] == 1     # falls back past it
        assert [s["grand_total"] for s in snapshots.load_all("default")] == [1]
        assert snapshots.nearest_before("default", days=1)["grand_total"] == 1
    assert "skipping unreadable snapshot" in err.getvalue()


def _fake_client(ledger, invledger, brands_html=None, japan_fails=False):
    d = Path(tempfile.mkdtemp(prefix="pp_fetch_test_"))

    def brands(mkt):
        if mkt == "japan" and japan_fails:
            raise RuntimeError("boom")
        return brands_html if mkt == "usa" and brands_html else "<html></html>"

    return SimpleNamespace(
        session_file=d / "session.json", ensure_session=lambda: None,
        settlement_records=lambda max_pages: ledger,
        invtrust_settlement_records=lambda max_pages: invledger,
        brands_html=brands,
        invtrust_top=lambda: {"SECURITIES_VALUE_TOTAL": "0", "INVEST_BRAND_ARRAY": []},
        invtrust_brands=lambda: {})


_LEDGER = [{"BASE_D": "2026.06.01", "SEQ_NO": 1, "SUMMARY_TYPE": "3", "AMOUNT": "1000",
            "CASH_BALANCE": "1000"}]


def test_fetch_account_sources_reflect_empty_and_partial_feeds():
    bad_val = ("<table class='d_table'><tr><td>●テスト株</td><td>ー(—)</td>"
               "<td>(1.0)</td><td>¥90(+¥1)</td></tr></table>")
    a = cli._fetch_account(_fake_client(_LEDGER, [], brands_html=bad_val), 1, 1)
    assert a["sources"]["invledger"] == "failed"          # empty = throttled, not ok
    assert a["sources"]["cash"] == "stale"                # only one ledger → not live
    assert a["sources"]["securities"] == "partial"        # unparsed valuation
    a = cli._fetch_account(_fake_client(_LEDGER, _LEDGER, japan_fails=True), 1, 1)
    assert a["sources"]["securities"] == "partial" and a["sources"]["cash"] == "live"


def test_build_snapshot_full_ledger_marker():
    snap = cli._build_snapshot(_fake_client(_LEDGER, _LEDGER), SimpleNamespace())
    assert snap["ledger_full"] is True
    assert snap["deposits"] == 1000 and "fx" not in snap["sources"]   # no FX fetch
    snap = cli._build_snapshot(_fake_client(_LEDGER, []), SimpleNamespace())
    assert snap["ledger_full"] is False


if __name__ == "__main__":
    raise SystemExit(run(globals()))
