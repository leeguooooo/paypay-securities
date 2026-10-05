import json
from datetime import datetime, timezone
from pathlib import Path
from _runner import run
from paypay_sec.guards import TradeConfig, load_trade_config, DEFAULT_TRADE_CONFIG
from paypay_sec.guards import check_guards, GuardContext
from paypay_sec.orders import build

_TOTAL = 1_000_000   # portfolio total so the max_pct check can run


def test_defaults_are_conservative():
    c = TradeConfig()
    assert c.max_order_jpy == DEFAULT_TRADE_CONFIG["max_order_jpy"]
    assert c.allow_markets == ["usa"]
    assert not hasattr(c, "allow_market_order") and not hasattr(c, "price_collar_pct")


def test_load_missing_returns_defaults(tmp=Path("/tmp/pp_cfg_missing")):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000


def test_load_broken_json_returns_defaults(tmp=Path("/tmp/pp_cfg_broken")):
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "trade.json").write_text("{ not json")
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000  # fail-safe


def test_load_merges_partial(tmp=Path("/tmp/pp_cfg_partial")):
    tmp.mkdir(parents=True, exist_ok=True)
    # old files may still carry the retired collar / market-order keys -> ignored
    (tmp / "trade.json").write_text(json.dumps({"max_order_jpy": 10000, "allow_market_order": True,
                                                "price_collar_pct": 5}))
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 10000
    assert c.daily_order_cap == 5  # untouched key keeps default


def test_load_garbage_values_do_not_loosen(tmp=Path("/tmp/pp_cfg_garbage")):
    tmp.mkdir(parents=True, exist_ok=True)
    # json accepts NaN/Infinity; bools are ints in Python; strings aren't numbers
    (tmp / "trade.json").write_text(
        '{"max_order_jpy": NaN, "max_pct_of_portfolio": Infinity, "daily_order_cap": true,'
        ' "allow_symbols": "TSLA", "allow_markets": {"usa": 1}}')
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000 and c.max_pct_of_portfolio == 10 and c.daily_order_cap == 5
    assert c.allow_symbols == [] and c.allow_markets == ["usa"]
    (tmp / "trade.json").write_text(json.dumps(
        {"max_order_jpy": "false", "max_pct_of_portfolio": -5, "daily_order_cap": 0,
         "allow_symbols": ["QQQ", 1]}))
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000 and c.max_pct_of_portfolio == 10 and c.daily_order_cap == 5
    assert c.allow_symbols == []
    (tmp / "trade.json").write_text(json.dumps({"max_order_jpy": False, "daily_order_cap": 2.0,
                                                "allow_symbols": ["qqq"]}))
    c = load_trade_config(config_path=tmp / "trade.json")
    assert c.max_order_jpy == 50000 and c.daily_order_cap == 2 and c.allow_symbols == ["QQQ"]


def _req(**kw):
    base = dict(market="usa", symbol="TSLA", side="buy", amount_jpy=40000)
    base.update(kw)
    return build(**base)


def test_guard_pass_clean():
    cfg = load_trade_config(config_path=Path("/tmp/none.json"))  # defaults
    ctx = GuardContext(now=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc), today_order_count=0,
                       portfolio_total_jpy=_TOTAL)
    assert check_guards(_req(), cfg, ctx) == []


def test_guard_market_not_allowed():
    cfg = TradeConfig(allow_markets=["usa"])
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=_TOTAL)
    v = check_guards(_req(market="japan"), cfg, ctx)
    assert any("market" in s for s in v)


def test_guard_symbol_allowlist():
    cfg = TradeConfig(allow_symbols=["QQQ"])
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=_TOTAL)
    assert any("symbol" in s for s in check_guards(_req(symbol="TSLA"), cfg, ctx))
    assert check_guards(_req(symbol="QQQ"), cfg, ctx) == []


def test_guard_daily_cap():
    cfg = TradeConfig(daily_order_cap=2)
    ctx = GuardContext(now=None, today_order_count=2, portfolio_total_jpy=_TOTAL)
    assert any("daily" in s.lower() for s in check_guards(_req(), cfg, ctx))


def test_guard_max_order_jpy_uses_preview():
    cfg = TradeConfig(max_order_jpy=50000)
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=_TOTAL)
    over = check_guards(_req(), cfg, ctx, preview={"total_jpy": 60000})
    assert any("max_order_jpy" in s or "上限" in s or "limit" in s.lower() for s in over)
    assert check_guards(_req(), cfg, ctx, preview={"total_jpy": 40000}) == []


def test_guard_max_pct_uses_preview_and_portfolio():
    cfg = TradeConfig(max_pct_of_portfolio=10)
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=500000)
    assert check_guards(_req(), cfg, ctx, preview={"total_jpy": 40000}) == []   # 8%
    # 60000 / 500000 = 12% > 10%
    assert any("pct" in s.lower() or "%" in s for s in check_guards(_req(), cfg, ctx, preview={"total_jpy": 60000}))


def test_guard_trading_hours_outside_window():
    cfg = TradeConfig(trading_hours={"usa": {"tz": "UTC", "windows": [["13:30", "20:00"]]}})
    ctx = GuardContext(now=datetime(2026, 5, 29, 2, 0, tzinfo=timezone.utc), today_order_count=0,
                       portfolio_total_jpy=_TOTAL)
    assert any("hours" in s.lower() or "時段" in s for s in check_guards(_req(), cfg, ctx))


def test_guard_trading_hours_inside_window():
    cfg = TradeConfig(trading_hours={"usa": {"tz": "UTC", "windows": [["13:30", "20:00"]]}})
    ctx = GuardContext(now=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc), today_order_count=0,
                       portfolio_total_jpy=_TOTAL)
    assert check_guards(_req(), cfg, ctx) == []  # inside window -> no violation


def test_guard_hours_undefined_market_no_violation():
    cfg = TradeConfig(trading_hours={"japan": {"tz": "Asia/Tokyo", "windows": [["09:00", "15:00"]]}})
    ctx = GuardContext(now=datetime(2026, 5, 29, 2, 0, tzinfo=timezone.utc), today_order_count=0,
                       portfolio_total_jpy=_TOTAL)
    # market 'usa' has no window defined -> _within_hours returns None -> no violation
    assert check_guards(_req(market="usa"), cfg, ctx) == []


def test_guard_max_order_jpy_pre_confirm_uses_amount():
    cfg = TradeConfig(max_order_jpy=50000)
    ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=_TOTAL)
    assert any("max_order_jpy" in s for s in check_guards(_req(amount_jpy=60000), cfg, ctx))


def test_guard_max_pct_fails_closed_without_total():
    cfg = TradeConfig()
    for total in (None, 0):
        ctx = GuardContext(now=None, today_order_count=0, portfolio_total_jpy=total)
        assert any("portfolio total unavailable" in s for s in check_guards(_req(), cfg, ctx))
        assert any("portfolio total unavailable" in s
                   for s in check_guards(_req(), cfg, ctx, preview={"total_jpy": 1000}))


def test_guard_unreadable_count_blocks():
    from paypay_sec.audit import COUNT_UNREADABLE
    ctx = GuardContext(now=None, today_order_count=COUNT_UNREADABLE, portfolio_total_jpy=_TOTAL)
    assert any("unavailable" in s for s in check_guards(_req(), TradeConfig(daily_order_cap=99), ctx))


def test_guard_malformed_trading_hours_blocks():
    ctx = GuardContext(now=datetime(2026, 5, 29, 14, 0, tzinfo=timezone.utc), today_order_count=0,
                       portfolio_total_jpy=_TOTAL)
    assert check_guards(_req(), TradeConfig(trading_hours=["09:00"]), ctx)
    assert check_guards(_req(), TradeConfig(trading_hours={"usa": {"tz": "UTC", "windows": [1]}}), ctx)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
