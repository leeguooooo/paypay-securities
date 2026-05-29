"""HTML/JS → structured data. All site-specific selectors live HERE so a
frontend redesign only touches this one module.

Sources (verified against live fixtures):
  - account header  : div.mypage_name / .mypage_invest / .mypage_gain  (man-yen text)
  - holdings detail : table.d_table on /trade/portfolio/brands/<market>  (¥ + comma text)
  - asset timeseries: var ticks/cashData/acuisitionData on /trade/history/<market> (万円)
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

from bs4 import BeautifulSoup

# ---------------------------------------------------------------- value parsing

_DASH = {"ー", "−", "-", "―", ""}


def parse_yen(text: Optional[str]) -> Optional[int]:
    """Parse a JPY amount in either '12万3456円' or '¥12,345' / '+¥678' form.
    Returns int yen, or None for blanks/dashes ('ー')."""
    if text is None:
        return None
    t = text.strip()
    if t in _DASH:
        return None
    cleaned = t.replace("¥", "").replace("円", "").replace(",", "").replace(" ", "").replace("+", "")
    sign = 1
    if cleaned[:1] in ("-", "−"):
        sign = -1
        cleaned = cleaned[1:]
    if not cleaned:
        return None
    if "万" in cleaned:
        man, _, rest = cleaned.partition("万")
        return sign * ((int(man) if man.isdigit() else 0) * 10000 + (int(rest) if rest.isdigit() else 0))
    return sign * int(cleaned) if cleaned.lstrip("-").isdigit() else None


def _find_yen(text: str) -> Optional[int]:
    """Pull the first money-looking token (must carry 円/¥/万) out of a blob.
    Whitespace is collapsed first because get_text() splits '16万0768円' into
    '16 万 0768 円'."""
    text = re.sub(r"\s+", "", text)
    cands = re.findall(r"[+\-−]?¥?\d[\d,]*万?\d*円?", text)
    cands = [c for c in cands if ("円" in c or "¥" in c or "万" in c)]
    return parse_yen(cands[0]) if cands else None


def _split_paren(s: str) -> tuple[str, str]:
    m = re.match(r"\s*([^(]*)\(([^)]*)\)", s)
    return (m.group(1).strip(), m.group(2).strip()) if m else (s.strip(), "")


# ---------------------------------------------------------------- models

@dataclass
class AccountSummary:
    member_id: Optional[str]
    total_valuation: Optional[int]   # 評価額合計 (yen)
    principal: Optional[int]         # 投資元本 (yen)
    unrealized_pl: Optional[int]     # 含み益/損 (yen)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Holding:
    name: str
    valuation: Optional[int]         # 評価額 (yen)
    weight_pct: Optional[float]      # 資産 (%)
    shares: Optional[float]          # 株数
    principal: Optional[int]         # 投資元本 (yen)
    unrealized_pl: Optional[int]     # 含み損益 (yen)
    account_types: list[str] = field(default_factory=list)  # NISA / 特定 …
    is_cash: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- parsers

def parse_summary(html: str) -> AccountSummary:
    soup = BeautifulSoup(html, "lxml")

    def text_of(selector_classes):
        for cls in selector_classes:
            el = soup.find(class_=cls)
            if el:
                return " ".join(el.get_text(" ", strip=True).split())
        return ""

    name_txt = text_of(["mypage_name", "mypage_assets_data"])
    assets_txt = text_of(["mypage_assets_data", "mypage_name"])
    member = None
    m = re.search(r"(P\d{6,})", name_txt)
    if m:
        member = m.group(1)
    return AccountSummary(
        member_id=member,
        total_valuation=_find_yen(assets_txt),   # value lives in mypage_assets_data
        principal=_find_yen(text_of(["mypage_invest"])),
        unrealized_pl=_find_yen(text_of(["mypage_gain"])),
    )


@dataclass
class InvTrustSummary:
    valuation: Optional[int]        # SECURITIES_VALUE_TOTAL (評価額合計)
    principal: Optional[int]        # TOTAL_ACQUISITION_FEE_TAX_TOTAL (投資元本)
    unrealized_pl: Optional[int]    # SUM_GROSS_PROFIT_TOTAL (含み損益)
    sell_order_pending: Optional[int]  # SELL_ORDER_AMOUNT_TOTAL (売却申込中)
    buyable_cash: Optional[int]     # BUYABLE_CASH
    holdings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _to_int(v) -> Optional[int]:
    if v in (None, ""):
        return None
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def parse_invtrust(top: dict) -> InvTrustSummary:
    """Parse the /v2/invest/brand/pc_invest_top JSON payload."""
    arr = top.get("INVEST_BRAND_ARRAY") or {}
    rows = arr.values() if isinstance(arr, dict) else arr
    holdings = [{
        "brand_id": h.get("BRAND_ID"),
        "valuation": _to_int(h.get("SECURITIES_VALUE")),
        "unrealized_pl": _to_int(h.get("SUM_GROSS_PROFIT")),
        "sell_order_pending": _to_int(h.get("SELL_ORDER_AMOUNT")),
    } for h in rows]
    return InvTrustSummary(
        valuation=_to_int(top.get("SECURITIES_VALUE_TOTAL")),
        principal=_to_int(top.get("TOTAL_ACQUISITION_FEE_TAX_TOTAL")),
        unrealized_pl=_to_int(top.get("SUM_GROSS_PROFIT_TOTAL")),
        sell_order_pending=_to_int(top.get("SELL_ORDER_AMOUNT_TOTAL")),
        buyable_cash=_to_int(top.get("BUYABLE_CASH")),
        holdings=holdings,
    )


def parse_holdings(html: str) -> list[Holding]:
    """Parse the table.d_table on /trade/portfolio/brands/<market>."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="d_table")
    holdings: list[Holding] = []
    if not table:
        return holdings
    current: Optional[Holding] = None
    for tr in table.find_all("tr"):
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tr.find_all(["td", "th"])]
        if len(cells) < 4 or cells[0] in ("銘柄",):
            continue
        first = cells[0]
        if first.startswith("●"):
            name = first.lstrip("●").strip()
            is_cash = name in ("現金", "現 金")
            val, pct = _split_paren(cells[1])
            _, shares_txt = _split_paren(cells[2])
            principal, pnl = _split_paren(cells[3])
            current = Holding(
                name=name,
                valuation=parse_yen(val),
                weight_pct=float(pct.rstrip("%")) if pct.rstrip("%").replace(".", "", 1).isdigit() else None,
                shares=float(re.sub(r"[^\d.]", "", shares_txt)) if re.search(r"\d", shares_txt) else None,
                principal=parse_yen(principal),
                unrealized_pl=parse_yen(pnl),
                is_cash=is_cash,
            )
            holdings.append(current)
        elif current is not None and first and first not in _DASH:
            # account-type breakdown row (NISA / 特定 …)
            current.account_types.append(first)
    return holdings


# transaction-ledger SUMMARY_TYPE codes (inferred from amount sign + holdings)
SUMMARY_TYPES = {"1": "買付", "2": "売却", "3": "入金", "4": "出金",
                 "8": "手数料/税", "31": "約定明細", "54": "手数料"}


def parse_transactions(records: list) -> list[dict]:
    """Clean the settlement ledger into trade rows. Drops the paired execution-
    detail lines (type 31, no cash balance) to leave a clean cash ledger."""
    def num(v):
        try:
            return float(v) if v not in (None, "") else None
        except (TypeError, ValueError):
            return None

    out = []
    for r in records:
        st = r.get("SUMMARY_TYPE")
        if st == "31":
            continue
        amt = num(r.get("AMOUNT"))
        bal = num(r.get("CASH_BALANCE"))
        out.append({
            "date": r.get("BASE_D"),
            "type": SUMMARY_TYPES.get(st, f"type{st}"),
            "brand": r.get("BRAND_NM"),
            "price": num(r.get("PRICE")),
            "qty": num(r.get("QTY")),
            "amount": int(round(amt)) if amt is not None else None,
            "fx": num(r.get("EXCHANGE_RATE")),
            "cash_balance": int(round(bal)) if bal is not None else None,
        })
    return out


def current_cash(records: list) -> Optional[int]:
    """Latest running cash balance (= the app's 現金) from the ledger."""
    for r in records:
        bal = r.get("CASH_BALANCE")
        if bal not in (None, ""):
            try:
                return int(round(float(bal)))
            except (TypeError, ValueError):
                pass
    return None


def parse_cash_balance(html: str) -> Optional[int]:
    """Securities settled cash today (証券 現金残高) from /trade/client/moneyschedule.
    Layout: '… 受渡日 現金残高 出金可能金額 2026.05.29 本日 5万0002円 2円 …'."""
    txt = re.sub(r"\s+", "", BeautifulSoup(html, "lxml").get_text(" "))
    m = re.search(r"本日([+\-]?\d[\d,]*万?\d*円)", txt)
    return parse_yen(m.group(1)) if m else None


def _js_array(html: str, var: str):
    m = re.search(rf"var\s+{var}\s*=\s*(\[.*?\])\s*;", html, re.S)
    return json.loads(m.group(1)) if m else None


def parse_history_series(html: str) -> dict:
    """Daily asset/cash time series from the history page JS (values in 万円)."""
    ticks = _js_array(html, "ticks") or []
    cash = _js_array(html, "cashData") or []
    acq = _js_array(html, "acuisitionData") or []  # site's spelling
    cash_map = {row[0]: row[1] for row in cash}
    acq_map = {row[0]: row[1] for row in acq}

    def yen(man):
        return int(round(man * 10000)) if man is not None else None

    series = []
    for idx, label in ticks:
        series.append({
            "label": label,
            "cash_yen": yen(cash_map.get(idx)),
            "asset_yen": yen(acq_map.get(idx)),  # 取得/資産 acquisition value
        })

    def grab(pat):
        m = re.search(pat, html)
        return m.group(1) if m else None

    return {
        "from_date": grab(r"var\s+fromDate\s*=\s*'([^']+)'"),
        "end_date": grab(r"var\s+endDate\s*=\s*'([^']+)'"),
        "points": series,
    }
