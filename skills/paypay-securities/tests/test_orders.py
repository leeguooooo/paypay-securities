from _runner import run
from paypay_sec.orders import build, OrderRequest, Side, OrderType, OrderError
from paypay_sec.guards import TradeConfig, GuardContext
from paypay_sec.orders import dry_run, place, PipelineResult
from paypay_sec.cli import make_confirmer


def test_build_limit_buy_by_qty():
    r = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    assert isinstance(r, OrderRequest)
    assert r.side is Side.BUY and r.order_type is OrderType.LIMIT
    assert r.qty == 1 and r.amount_jpy is None and r.limit_price == 250.0
    assert r.symbol == "TSLA"


def test_build_amount_buy():
    r = build(market="usa", symbol="QQQ", side="buy", amount_jpy=50000, limit=500.0)
    assert r.amount_jpy == 50000 and r.qty is None


def test_build_normalizes_symbol_and_market():
    r = build(market="us", symbol=" tsla ", side="buy", qty=1, limit=1.0)
    assert r.symbol == "TSLA" and r.market == "usa"


def test_build_rejects_both_qty_and_amount():
    try:
        build(market="usa", symbol="TSLA", side="buy", qty=1, amount_jpy=50000, limit=1.0)
        assert False, "expected OrderError"
    except OrderError:
        pass


def test_build_rejects_neither_qty_nor_amount():
    try:
        build(market="usa", symbol="TSLA", side="buy", limit=1.0)
        assert False
    except OrderError:
        pass


def test_build_no_limit_is_market_at_quote():
    # PayPay US orders fill at the prevailing quote — no per-order limit required.
    r = build(market="usa", symbol="TSLA", side="buy", qty=1)
    assert r.limit_price is None and r.order_type is OrderType.LIMIT


def test_build_amount_no_limit_ok():
    r = build(market="usa", symbol="QQQ", side="buy", amount_jpy=1000)
    assert r.amount_jpy == 1000 and r.limit_price is None


def test_build_account_type_validated():
    r = build(market="usa", symbol="TSLA", side="buy", amount_jpy=1000, account_type=3)
    assert r.account_type == 3
    try:
        build(market="usa", symbol="TSLA", side="buy", amount_jpy=1000, account_type=9)
        assert False
    except OrderError:
        pass


def test_build_market_order_forbids_limit_price():
    try:
        build(market="usa", symbol="TSLA", side="buy", qty=1, market_order=True, limit=10.0)
        assert False
    except OrderError:
        pass


def test_build_rejects_nonpositive():
    for kw in (dict(qty=0, limit=1.0), dict(qty=-1, limit=1.0), dict(amount_jpy=0, limit=1.0), dict(qty=1, limit=-5.0)):
        try:
            build(market="usa", symbol="TSLA", side="buy", **kw)
            assert False, kw
        except OrderError:
            pass


def test_build_rejects_unknown_side():
    try:
        build(market="usa", symbol="TSLA", side="hodl", qty=1, limit=1.0)
        assert False
    except OrderError:
        pass


def _confirm_ok(req):
    return {"token": "TOK123", "total_jpy": 40000, "est_price": 250.0, "fee_jpy": 0, "raw": {}}


def test_dry_run_passes_guards_and_returns_preview():
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = dry_run(req, TradeConfig(), GuardContext(today_order_count=0), confirm=_confirm_ok)
    assert isinstance(res, PipelineResult)
    assert res.ok and res.violations == [] and res.preview["token"] == "TOK123"
    assert res.submitted is False


def test_dry_run_blocks_on_guard_before_confirm():
    called = {"n": 0}
    def confirm_spy(req):
        called["n"] += 1; return _confirm_ok(req)
    req = build(market="japan", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = dry_run(req, TradeConfig(allow_markets=["usa"]), GuardContext(), confirm=confirm_spy)
    assert not res.ok and res.violations
    assert called["n"] == 0  # fail fast: never hit confirm when pre-checks fail


def test_dry_run_blocks_on_post_confirm_amount():
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = dry_run(req, TradeConfig(max_order_jpy=10000), GuardContext(), confirm=_confirm_ok)
    assert not res.ok and any("max_order_jpy" in s for s in res.violations)
    assert res.submitted is False


def test_place_requires_confirmer_true_to_submit():
    submitted = {"tok": None}
    def submit_fn(token, req): submitted["tok"] = token; return {"order_id": "OID9"}
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = place(req, TradeConfig(), GuardContext(), confirm=_confirm_ok, submit=submit_fn,
                confirmer=lambda req, preview: True)
    assert res.submitted and res.order_id == "OID9" and submitted["tok"] == "TOK123"


def test_place_aborts_when_confirmer_false():
    submitted = {"n": 0}
    def submit_fn(token, req): submitted["n"] += 1; return {}
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = place(req, TradeConfig(), GuardContext(), confirm=_confirm_ok, submit=submit_fn,
                confirmer=lambda req, preview: False)
    assert not res.submitted and submitted["n"] == 0


def test_place_does_not_submit_when_guard_fails():
    submitted = {"n": 0}
    def submit_fn(token, req): submitted["n"] += 1; return {}
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    res = place(req, TradeConfig(max_order_jpy=10000), GuardContext(),
                confirm=_confirm_ok, submit=submit_fn, confirmer=lambda r, p: True)
    assert not res.submitted and submitted["n"] == 0


def test_confirmer_accepts_exact_phrase():
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    preview = {"total_jpy": 40000}
    yes = make_confirmer(reader=lambda prompt: "TSLA 1")
    assert yes(req, preview) is True


def test_confirmer_rejects_wrong_phrase():
    req = build(market="usa", symbol="TSLA", side="buy", qty=1, limit=250.0)
    no = make_confirmer(reader=lambda prompt: "yes")
    assert no(req, {"total_jpy": 40000}) is False


def test_confirmer_amount_phrase():
    req = build(market="usa", symbol="QQQ", side="buy", amount_jpy=50000, limit=500.0)
    yes = make_confirmer(reader=lambda prompt: "QQQ 50000")
    assert yes(req, {"total_jpy": 50000}) is True


if __name__ == "__main__":
    raise SystemExit(run(globals()))
