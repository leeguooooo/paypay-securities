from pathlib import Path

import requests

from _runner import run
from paypay_sec.client import PayPayClient, OrderOutcomeUnknown
from paypay_sec.config import Settings
from paypay_sec.orders import build


# The web confirm/submit responses are {STATUS, HTML}; the order fields ride in
# the HTML as hidden <input>s (ORDER_CONFIRM_NO + the echo-back form). client.py
# parses them with BeautifulSoup, so attribute order / quoting don't matter.
_POPUP_HTML = (
    "<form id='buy_form'>"
    "<input type='hidden' name='ORDER_CONFIRM_NO' value='CONF123' />"
    "<input type='hidden' name='ORDER_AMOUNT' value='1000' />"
    "<input type='hidden' name='ORDER_PRICE' value='313.3' />"
    "<input type='hidden' name='ORDER_FEE' value='0' />"
    "<input type='hidden' name='BRAND_ID' value='4' />"
    "</form>"
)
_COMPLETE_HTML = "<input type='hidden' name='ORDER_NO' value='ORD999' />"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = "json"

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def FakeCookies(**kw):
    jar = requests.cookies.RequestsCookieJar()
    for k, v in kw.items():
        jar.set(k, v, domain="www.paypay-sec.co.jp", path="/")
    return jar


class FakeSession:
    """Routes the page GET (CSRF seed), ajax_detail GET, confirm (…_popup.json),
    submit (…_complete) and receipt (…_complete_popup.json) to canned responses so a
    test can drive the whole confirm→submit flow. `popup` may be a list (one per call)."""

    def __init__(self, *, live=None, popup=None, complete=None, cookies=None, post_exc=None,
                 receipt=None, receipt_exc=None, seed_csrf="csrf-seeded"):
        self.live = live or {
            "STATUS": True,
            "PREORDERABLE": 0,
            "brand": {
                "BRAND_ID": "4",
                "BRAND_CD": "AAPL",
                "PRICE": 313.3,
                "EXCHANGE_RATE": 159.62,
                "ORDER_AMOUNT_LOWER": "1.00000",
            },
        }
        self.popup = popup or {"STATUS": True, "HTML": _POPUP_HTML}
        self.complete = complete or {"STATUS": True, "HTML": _COMPLETE_HTML}
        self.cookies = cookies if cookies is not None else FakeCookies()
        self.post_exc = post_exc
        self.receipt = receipt if receipt is not None else {"STATUS": True, "HTML": ""}
        self.receipt_exc = receipt_exc
        self.seed_csrf = seed_csrf
        self.get_calls = []
        self.post_calls = []
        self.calls = []            # ordered (method, url) across GET + POST

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        self.calls.append(("GET", url))
        if "/trade/brand/buy/" in url or "/trade/brand/sell/" in url:   # full order page
            if self.seed_csrf is not None:
                self.cookies.set("fuel_csrf_token", self.seed_csrf,
                                 domain="www.paypay-sec.co.jp", path="/")
            return FakeResponse("<html></html>")
        return FakeResponse(self.live)

    def post(self, url, data=None, **kwargs):
        self.post_calls.append((url, data or {}, kwargs))
        self.calls.append(("POST", url))
        if "complete_popup" in url:
            if self.receipt_exc is not None:
                raise self.receipt_exc
            return FakeResponse(self.receipt)
        if "complete" in url:
            if self.post_exc is not None:
                raise self.post_exc
            return FakeResponse(self.complete)
        if isinstance(self.popup, list):
            return FakeResponse(self.popup.pop(0))
        return FakeResponse(self.popup)


def _client(fake):
    c = PayPayClient(
        Settings(member_id="m", password="p", cookie="SMS_AUTH_STRING=x"),
        session_file=Path("/tmp/pp_test_client_orders_session.json"),
        use_cache=False,
    )
    c._from_cache = True
    c._session = fake
    return c


def test_order_confirm_posts_buy_popup_only():
    fake = FakeSession()
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)

    preview = c.order_confirm(req)

    assert preview["token"] == "CONF123"
    assert preview["total_jpy"] == 1000
    assert preview["brand_id"] == "4"
    url, data, _ = fake.post_calls[0]
    assert url.endswith("/trade/brand/ajax_buy_popup.json")
    assert data["val"] == 1000
    assert data["ACCOUNT_TYPE"] == 2
    # confirm is a preview only — it must never hit the _complete (real order) endpoint
    assert not any("complete" in u for u, _, _ in fake.post_calls)


def test_order_confirm_resolves_symbol_from_summary_page():
    fake = FakeSession()
    c = _client(fake)
    c.summary_html = lambda market="usa": """
        <div class="mypage_brand_icon">
          <a href="/trade/brand/4/0">
            <table style="background-image:url('304x304_aapl.png')"></table>
          </a>
        </div>
    """

    req = build(market="usa", symbol="AAPL", side="buy", amount_jpy=1000)
    preview = c.order_confirm(req)

    assert preview["brand_id"] == "4"
    # CSRF seed (full page GET), then the brand-detail GET against ajax_detail/<brand_id>/0
    assert fake.get_calls[0][0].endswith("/trade/brand/buy/4")
    assert fake.get_calls[1][0].endswith("/trade/brand/ajax_detail/4/0")


def test_order_submit_requires_trade_password():
    fake = FakeSession()
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)
    c.order_confirm(req)

    try:
        c.order_submit("CONF123", req)  # no TRADE_PASSWORD
        assert False, "expected RuntimeError (missing TRADE_PASSWORD)"
    except RuntimeError as e:
        assert "TRADE_PASSWORD" in str(e) or "取引パスワード" in str(e)
    # nothing was sent to the _complete endpoint
    assert not any("complete" in u for u, _, _ in fake.post_calls)


def test_order_submit_places_and_consumes_token():
    fake = FakeSession()
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)
    c.order_confirm(req)

    out = c.order_submit("CONF123", req, trade_password="secret")

    assert out["order_id"] == "ORD999"
    complete = [(u, d) for u, d, _ in fake.post_calls if "complete" in u]
    assert len(complete) == 2                          # order, then the receipt popup
    url, data = complete[0]
    assert url.endswith("/trade/brand/ajax_buy_complete")
    assert data["ORDER_CONFIRM_NO"] == "CONF123"
    assert data["TRADE_PASSWORD"] == "secret"
    assert data["CSRF_TOKEN"] == "csrf-seeded"         # seeded by the order-page GET
    assert data["BRAND_ID"] == "4" and data["PREORDER"] == "0"
    rurl, rdata = complete[1]
    assert rurl.endswith("/trade/brand/ajax_buy_complete_popup.json")
    assert rdata["BRAND_ID"] == "4" and rdata["CSRF_TOKEN"] == "csrf-seeded"
    # the confirm token is single-use — a replay must fail
    try:
        c.order_submit("CONF123", req, trade_password="secret")
        assert False, "token should be single-use"
    except RuntimeError:
        pass


def test_order_confirm_reports_popup_rejection():
    fake = FakeSession(popup={"STATUS": False, "MESSAGE_ARRAY": ["買付可能金額を超える金額が指定されています"]})
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)

    try:
        c.order_confirm(req)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "買付可能金額" in str(e)


def _confirmed(**kw):
    fake = FakeSession(**kw)
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)
    c.order_confirm(req)
    return fake, c, req


def test_order_submit_status_must_be_strictly_true():
    for status in (False, 0, "0", "false", None, {}):
        _, c, req = _confirmed(complete={"STATUS": status, "MESSAGE": "NG", "ORDER_NO": "X"})
        try:
            c.order_submit("CONF123", req, trade_password="secret")
            assert False, f"STATUS={status!r} should be a rejection"
        except OrderOutcomeUnknown:
            assert False, "an explicit STATUS is a known rejection, not unknown"
        except RuntimeError as e:
            assert "rejected" in str(e)


def test_order_submit_missing_order_no_is_none():
    _, c, req = _confirmed(complete={"STATUS": 1, "HTML": "<p>done</p>"})
    out = c.order_submit("CONF123", req, trade_password="secret")
    assert out["order_id"] is None          # never falls back to the confirm no / token


def test_order_submit_order_no_from_json():
    _, c, req = _confirmed(complete={"STATUS": "1", "ORDER_NO": 12345})
    assert c.order_submit("CONF123", req, trade_password="secret")["order_id"] == "12345"


def test_order_submit_transport_failure_is_outcome_unknown():
    for exc in (requests.Timeout("read timed out"), requests.ConnectionError("reset")):
        fake, c, req = _confirmed(post_exc=exc)
        try:
            c.order_submit("CONF123", req, trade_password="secret")
            assert False, "expected OrderOutcomeUnknown"
        except OrderOutcomeUnknown:
            pass
        # the token is consumed even when the outcome is unknown — no silent resend
        try:
            c.order_submit("CONF123", req, trade_password="secret")
            assert False
        except OrderOutcomeUnknown:
            assert False, "replay must not reach the network"
        except RuntimeError:
            pass
        assert sum(u.endswith("_complete") for u, _, _ in fake.post_calls) == 1
        assert not any("complete_popup" in u for u, _, _ in fake.post_calls)


def test_order_submit_5xx_and_bad_json_are_outcome_unknown():
    for complete, code in ((ValueError("no json"), 200), ({"STATUS": True}, 502)):
        fake = FakeSession(complete=complete)
        fake_post = fake.post
        fake.post = lambda url, data=None, **kw: (
            FakeResponse(complete, code) if "complete" in url else fake_post(url, data, **kw))
        c = _client(fake)
        req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)
        c.order_confirm(req)
        try:
            c.order_submit("CONF123", req, trade_password="secret")
            assert False, "expected OrderOutcomeUnknown"
        except OrderOutcomeUnknown:
            pass


def test_hidden_fields_value_before_name_and_quotes():
    html = ('<input value="CONF9" type=hidden name=ORDER_CONFIRM_NO>'
            "<input type='hidden' value='1,000' name='ORDER_AMOUNT'/>"
            '<input name="EMPTY" value="">')
    f = PayPayClient._hidden_fields(html)
    assert f["ORDER_CONFIRM_NO"] == "CONF9" and f["ORDER_AMOUNT"] == "1,000" and f["EMPTY"] == ""


def test_confirm_rejection_hints_market_closed():
    fake = FakeSession(popup={"STATUS": "0", "MESSAGE": "受付時間外"})   # both refuse
    c = _client(fake)
    try:
        c.order_confirm(build(market="usa", symbol="4", side="buy", amount_jpy=1000))
        assert False
    except RuntimeError as e:
        assert "受付時間外" in str(e) and "予約注文 also refused" in str(e)
    assert sum("popup" in u for u, _, _ in fake.post_calls) == 2   # preorder tried, refused


def test_preorder_status_ok_but_not_preorder_shape_is_rejected():
    # a STATUS-true popup without the preorder DATA must not be taken as a preorder
    fake = FakeSession(popup=[_CLOSED, {"STATUS": True, "HTML": ""}])
    c = _client(fake)
    try:
        c.order_confirm(build(market="usa", symbol="4", side="buy", amount_jpy=1000))
        assert False
    except RuntimeError as e:
        assert "買付可能金額" in str(e)


def test_preorder_fallback_even_when_detail_says_not_preorderable():
    # live 2026-10-05: ajax_detail reports PREORDERABLE=0 while the market is closed;
    # only the preorder=1 popup carries PREORDERABLE:1 — don't gate on the detail
    fake = FakeSession(popup=[_CLOSED, _PREORDER_POPUP])      # default live: PREORDERABLE 0
    c = _client(fake)
    pv = c.order_confirm(build(market="usa", symbol="4", side="buy", amount_jpy=1000))
    assert pv["preorder"] is True


def test_symbol_resolves_via_brand_cd_when_cards_have_no_logo():
    fake = FakeSession()                                      # live brand BRAND_CD = AAPL
    c = _client(fake)
    c.summary_html = lambda market="usa": (
        '<div class="mypage_brand_icon"><a href="/trade/brand/4/0">x</a></div>')
    pv = c.order_confirm(build(market="usa", symbol="aapl", side="buy", amount_jpy=1000))
    assert pv["brand_id"] == "4"


def test_csrf_seeded_before_popup():
    fake = FakeSession()
    c = _client(fake)
    c.order_confirm(build(market="usa", symbol="4", side="sell", amount_jpy=1000))
    seed = fake.calls.index(("GET", "https://www.paypay-sec.co.jp/trade/brand/sell/4"))
    popup = next(i for i, (m, u) in enumerate(fake.calls) if u.endswith("ajax_sell_popup.json"))
    assert seed < popup
    assert c._csrf() == "csrf-seeded"


def test_submit_refuses_without_csrf():
    fake, c, req = _confirmed(seed_csrf=None)
    try:
        c.order_submit("CONF123", req, trade_password="secret")
        assert False
    except OrderOutcomeUnknown:
        assert False, "nothing was sent — must not be 'unknown'"
    except RuntimeError as e:
        assert "csrf" in str(e).lower()
    assert not any("complete" in u for u, _, _ in fake.post_calls)


_CLOSED = {"STATUS": False, "MESSAGE_ARRAY": ["買付可能金額を超える金額が指定されています"]}
_PREORDER_POPUP = {"STATUS": True, "DATA": {
    "brand": {"BRAND_ID": "4", "PRICE": 310.0, "EXCHANGE_RATE": 150.0},
    "PREORDERABLE": 1, "PREORDER_EXECUTE_DATE": "2026/06/09"}}


def _preorder_live():
    return {"STATUS": True, "PREORDERABLE": 1,
            "brand": {"BRAND_ID": "4", "PRICE": 313.3, "EXCHANGE_RATE": 159.62,
                      "ORDER_AMOUNT_LOWER": "1.00000"}}


def test_preorder_fallback_and_popup_shape():
    fake = FakeSession(live=_preorder_live(), popup=[_CLOSED, _PREORDER_POPUP])
    c = _client(fake)
    pv = c.order_confirm(build(market="usa", symbol="4", side="buy", amount_jpy=1000))
    popups = [d for u, d, _ in fake.post_calls if u.endswith("ajax_buy_popup.json")]
    assert [d["preorder"] for d in popups] == [0, 1]
    assert pv["preorder"] is True and pv["execute_date"] == "2026/06/09"
    assert pv["token"] and pv["total_jpy"] == 1000 and pv["est_price"] == 310.0


def test_preorder_submit_echoes_preorder_and_empty_confirm_no():
    fake = FakeSession(live=_preorder_live(), popup=[_CLOSED, _PREORDER_POPUP],
                       complete={"STATUS": True, "DATA": {"ORDER_NO": "P77"}})
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000)
    pv = c.order_confirm(req)
    out = c.order_submit(pv["token"], req, trade_password="secret")
    _, data, _ = next(x for x in fake.post_calls if x[0].endswith("_complete"))
    assert data["PREORDER"] == "1" and data["ORDER_CONFIRM_NO"] == ""
    assert data["BRAND_ID"] == "4" and data["ORDER_AMOUNT"] == "1000"
    assert data["ORDER_PRICE"] == "310.0" and data["ORDER_EXCHANGE_RATE"] == "150.0"
    assert out["preorder"] is True and out["order_id"] == "P77"


def test_immediate_popup_without_confirm_no_gives_empty_token():
    fake = FakeSession(popup={"STATUS": True, "HTML": "<input name='ORDER_AMOUNT' value='1000'>"})
    c = _client(fake)
    pv = c.order_confirm(build(market="usa", symbol="4", side="buy", amount_jpy=1000))
    assert pv["token"] == "" and pv["preorder"] is False   # orders.place aborts on this


def test_receipt_failure_is_still_submitted():
    for kw in ({"receipt_exc": requests.Timeout("slow")}, {"receipt": {"STATUS": False}}):
        _, c, req = _confirmed(complete={"STATUS": True, "HTML": ""}, **kw)
        out = c.order_submit("CONF123", req, trade_password="secret")
        assert out["order_id"] is None          # CLI then says "verify with paypay orders"


def test_order_id_from_receipt_html():
    _, c, req = _confirmed(complete={"STATUS": True},
                           receipt={"STATUS": True, "HTML": _COMPLETE_HTML})
    assert c.order_submit("CONF123", req, trade_password="secret")["order_id"] == "ORD999"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
