import contextlib
import io
import shutil
from pathlib import Path
from types import SimpleNamespace

from _runner import run
from paypay_sec.orders import build, OrderRequest, Side, OrderError
from paypay_sec.guards import TradeConfig, GuardContext
from paypay_sec.orders import dry_run, place, PipelineResult
from paypay_sec.client import OrderOutcomeUnknown
from paypay_sec import cli, audit
from paypay_sec.cli import make_confirmer

_CTX = GuardContext(today_order_count=0, portfolio_total_jpy=1_000_000)


def test_build_amount_buy():
    r = build(market="usa", symbol="QQQ", side="buy", amount_jpy=50000)
    assert isinstance(r, OrderRequest)
    assert r.side is Side.BUY and r.amount_jpy == 50000 and r.account_type == 2


def test_build_normalizes_symbol_and_market():
    r = build(market="us", symbol=" tsla ", side="buy", amount_jpy=1000)
    assert r.symbol == "TSLA" and r.market == "usa"


def test_build_requires_amount():
    try:
        build(market="usa", symbol="TSLA", side="buy", amount_jpy=None)
        assert False, "expected OrderError"
    except OrderError:
        pass


def test_build_account_type_validated():
    r = build(market="usa", symbol="TSLA", side="buy", amount_jpy=1000, account_type=3)
    assert r.account_type == 3
    try:
        build(market="usa", symbol="TSLA", side="buy", amount_jpy=1000, account_type=9)
        assert False
    except OrderError:
        pass


def test_build_rejects_bad_amounts():
    for amt in (0, -1, float("nan"), float("inf"), float("-inf"), 1.5, True, "1000"):
        try:
            build(market="usa", symbol="TSLA", side="buy", amount_jpy=amt)
            assert False, amt
        except OrderError:
            pass


def test_build_rejects_unknown_side():
    try:
        build(market="usa", symbol="TSLA", side="hodl", amount_jpy=1000)
        assert False
    except OrderError:
        pass


def _req(**kw):
    base = dict(market="usa", symbol="TSLA", side="buy", amount_jpy=40000)
    base.update(kw)
    return build(**base)


def _confirm_ok(req):
    return {"token": "TOK123", "total_jpy": 40000, "est_price": 250.0, "fee_jpy": 0, "raw": {}}


def test_dry_run_passes_guards_and_returns_preview():
    res = dry_run(_req(), TradeConfig(), _CTX, confirm=_confirm_ok)
    assert isinstance(res, PipelineResult)
    assert res.ok and res.violations == [] and res.preview["token"] == "TOK123"
    assert res.submitted is False


def test_dry_run_blocks_on_guard_before_confirm():
    called = {"n": 0}
    def confirm_spy(req):
        called["n"] += 1; return _confirm_ok(req)
    res = dry_run(_req(market="japan"), TradeConfig(allow_markets=["usa"]), _CTX, confirm=confirm_spy)
    assert not res.ok and res.violations
    assert called["n"] == 0  # fail fast: never hit confirm when pre-checks fail


def test_dry_run_blocks_amount_pre_confirm():
    called = {"n": 0}
    def confirm_spy(req):
        called["n"] += 1; return _confirm_ok(req)
    res = dry_run(_req(amount_jpy=60000), TradeConfig(max_order_jpy=50000), _CTX, confirm=confirm_spy)
    assert not res.ok and any("max_order_jpy" in s for s in res.violations)
    assert called["n"] == 0  # amount is known up front -> blocked before confirm


def test_dry_run_blocks_on_post_confirm_amount():
    # request is under the cap, but the server's ORDER_AMOUNT is over it
    res = dry_run(_req(amount_jpy=9000), TradeConfig(max_order_jpy=10000), _CTX, confirm=_confirm_ok)
    assert not res.ok and any("max_order_jpy" in s for s in res.violations)
    assert res.submitted is False


def test_place_requires_confirmer_true_to_submit():
    submitted = {"tok": None}
    def submit_fn(token, req): submitted["tok"] = token; return {"order_id": "OID9"}
    res = place(_req(), TradeConfig(), _CTX, confirm=_confirm_ok, submit=submit_fn,
                confirmer=lambda req, preview: True)
    assert res.submitted and res.order_id == "OID9" and submitted["tok"] == "TOK123"


def test_place_missing_order_id_is_none():
    res = place(_req(), TradeConfig(), _CTX, confirm=_confirm_ok,
                submit=lambda t, r: {"order_id": None, "raw": {}}, confirmer=lambda r, p: True)
    assert res.submitted and res.order_id is None


def test_place_aborts_when_confirmer_false():
    submitted = {"n": 0}
    def submit_fn(token, req): submitted["n"] += 1; return {}
    res = place(_req(), TradeConfig(), _CTX, confirm=_confirm_ok, submit=submit_fn,
                confirmer=lambda req, preview: False)
    assert not res.submitted and submitted["n"] == 0
    assert not res.ok and res.aborted_reason


def test_place_does_not_submit_when_guard_fails():
    submitted = {"n": 0}
    def submit_fn(token, req): submitted["n"] += 1; return {}
    res = place(_req(), TradeConfig(max_order_jpy=10000), _CTX,
                confirm=_confirm_ok, submit=submit_fn, confirmer=lambda r, p: True)
    assert not res.submitted and submitted["n"] == 0


def test_place_outcome_unknown():
    def submit_fn(token, req): raise OrderOutcomeUnknown("timeout")
    res = place(_req(), TradeConfig(), _CTX, confirm=_confirm_ok, submit=submit_fn,
                confirmer=lambda r, p: True)
    assert res.outcome_unknown and not res.submitted and not res.ok


def test_confirmer_accepts_exact_phrase_with_side():
    yes = make_confirmer(reader=lambda prompt: "BUY TSLA 40000")
    with contextlib.redirect_stdout(io.StringIO()):
        assert yes(_req(), {"total_jpy": 40000}) is True


def test_confirmer_rejects_phrase_without_side():
    for typed in ("TSLA 40000", "SELL TSLA 40000", "yes", ""):
        no = make_confirmer(reader=lambda prompt, t=typed: t)
        with contextlib.redirect_stdout(io.StringIO()):
            assert no(_req(), {"total_jpy": 40000}) is False, typed


def test_confirmer_prompt_states_terms():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        make_confirmer(reader=lambda p: "")(_req(side="sell", account_type=3), {"total_jpy": 40000})
    out = buf.getvalue()
    assert "SELL TSLA" in out and "金額指定 ¥40,000" in out
    assert "current quote" in out and "成長投資枠NISA" in out


def test_place_preorder_preview_submits():
    pv = {"token": "preorder:ab12", "preorder": True, "execute_date": "2026/06/09",
          "total_jpy": 40000}
    seen = {}
    def submit_fn(token, req): seen["tok"] = token; return {"order_id": None}
    res = place(_req(), TradeConfig(), _CTX, confirm=lambda r: pv, submit=submit_fn,
                confirmer=lambda r, p: True)
    assert res.submitted and seen["tok"] == "preorder:ab12"


def test_place_aborts_on_empty_token():
    res = place(_req(), TradeConfig(), _CTX, confirm=lambda r: {"token": "", "total_jpy": 40000},
                submit=lambda t, r: {}, confirmer=lambda r, p: True)
    assert not res.submitted and res.aborted_reason == "confirm returned no token"


def test_confirmer_prompt_states_preorder():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        make_confirmer(reader=lambda p: "")(
            _req(), {"total_jpy": 40000, "preorder": True, "execute_date": "2026/06/09"})
    out = buf.getvalue()
    assert "予約注文" in out and "2026/06/09" in out and "current quote" not in out


# ---- CLI order path (offline: fake client, tmp audit log, default config) ----

class _FakeClient:
    def __init__(self, submit_exc=None):
        self.submits = 0
        self.submit_exc = submit_exc

    def order_confirm(self, req):
        return {"token": "TOK", "total_jpy": req.amount_jpy, "est_price": 1.0, "fee_jpy": 0}

    def order_submit(self, token, req, trade_password=""):
        self.submits += 1
        if self.submit_exc:
            raise self.submit_exc
        return {"order_id": None, "raw": {}}


@contextlib.contextmanager
def _cli_env(tmp, *, tty, typed, total=(1_000_000,)):
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir(parents=True)
    saved = (audit._log_path, cli._guards.load_trade_config, cli._consolidated_holdings,
             cli._stdin_is_tty, cli.make_confirmer, cli.getpass.getpass)
    audit._log_path = lambda account: tmp / "orders.log"
    cli._guards.load_trade_config = lambda account=None: TradeConfig()
    cli._consolidated_holdings = lambda client: (
        [{"valuation": total[0]}], 0, True, None, {"securities": "ok", "invtrust": "ok", "cash": "live"})
    cli._stdin_is_tty = lambda: tty
    real_mc = saved[4]
    cli.make_confirmer = lambda reader=None: real_mc(reader=lambda p: typed)
    cli.getpass.getpass = lambda prompt="": "pw"
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            yield out, err
    finally:
        (audit._log_path, cli._guards.load_trade_config, cli._consolidated_holdings,
         cli._stdin_is_tty, cli.make_confirmer, cli.getpass.getpass) = saved


def _args(**kw):
    a = dict(market="usa", symbol="TSLA", amount=40000, account=None, account_type=2,
             execute=False, json=False)
    a.update(kw)
    return SimpleNamespace(**a)


def _kinds(tmp):
    import json
    p = tmp / "orders.log"
    return [json.loads(l)["kind"] for l in p.read_text().splitlines()] if p.exists() else []


def test_cli_dry_run_never_submits(tmp=Path("/tmp/pp_cli_orders_1")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=True, typed="BUY TSLA 40000") as (out, _):
        rc = cli.cmd_buy(fc, _args())
    assert rc == 0 and fc.submits == 0
    assert "DRY-RUN" in out.getvalue() and "金額指定 ¥40,000" in out.getvalue()
    assert _kinds(tmp) == ["dry_run"]


def test_cli_execute_refused_without_tty(tmp=Path("/tmp/pp_cli_orders_2")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=False, typed="BUY TSLA 40000") as (_, err):
        rc = cli.cmd_buy(fc, _args(execute=True))
    assert rc == 2 and fc.submits == 0 and "TTY" in err.getvalue()


def test_cli_execute_submits_without_order_no(tmp=Path("/tmp/pp_cli_orders_3")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=True, typed="BUY TSLA 40000") as (out, _):
        rc = cli.cmd_buy(fc, _args(execute=True))
    assert rc == 0 and fc.submits == 1
    assert "order number not returned" in out.getvalue()
    assert _kinds(tmp) == ["submit"]


def test_cli_phrase_mismatch_is_aborted(tmp=Path("/tmp/pp_cli_orders_4")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=True, typed="TSLA 40000") as (out, _):
        rc = cli.cmd_buy(fc, _args(execute=True))
    assert rc == 1 and fc.submits == 0
    assert "ABORTED:" in out.getvalue() and "DRY-RUN" not in out.getvalue()
    assert _kinds(tmp) == ["aborted"]


def test_cli_submit_unknown_logged_and_capped(tmp=Path("/tmp/pp_cli_orders_5")):
    fc = _FakeClient(submit_exc=OrderOutcomeUnknown("order submit outcome unknown: timeout"))
    with _cli_env(tmp, tty=True, typed="BUY TSLA 40000") as (out, _):
        rc = cli.cmd_buy(fc, _args(execute=True))
    assert rc == 1 and "outcome UNKNOWN" in out.getvalue() and "paypay orders" in out.getvalue()
    assert _kinds(tmp) == ["submit_unknown"]
    assert audit.count_today(log_path=tmp / "orders.log") == 1   # counts toward the cap


def test_cli_max_pct_fails_closed_without_total(tmp=Path("/tmp/pp_cli_orders_6")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=True, typed="") as (out, _):
        cli._consolidated_holdings = lambda client: (_ for _ in ()).throw(RuntimeError("down"))
        rc = cli.cmd_buy(fc, _args())
    assert rc == 1 and "portfolio total unavailable" in out.getvalue()


def test_cli_max_pct_uses_portfolio_total(tmp=Path("/tmp/pp_cli_orders_7")):
    fc = _FakeClient()
    with _cli_env(tmp, tty=True, typed="", total=(100_000,)) as (out, _):
        rc = cli.cmd_buy(fc, _args())             # 40,000 / 100,000 = 40% > 10%
    assert rc == 1 and "of portfolio" in out.getvalue()


def test_cli_rejects_removed_flags():
    p = cli.build_parser()
    for argv in (["buy", "TSLA", "--qty", "1"], ["buy", "TSLA", "--amount", "1000", "--limit", "1"],
                 ["buy", "TSLA", "--amount", "1000", "--market-order"], ["buy", "TSLA"]):
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                p.parse_args(argv)
            assert False, argv
        except SystemExit:
            pass


if __name__ == "__main__":
    raise SystemExit(run(globals()))
