"""FX mid-rate lookup + cache coverage. Ledger dates are dotted ('2026.05.26');
the ECB/frankfurter series is ISO — a raw string compare silently returned the
LATEST cached rate for any date. Network is always mocked here."""
import json
import tempfile
from pathlib import Path

from _runner import run
from paypay_sec import market

SERIES = {"2026-05-22": 150.0, "2026-05-25": 151.0, "2026-05-26": 152.0, "2026-06-10": 160.0}


def test_mid_for_dotted_date_exact():
    assert market.mid_for(SERIES, "2026.05.26") == 152.0


def test_mid_for_dotted_date_falls_back_to_preceding_business_day():
    # 05-23/24 is a weekend → Friday 05-22, NOT the newest (06-10) rate
    assert market.mid_for(SERIES, "2026.05.24") == 150.0
    assert market.mid_for(SERIES, "2026.05.01") is None


class _Resp:
    def __init__(self, rates):
        self._rates = rates

    def raise_for_status(self):
        pass

    def json(self):
        return {"rates": {d: {"JPY": v} for d, v in self._rates.items()}}


def _with_mock(fn):
    d = Path(tempfile.mkdtemp(prefix="pp_fx_test_"))
    orig_cache, orig_get = market._FX_CACHE, market.requests.get
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _Resp({"2026-05-25": 151.0, "2026-05-26": 152.0})

    market._FX_CACHE = d / "usdjpy.json"
    market.requests.get = fake_get
    try:
        fn(calls)
    finally:
        market._FX_CACHE, market.requests.get = orig_cache, orig_get


def test_series_fetches_iso_range_and_then_hits_cache():
    def body(calls):
        s = market.usdjpy_series("2026.05.25", "2026.05.26")
        assert s["2026-05-26"] == 152.0
        assert len(calls) == 1 and "2026-05-25..2026-05-26" in calls[0]   # ISO, not dotted
        market.usdjpy_series("2026.05.25", "2026.05.26")                   # covered → no fetch
        assert len(calls) == 1
        market.usdjpy_series("2026.05.25", "2026.05.28")                   # only the gap
        assert len(calls) == 2 and "2026-05-27..2026-05-28" in calls[1]
    _with_mock(body)


def test_legacy_flat_cache_still_readable():
    def body(calls):
        market._FX_CACHE.write_text(json.dumps({"2026-05-26": 152.0}), encoding="utf-8")
        rates, covered = market._load()
        assert rates == {"2026-05-26": 152.0} and covered == []
    _with_mock(body)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
