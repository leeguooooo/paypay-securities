import json
from datetime import datetime, timezone
from pathlib import Path
from _runner import run
from paypay_sec.guards import TradeConfig, load_trade_config, DEFAULT_TRADE_CONFIG
from paypay_sec.guards import check_guards, GuardContext
from paypay_sec.orders import build


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


def _req(**kw):
    base = dict(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    base.update(kw)
    return build(**base)


def test_guard_pass_clean():
    cfg = load_trade_config(config_path=Path("/tmp/none.json"))  # defaults
    ctx = GuardContext(now=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc), today_order_count=0)
    assert check_guards(_req(), cfg, ctx) == []


def test_guard_market_not_allowed():
    cfg = TradeConfig(allow_markets=["usa"])
    ctx = GuardContext(now=None, today_order_count=0)
    v = check_guards(_req(market="japan"), cfg, ctx)
    assert any("market" in s for s in v)


def test_guard_symbol_allowlist():
    cfg = TradeConfig(allow_symbols=["QQQ"])
    ctx = GuardContext(now=None, today_order_count=0)
    assert any("symbol" in s for s in check_guards(_req(symbol="TSLA"), cfg, ctx))
    assert check_guards(_req(symbol="QQQ"), cfg, ctx) == []


def test_guard_market_order_blocked_by_default():
    cfg = TradeConfig()  # allow_market_order False
    ctx = GuardContext(now=None, today_order_count=0)
    assert any("market order" in s.lower() for s in check_guards(_req(market_order=True, qty=1, limit=None), cfg, ctx))


def test_guard_daily_cap():
    cfg = TradeConfig(daily_order_cap=2)
    ctx = GuardContext(now=None, today_order_count=2)
    assert any("daily" in s.lower() for s in check_guards(_req(), cfg, ctx))


def test_guard_price_collar_trips_on_far_limit():
    cfg = TradeConfig(price_collar_pct=5)
    ctx = GuardContext(now=None, today_order_count=0, current_quote=250.0)
    # limit 300 is +20% vs quote 250 -> collar trips
    assert any("collar" in s.lower() for s in check_guards(_req(limit=300.0), cfg, ctx))
    # limit 252 is within 5%
    assert check_guards(_req(limit=252.0), cfg, ctx) == []


def test_guard_collar_skipped_without_quote():
    cfg = TradeConfig(price_collar_pct=5)
    ctx = GuardContext(now=None, today_order_count=0, current_quote=None)
    assert check_guards(_req(limit=9999.0), cfg, ctx) == []  # no quote -> cannot enforce, must not crash


def test_guard_max_order_jpy_uses_preview():
    cfg = TradeConfig(max_order_jpy=50000)
    ctx = GuardContext(now=None, today_order_count=0)
    over = check_guards(_req(), cfg, ctx, preview={"total_jpy": 60000})
    assert any("max_order_jpy" in s or "上限" in s or "limit" in s.lower() for s in over)
    assert check_guards(_req(), cfg, ctx, preview={"total_jpy": 40000}) == []


def test_guard_max_pct_uses_preview_and_portfolio():
    cfg = TradeConfig(max_pct_of_portfolio=10)
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=500000)
    # 60000 / 500000 = 12% > 10%
    assert any("pct" in s.lower() or "%" in s for s in check_guards(_req(), cfg, ctx, preview={"total_jpy": 60000}))


def test_guard_trading_hours_outside_window():
    cfg = TradeConfig(trading_hours={"usa": {"tz": "UTC", "windows": [["13:30", "20:00"]]}})
    ctx = GuardContext(now=datetime(2026, 5, 29, 2, 0, tzinfo=timezone.utc), today_order_count=0)
    assert any("hours" in s.lower() or "時段" in s for s in check_guards(_req(), cfg, ctx))


if __name__ == "__main__":
    raise SystemExit(run(globals()))
