import json
from datetime import datetime, timezone
from pathlib import Path
from _runner import run
from paypay_sec import audit


def _clean(p):
    import shutil; shutil.rmtree(p, ignore_errors=True); p.mkdir(parents=True)


def test_record_appends_jsonl(tmp=Path("/tmp/pp_audit_1")):
    _clean(tmp)
    log = tmp / "orders.log"
    audit.record({"kind": "submit", "symbol": "TSLA"}, log_path=log,
                 now=datetime(2026, 5, 29, 1, 2, 3, tzinfo=timezone.utc))
    audit.record({"kind": "dry_run", "symbol": "QQQ"}, log_path=log,
                 now=datetime(2026, 5, 29, 1, 2, 4, tzinfo=timezone.utc))
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 2
    rec0 = json.loads(lines[0])
    assert rec0["kind"] == "submit" and rec0["symbol"] == "TSLA" and "ts" in rec0


def test_count_today_counts_only_kind_and_day(tmp=Path("/tmp/pp_audit_2")):
    _clean(tmp)
    log = tmp / "orders.log"
    d = datetime(2026, 5, 29, 10, 0, tzinfo=timezone.utc)
    prev = datetime(2026, 5, 28, 10, 0, tzinfo=timezone.utc)
    audit.record({"kind": "submit"}, log_path=log, now=d)
    audit.record({"kind": "submit"}, log_path=log, now=d)
    audit.record({"kind": "dry_run"}, log_path=log, now=d)   # not counted
    audit.record({"kind": "submit"}, log_path=log, now=prev) # other day
    assert audit.count_today(log_path=log, now=d, kind="submit") == 2


def test_count_today_missing_log_is_zero(tmp=Path("/tmp/pp_audit_3")):
    _clean(tmp)
    assert audit.count_today(log_path=tmp / "nope.log",
                             now=datetime(2026, 5, 29, tzinfo=timezone.utc)) == 0


if __name__ == "__main__":
    raise SystemExit(run(globals()))
