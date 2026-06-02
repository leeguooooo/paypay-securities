from pathlib import Path

from _runner import run
from paypay_sec.client import PayPayClient
from paypay_sec.config import Settings
from paypay_sec.orders import build


# The web confirm/submit responses are {STATUS, HTML}; the order fields ride in
# the HTML as hidden <input>s (ORDER_CONFIRM_NO + the echo-back form). client.py
# extracts them with a name=value regex, so the fixtures mirror that shape.
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
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeCookies:
    def __init__(self, **kw):
        self._d = kw

    def get(self, key, default=None):
        return self._d.get(key, default)


class FakeSession:
    """Routes the confirm (…_popup.json) and submit (…_complete) POSTs to
    separate canned responses so a test can drive the whole confirm→submit flow."""

    def __init__(self, *, live=None, popup=None, complete=None, cookies=None):
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
        self.cookies = cookies or FakeCookies()
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return FakeResponse(self.live)

    def post(self, url, data=None, **kwargs):
        self.post_calls.append((url, data or {}, kwargs))
        return FakeResponse(self.complete if "complete" in url else self.popup)


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
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000, limit=313.3)

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


def test_order_confirm_estimates_amount_for_qty_request():
    fake = FakeSession()
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", qty=1, limit=313.3)

    c.order_confirm(req)

    _, data, _ = fake.post_calls[0]
    assert data["val"] == 50009  # ceil(1 * 313.3 * 159.62)


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

    req = build(market="usa", symbol="AAPL", side="buy", amount_jpy=1000, limit=313.3)
    preview = c.order_confirm(req)

    assert preview["brand_id"] == "4"
    # the brand-detail GET resolves against ajax_detail/<brand_id>/0
    assert fake.get_calls[0][0].endswith("/trade/brand/ajax_detail/4/0")


def test_order_submit_requires_trade_password():
    fake = FakeSession()
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000, limit=313.3)
    c.order_confirm(req)

    try:
        c.order_submit("CONF123", req)  # no TRADE_PASSWORD
        assert False, "expected RuntimeError (missing TRADE_PASSWORD)"
    except RuntimeError as e:
        assert "TRADE_PASSWORD" in str(e) or "取引パスワード" in str(e)
    # nothing was sent to the _complete endpoint
    assert not any("complete" in u for u, _, _ in fake.post_calls)


def test_order_submit_places_and_consumes_token():
    fake = FakeSession(cookies=FakeCookies(fuel_csrf_token="csrf-abc"))
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000, limit=313.3)
    c.order_confirm(req)

    out = c.order_submit("CONF123", req, trade_password="secret")

    assert out["order_id"] == "ORD999"
    complete = [(u, d) for u, d, _ in fake.post_calls if "complete" in u]
    assert len(complete) == 1
    url, data = complete[0]
    assert url.endswith("/trade/brand/ajax_buy_complete")
    assert data["ORDER_CONFIRM_NO"] == "CONF123"
    assert data["TRADE_PASSWORD"] == "secret"
    assert data["CSRF_TOKEN"] == "csrf-abc"            # from the fuel_csrf_token cookie
    # the confirm token is single-use — a replay must fail
    try:
        c.order_submit("CONF123", req, trade_password="secret")
        assert False, "token should be single-use"
    except RuntimeError:
        pass


def test_order_confirm_reports_popup_rejection():
    fake = FakeSession(popup={"STATUS": False, "MESSAGE_ARRAY": ["買付可能金額を超える金額が指定されています"]})
    c = _client(fake)
    req = build(market="usa", symbol="4", side="buy", amount_jpy=1000, limit=313.3)

    try:
        c.order_confirm(req)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "買付可能金額" in str(e)


if __name__ == "__main__":
    raise SystemExit(run(globals()))
