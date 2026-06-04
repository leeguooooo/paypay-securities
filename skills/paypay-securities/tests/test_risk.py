from _runner import run
from paypay_sec import cli


def _rows():
    # 18% Tesla, a QQQ ETF, an S&P fund (投信), in JPY valuations
    return [
        {"name": "TSLA", "category": "証券", "valuation": 180_000, "unrealized_pl": 1000},
        {"name": "QQQ", "category": "証券", "valuation": 120_000, "unrealized_pl": 2000},
        {"name": "eMAXIS Slim S&P500", "category": "投信", "valuation": 200_000, "unrealized_pl": 3000},
    ]


def test_kind_classification():
    assert cli._kind_of({"category": "証券", "name": "QQQ"}) == "ETF"
    assert cli._kind_of({"category": "証券", "name": "TSLA"}) == "個股"
    assert cli._kind_of({"category": "証券", "name": "Some Growth ETF"}) == "ETF"
    assert cli._kind_of({"category": "投信", "name": "whatever"}) == "投信"


def test_us_underlying_classification():
    # US-listed 証券 are always US underlying
    assert cli._us_underlying({"category": "証券", "name": "TSLA"}) is True
    # a JPY-priced 投信 with S&P500 in the name is US underlying despite JPY quotation
    assert cli._us_underlying({"category": "投信", "name": "eMAXIS Slim 米国株式(S&P500)"}) is True
    assert cli._us_underlying({"category": "投信", "name": "ナスダック100"}) is True
    # a JP-equity fund is NOT US underlying
    assert cli._us_underlying({"category": "投信", "name": "eMAXIS Slim 国内株式(TOPIX)"}) is False


def test_risk_weights_and_concentration():
    cash = 0
    p = cli._risk_payload(_rows(), cash, sell_pending=None, sources={"securities": "ok"})
    assert p["grand_total"] == 500_000
    assert p["invested_total"] == 500_000
    # largest = S&P fund 200k / 500k = 40%
    assert p["largest_position"]["name"] == "eMAXIS Slim S&P500"
    assert p["largest_position"]["weight_pct"] == 40.0
    assert p["top1_pct"] == 40.0
    assert p["top3_pct"] == 100.0
    # 証券 (USD-listed, quotation) = 300k / 500k = 60%
    assert p["usd_asset_pct"] == 60.0
    # US underlying = 証券 300k + S&P500 fund 200k = 500k → 100% (the point of #5)
    assert p["us_underlying_pct"] == 100.0
    # category split
    assert p["by_category_pct"]["証券"] == 60.0
    assert p["by_category_pct"]["投信"] == 40.0
    # kind split: 個股 36% (TSLA180k), ETF 24% (QQQ120k), 投信 40%
    assert p["by_kind_pct"]["個股"] == 36.0
    assert p["by_kind_pct"]["ETF"] == 24.0
    assert p["by_kind_pct"]["投信"] == 40.0


def test_risk_cash_in_denominator():
    p = cli._risk_payload(_rows(), cash=500_000, sell_pending=None, sources={})
    assert p["grand_total"] == 1_000_000
    assert p["cash_pct"] == 50.0
    # weights now over 1M
    assert p["by_kind_pct"]["現金"] == 50.0
    assert p["usd_asset_pct"] == 30.0  # 300k / 1M


def test_risk_facts_only_note():
    p = cli._risk_payload(_rows(), 0, None, {})
    # the note must disclaim advice/judgment (scope boundary)
    assert "助言" in p["note"] and "リスク評価" in p["note"]


def test_risk_empty_account():
    p = cli._risk_payload([], 0, None, {})
    assert p["grand_total"] == 0
    assert p["largest_position"] is None
    assert p["top1_pct"] == 0.0


def test_cmd_risk_json_smoke():
    """Full cmd_risk path (wiring + JSON emit) with the network fetch mocked."""
    import contextlib
    import io
    import json
    from types import SimpleNamespace

    orig = cli._consolidated_holdings
    cli._consolidated_holdings = lambda client: (
        _rows(), 50_000, True, 1234, {"securities": "ok", "invtrust": "ok", "cash": "live"})
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_risk(None, SimpleNamespace(json=True, fmt=None, lang="ja"))
        out = json.loads(buf.getvalue())
    finally:
        cli._consolidated_holdings = orig
    assert rc == 0
    for k in ("as_of", "grand_total", "cash_pct", "positions", "by_kind_pct",
              "by_category_pct", "top3_pct", "usd_asset_pct", "sources", "note"):
        assert k in out, k
    assert out["invtrust_sell_pending"] == 1234
    assert out["cash"] == 50_000


if __name__ == "__main__":
    raise SystemExit(run(globals()))
