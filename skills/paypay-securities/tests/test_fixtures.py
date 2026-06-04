"""Parser regression tests over SANITIZED synthetic fixtures.

The PayPay証券 pages/APIs are non-public and unversioned — the parsers are the
most likely thing to break on a frontend redesign. These fixtures mirror the
*shape* of each source (no real account data) so a layout change is caught here.
Real-account fixtures stay in the gitignored repo-root tests/.
"""
from _runner import run
from paypay_sec import parsers


# ---------------------------------------------------------------- parse_summary
_SUMMARY_HTML = """
<div class="mypage_name">会員 P123456 様</div>
<div class="mypage_assets_data">100万0000円</div>
<div class="mypage_invest">90万円</div>
<div class="mypage_gain">10万円</div>
"""


def test_parse_summary():
    s = parsers.parse_summary(_SUMMARY_HTML)
    assert s.member_id == "P123456"
    assert s.total_valuation == 1_000_000
    assert s.principal == 900_000
    assert s.unrealized_pl == 100_000


def test_parse_summary_empty_is_graceful():
    s = parsers.parse_summary("<html><body></body></html>")
    assert s.member_id is None and s.total_valuation is None


# --------------------------------------------------------------- parse_holdings
_HOLDINGS_HTML = """
<table class="d_table">
  <tr><th>銘柄</th><th>評価額</th><th>株数</th><th>損益</th></tr>
  <tr><td>●テスト株</td><td>¥100,000(40.0%)</td><td>(1.500000)</td><td>¥90,000(+¥10,000)</td></tr>
  <tr><td>特定</td><td></td><td></td><td></td></tr>
  <tr><td>●現金</td><td>¥50,000(20.0%)</td><td>(—)</td><td>¥50,000(ー)</td></tr>
</table>
"""


def test_parse_holdings():
    hs = parsers.parse_holdings(_HOLDINGS_HTML)
    assert len(hs) == 2
    stock = hs[0]
    assert stock.name == "テスト株" and stock.valuation == 100_000
    assert stock.weight_pct == 40.0 and stock.shares == 1.5
    assert stock.principal == 90_000 and stock.unrealized_pl == 10_000
    assert "特定" in stock.account_types
    assert hs[1].is_cash is True


def test_parse_holdings_no_table_is_graceful():
    assert parsers.parse_holdings("<div>redesigned page</div>") == []


# ------------------------------------------------ parse_transactions / current_cash
_LEDGER = [
    {"SUMMARY_TYPE": "1", "AMOUNT": "-50000", "CASH_BALANCE": "50000", "BASE_D": "2026.01.01",
     "BRAND_NM": "TSLA", "PRICE": "300", "QTY": "1", "EXCHANGE_RATE": "150"},
    {"SUMMARY_TYPE": "31", "AMOUNT": "", "CASH_BALANCE": ""},           # 約定明細 → dropped
    {"SUMMARY_TYPE": "3", "AMOUNT": "100000", "CASH_BALANCE": "100000", "BASE_D": "2026.01.02"},
]


def test_parse_transactions_drops_detail_lines():
    txns = parsers.parse_transactions(_LEDGER)
    assert len(txns) == 2                       # type 31 dropped
    assert txns[0]["type"] == "買付" and txns[0]["amount"] == -50000
    assert txns[0]["fx"] == 150.0


def test_current_cash_and_throttled_empty():
    assert parsers.current_cash(_LEDGER) == 50000
    assert parsers.current_cash([]) is None     # throttled/empty feed → None, not 0


# --------------------------------------------------------------- parse_invtrust
_INV_TOP = {
    "INVEST_BRAND_ARRAY": {"0": {"BRAND_ID": "99", "SECURITIES_VALUE": "200000",
                                 "SUM_GROSS_PROFIT": "3000", "SELL_ORDER_AMOUNT": "0"}},
    "SECURITIES_VALUE_TOTAL": "200000", "TOTAL_ACQUISITION_FEE_TAX_TOTAL": "197000",
    "SUM_GROSS_PROFIT_TOTAL": "3000", "SELL_ORDER_AMOUNT_TOTAL": "0", "BUYABLE_CASH": "0",
}


def test_parse_invtrust():
    inv = parsers.parse_invtrust(_INV_TOP)
    assert inv.valuation == 200_000 and inv.principal == 197_000
    assert inv.unrealized_pl == 3_000
    assert len(inv.holdings) == 1 and inv.holdings[0]["valuation"] == 200_000


def test_parse_invtrust_empty_is_graceful():
    inv = parsers.parse_invtrust({})            # server hung / empty body
    assert inv.valuation is None and inv.holdings == []


def test_parse_invtrust_reserve_plan_flag():
    # INVEST_BRAND_RESERVE_STATUS_ARRAY (keyed by brand_id) flags active 定投/つみたて
    top = {**_INV_TOP,
           "INVEST_BRAND_ARRAY": {"0": {"BRAND_ID": "6", "SECURITIES_VALUE": "100000"},
                                  "1": {"BRAND_ID": "9", "SECURITIES_VALUE": "50000"}},
           "INVEST_BRAND_RESERVE_STATUS_ARRAY": {"6": {"RESERVE_ORDER_STATUS": 1},
                                                 "9": {"RESERVE_ORDER_STATUS": 0}}}
    inv = parsers.parse_invtrust(top)
    by = {h["brand_id"]: h for h in inv.holdings}
    assert by["6"]["reserve_plan"] is True      # active 定投
    assert by["9"]["reserve_plan"] is False     # held but no plan
    assert inv.reserve_brand_ids == ["6"]


# --------------------------------------------------- parse_invtrust_transactions
_INV_LEDGER = [
    {"SUMMARY_TYPE": "1", "AMOUNT": "-30000", "CASH_BALANCE": "0", "BASE_D": "2026.02.01",
     "BRAND_NM": "S&P500", "ACCOUNT_TYPE": "3", "PRICE": "20000", "QTY": "15000"},
    {"SUMMARY_TYPE": "31"},                      # dropped
]


def test_parse_invtrust_transactions():
    txns = parsers.parse_invtrust_transactions(_INV_LEDGER)
    assert len(txns) == 1
    assert txns[0]["brand"] == "S&P500" and txns[0]["amount"] == -30000
    assert txns[0]["qty"] == 15000.0


# ------------------------------------------------------------- parse_open_orders
# real /trade/preorder/ columns (headers verified 2026-06-04)
_PREORDER_HTML = """
<table class="d_table">
  <tr><th>受付日時 (執行日)</th><th>銘柄</th><th>売買</th><th>口座区分</th><th>金額・株数</th><th>ステータス</th><th>備考</th></tr>
  <tr><td>2026/06/05 09:00</td><td>TSLA</td><td>買付</td><td>特定</td><td>¥10,000</td><td>受付中 ORDER_NO:123456</td><td>—</td></tr>
</table>
"""


def test_parse_open_orders_canonical_columns():
    orders = parsers.parse_open_orders(_PREORDER_HTML)
    assert len(orders) == 1
    o = orders[0]
    assert o["symbol"] == "TSLA" and o["side"] == "買付"
    assert o["account_type"] == "特定" and o["size"] == "¥10,000"
    assert o["status"].startswith("受付中")
    assert o["datetime"].startswith("2026/06/05")
    assert o["order_id"] == "123456"


def test_parse_open_orders_header_only_is_empty():
    assert parsers.parse_open_orders("<table class='d_table'><tr><th>x</th></tr></table>") == []
    assert parsers.parse_open_orders("") == []


# --------------------------------------------- session-expired redirect resilience
def test_parsers_survive_login_redirect_html():
    # a session-expired fetch returns the login page, not the data table
    login_html = "<html><body><form id='login'>ログインしてください</form></body></html>"
    assert parsers.parse_holdings(login_html) == []
    assert parsers.parse_open_orders(login_html) == []
    assert parsers.parse_summary(login_html).total_valuation is None


if __name__ == "__main__":
    raise SystemExit(run(globals()))
