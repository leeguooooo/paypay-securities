"""Snapshot integrity: failed reads never enter the series; long gaps are not
booked as one day's P&L; trend math. Synthetic data only, no network."""
import contextlib
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from _runner import run
from paypay_sec import cli, report, snapshots


def _tmp_redirect():
    d = Path(tempfile.mkdtemp(prefix="pp_integrity_"))
    snapshots.state_dir = lambda account: d
    return d


def _snap(ts, gt, nd=0, rz=0, sources=None, **extra):
    return {"ts": ts, "as_of": ts, "grand_total": gt, "net_deposit": nd, "realized_total": rz,
            "sources": sources or {"securities": "ok", "invtrust": "ok", "ledger": "ok",
                                   "invledger": "ok", "cash": "live"}, **extra}


_FAILED = {"securities": "failed", "invtrust": "failed", "ledger": "failed",
           "invledger": "failed", "cash": "stale"}


def test_problems_flags_failed_sources_and_missing_total():
    assert snapshots.problems(_snap("20260101-073000", 100)) == []
    assert snapshots.problems(_snap("20260101-073000", 403, sources=_FAILED)) == [
        "failed sources: invledger, invtrust, ledger, securities"]
    assert "grand_total missing" in snapshots.problems({"sources": {}})


def test_latest_and_nearest_skip_unusable():
    _tmp_redirect()
    snapshots.save("default", _snap("20260101-073000", 100))
    snapshots.save("default", _snap("20260102-073000", 403, sources=_FAILED))
    assert snapshots.latest("default")["grand_total"] == 100
    assert snapshots.nearest_before("default", 1)["grand_total"] == 100


def test_quarantine_moves_only_unusable():
    d = _tmp_redirect()
    snapshots.save("default", _snap("20260101-073000", 100))
    snapshots.save("default", _snap("20260102-073000", 403, sources=_FAILED))
    assert [p.name for p in snapshots.quarantine_unusable("default", dry_run=True)] == ["20260102-073000.json"]
    assert len(snapshots.list_paths("default")) == 2          # dry run touched nothing
    moved = snapshots.quarantine_unusable("default")
    assert [p.name for p in moved] == ["20260102-073000.json"]
    assert [p.name for p in snapshots.list_paths("default")] == ["20260101-073000.json"]
    assert (d / "snapshots" / "rejected" / "20260102-073000.json").exists()


def test_snapshot_save_rejects_failed_read():
    d = _tmp_redirect()
    orig = cli._build_snapshot
    cli._build_snapshot = lambda client, args: _snap("20260103-073000", 403, sources=_FAILED)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = cli.cmd_snapshot(None, SimpleNamespace(account=None, snap_cmd="save", json=False))
    finally:
        cli._build_snapshot = orig
    assert rc == cli.SNAPSHOT_REJECTED_EXIT, rc
    assert snapshots.list_paths(None) == [], "failed read leaked into the series"
    assert list((d / "snapshots" / "rejected").glob("*.json")), "rejected snapshot not kept"
    assert "NOT saved" in err.getvalue()


def test_long_gap_is_not_one_days_pnl():
    snaps = [_snap("20260601-073000", 500_000, nd=500_000),
             _snap("20260602-073000", 510_000, nd=500_000),
             _snap("20261125-073000", 900_000, nd=800_000)]   # five months later
    s = report.daily_pnl_series(snaps)
    assert s["2026-06-02"]["pnl"] == 10_000
    gap = s["2026-11-25"]
    assert gap["pnl"] is None and gap["data_quality"] == "missing"
    assert gap["range_from"] == "2026-06-02" and gap["range_pnl"] == 90_000


def test_weekend_gap_still_counts():
    snaps = [_snap("20260605-073000", 100_000), _snap("20260609-073000", 101_000)]  # Fri → Tue
    assert report.daily_pnl_series(snaps)["2026-06-09"]["pnl"] == 1_000


def test_trend_strips_deposits_and_ignores_failed():
    snaps = [_snap("20261101-073000", 1_000_000, nd=1_000_000),
             _snap("20261102-073000", 403, sources=_FAILED),
             _snap("20261103-073000", 1_120_000, nd=1_100_000),
             _snap("20261104-073000", 1_110_000, nd=1_100_000)]
    t = report.trend_summary(snaps)
    assert t["start"]["date"] == "2026-11-01" and t["end"]["date"] == "2026-11-04"
    assert t["net_flow"] == 100_000 and t["change"] == 110_000 and t["pnl"] == 10_000
    assert t["pnl_pct"] == round(10_000 / 1_050_000 * 100, 2)          # simple Dietz
    assert t["stats"]["best"]["pnl"] == 20_000 and t["stats"]["worst"]["pnl"] == -10_000


def test_trend_window_ignores_far_baseline():
    snaps = [_snap("20260601-073000", 500_000), _snap("20261120-073000", 900_000),
             _snap("20261125-073000", 910_000)]
    t = report.trend_summary(snaps, since="2026-11-01")
    assert t["start"]["date"] == "2026-11-20", t["start"]   # not the June snapshot


def test_household_flags_misaligned_windows():
    per = {"a": [_snap("20261101-073000", 100), _snap("20261125-073000", 110)],
           "b": [_snap("20261124-073000", 200), _snap("20261125-073000", 220)]}
    h = cli._trend_payload(per, None)["household"]
    assert h["pnl"] == 30 and h["aligned"] is False


def test_long_gap_with_ledger_change_has_no_range_pnl():
    snaps = [_snap("20260601-073000", 500_000, nd=500_000, ledger_full=False),
             _snap("20261125-073000", 900_000, nd=800_000, ledger_full=True)]
    gap = report.daily_pnl_series(snaps)["2026-11-25"]
    assert gap["range_from"] == "2026-06-01" and gap["range_pnl"] is None


def test_diff_refuses_failed_live_read():
    _tmp_redirect()
    snapshots.save(None, _snap("20260101-073000", 100))
    orig = cli._build_snapshot
    cli._build_snapshot = lambda client, args: _snap("20260103-073000", 403, sources=_FAILED)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            rc = cli.cmd_diff(None, SimpleNamespace(account=None, days=None, json=False, fmt=None))
    finally:
        cli._build_snapshot = orig
    assert rc == cli.SNAPSHOT_REJECTED_EXIT and out.getvalue() == "", out.getvalue()


def test_daily_script_alerts_and_exits_nonzero():
    import os
    import subprocess
    script = Path(__file__).resolve().parent.parent / "bin" / "snapshot-daily.sh"
    with tempfile.TemporaryDirectory() as tmp:
        home, stub = Path(tmp) / "home", Path(tmp) / "bin"
        (home / ".paypay-sec").mkdir(parents=True)
        (home / ".paypay-sec" / "second.env").write_text("")
        stub.mkdir()
        # stub uv: snapshot save fails (exit 3) for the default account only
        (stub / "uv").write_text('#!/bin/bash\ncase "$*" in *"snapshot save -a"*) exit 0;; '
                                 '*"snapshot save"*) echo "error: snapshot NOT saved" >&2; exit 3;; '
                                 '*) exit 0;; esac\n')
        (stub / "uv").chmod(0o755)
        env = {"HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin",
               "PAYPAY_SNAPSHOT_ANY_DAY": "1", "PAYPAY_SNAPSHOT_NOTIFY": "0"}
        r = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, text=True,
                           timeout=60)
        log = (home / ".paypay-sec" / "snapshot-cron.log").read_text()
        assert r.returncode == 1, (r.returncode, r.stderr)
        assert "ALERT: snapshot failed for: default" in log, log
        assert "second" not in log.split("ALERT:")[1], "a passing account was reported as failed"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
