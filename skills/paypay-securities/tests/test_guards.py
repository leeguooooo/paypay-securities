import json
from pathlib import Path
from _runner import run
from paypay_sec.guards import TradeConfig, load_trade_config, DEFAULT_TRADE_CONFIG


def test_defaults_are_conservative():
    c = TradeConfig()
    assert c.max_order_jpy == DEFAULT_TRADE_CONFIG["max_order_jpy"]
    assert c.allow_market_order is False
    assert c.allow_markets == ["usa"]


def test_load_missing_returns_defaults(tmp=Path("/tmp/pp_cfg_missing")):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.allow_market_order is False and c.max_order_jpy == 50000


def test_load_broken_json_returns_defaults(tmp=Path("/tmp/pp_cfg_broken")):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "trade.json").write_text("{ not json")
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000  # fail-safe


def test_load_merges_partial(tmp=Path("/tmp/pp_cfg_partial")):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "trade.json").write_text(json.dumps({"max_order_jpy": 10000, "allow_market_order": True}))
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 10000 and c.allow_market_order is True
    assert c.daily_order_cap == 5  # untouched key keeps default


if __name__ == "__main__":
    raise SystemExit(run(globals()))
