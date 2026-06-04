from _runner import run
from paypay_sec import cli


def test_no_double_count_of_invtrust_tax_and_transfer():
    # the live-verified case: 証券 cash-ledger fees == 投信 tax + transfer (0 commission).
    # total must be cash-ledger + FX, NOT cash-ledger + tax + transfer + FX.
    mc = cli._measured_cost(explicit_fees=1542, fx_cost=916, inv_tax=1102, inv_transfer=440)
    assert mc["total_cost"] == 2458             # 1542 + 916, NOT 4000
    assert mc["securities_fee_residual"] == 0   # 1542 - 1102 - 440
    assert mc["cost_reconciles"] is True


def test_residual_when_securities_have_own_fees():
    mc = cli._measured_cost(explicit_fees=2000, fx_cost=500, inv_tax=1102, inv_transfer=440)
    assert mc["securities_fee_residual"] == 458   # 2000 - 1542
    assert mc["total_cost"] == 2500
    assert mc["cost_reconciles"] is True


def test_incomplete_ledger_window_flagged():
    # 投信 ledger shows more tax/transfer than the (short) 証券 cash window captured
    mc = cli._measured_cost(explicit_fees=500, fx_cost=100, inv_tax=1102, inv_transfer=440)
    assert mc["cost_reconciles"] is False   # residual negative → window missed rows
    assert mc["securities_fee_residual"] < 0


def test_handles_none():
    mc = cli._measured_cost(None, None, None, None)
    assert mc["total_cost"] == 0 and mc["cost_reconciles"] is True


if __name__ == "__main__":
    raise SystemExit(run(globals()))
