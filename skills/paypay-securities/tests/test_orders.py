from _runner import run
from paypay_sec.orders import build, OrderRequest, Side, OrderType, OrderError


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


def test_build_limit_requires_price():
    try:
        build(market="usa", symbol="TSLA", side="buy", qty=1)  # no limit, not market
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


if __name__ == "__main__":
    raise SystemExit(run(globals()))
