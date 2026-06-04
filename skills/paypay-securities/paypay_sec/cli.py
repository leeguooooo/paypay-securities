"""paypay — read-only CLI for the PayPay証券 web frontend (Phase 1).

  paypay login                 verify login (token + no-SMS)
  paypay balance  [-m usa]     account summary (valuation / principal / P&L)
  paypay portfolio [-m usa]    holdings with per-position detail
  paypay history  [-m usa]     daily asset/cash time series

Add --json to any command for machine-readable output. Credentials come from
.env / environment (see .env.example) — never passed on the command line.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import contextlib
import getpass
import io
import json
import os
import re
import sys
import unicodedata

import requests

from .client import LoginError, PayPayClient
from .config import Settings
from . import parsers, costs, market, config, report, i18n, snapshots
from . import orders as _orders, guards as _guards, audit as _audit
from datetime import datetime, timezone, timedelta

# Output language for human-facing (table/lark) renders; set from --lang in main().
# JSON output is never localized — its keys are a stable machine contract.
_LANG = "ja"

# JST — PayPay証券 reports in Japan time; as_of stamps use it.
_JST = timezone(timedelta(hours=9))


def _now_jst_str() -> str:
    return datetime.now(timezone.utc).astimezone(_JST).strftime("%Y-%m-%d %H:%M JST")


# --all pages until NEXT_FLG=false; this is the upper bound that keeps a stuck
# feed from looping forever (settlement_records already stops at NEXT_FLG).
_ALL_PAGES_CAP = 500


def _pages(args, default: int) -> int:
    """Resolve the ledger page count: a big cap when --all, else --pages/default."""
    if getattr(args, "fetch_all", False):
        return _ALL_PAGES_CAP
    return getattr(args, "pages", default)


def _measured_cost(explicit_fees, fx_cost, inv_tax, inv_transfer) -> dict:
    """Measured trading cost. 投信譲渡益税 + 送金手数料 already settle in the 証券 cash
    ledger (so they're inside explicit_fees) — they are a BREAKDOWN, not extra cost.
    The only cost outside the ledger is the reconstructed FX spread. residual<0 means
    the cash-ledger window missed some 投信 rows (→ --all)."""
    ef, fx = explicit_fees or 0, fx_cost or 0
    residual = ef - (inv_tax or 0) - (inv_transfer or 0)
    return {"total_cost": ef + fx, "securities_fee_residual": residual,
            "cost_reconciles": residual >= 0}


def _basis_hint(reconciles: bool, fetched_all: bool) -> str:
    """Warn (don't hide) when realized P&L rests on an incomplete cost basis —
    i.e. a sell with no matching buy in the fetched window. Points at --all."""
    if reconciles:
        return ""
    tail = "" if fetched_all else " — `--all` で全履歴を取得して再計算を"
    return ("⚠ 売却>取得の銘柄あり: 取得原価が不足 → 実現損益・原価が過小/不完全の可能性"
            + tail)


def _yen(v):
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}¥{abs(v):,}"


# --- display-width-aware padding (CJK full-width chars count as 2 columns) ---
def _dw(s) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _trunc(s, width: int) -> str:
    s, out, cur = str(s), "", 0
    for c in s:
        cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        if cur + cw > width:
            break
        out += c
        cur += cw
    return out


def _lj(s, width: int) -> str:   # left-justify to display width
    s = _trunc(s, width)
    return s + " " * max(0, width - _dw(s))


def _rj(s, width: int) -> str:   # right-justify to display width
    s = _trunc(s, width)
    return " " * max(0, width - _dw(s)) + s


def _signed_yen(v):
    if v is None:
        return "—"
    return f"+¥{v:,}" if v >= 0 else f"-¥{abs(v):,}"


def _fmt(args) -> str:
    if getattr(args, "json", False):
        return "json"
    return getattr(args, "fmt", None) or "table"


SCHEMA_VERSION = "1.0"


def _json_dump(obj) -> str:
    """JSON for machine consumers. Dict payloads get a stable schema_version stamped
    first (so cron/dashboards/daily-reports can version their parsing)."""
    if isinstance(obj, dict) and "schema_version" not in obj:
        obj = {"schema_version": SCHEMA_VERSION, **obj}
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _run_localized(render, payload) -> None:
    """Run a render fn (which prints) and localize its output per _LANG.
    JSON never reaches here, so machine output is unaffected."""
    if _LANG == "zh":
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render(payload)
        sys.stdout.write(i18n.localize(buf.getvalue(), _LANG))
    else:
        render(payload)


_SOURCE_LABELS = {"securities": "証券", "invtrust": "投信", "cash": "現金",
                  "fx": "為替", "ledger": "取引履歴", "invledger": "投信履歴", "names": "投信名称"}


def _source_warnings(sources: dict) -> list:
    """Loud, non-silent warnings for any source that failed or went stale, so a
    report never looks complete when a feed actually dropped out."""
    warn = []
    for k, v in (sources or {}).items():
        lab = _SOURCE_LABELS.get(k, k)
        if v == "failed":
            warn.append(f"⚠ {lab} の取得に失敗 — この値は本回欠落/不完全の可能性")
        elif v == "stale":
            warn.append(f"⚠ {lab} は stale(前回キャッシュ値, ライブ取得できず)")
    return warn


def _freshness_block(p: dict, lines) -> None:
    """Append the as_of stamp + per-source freshness + warnings to `lines`
    (a list the caller `print`s/joins). Used by table & lark renderers."""
    if p.get("as_of"):
        lines.append(f"  查询时间 {p['as_of']}")
    src = p.get("sources")
    if src:
        lines.append("  データ鮮度: " + "  ".join(f"{_SOURCE_LABELS.get(k, k)}={v}" for k, v in src.items()))
        for w in _source_warnings(src):
            lines.append("  " + w)


def _emit_fmt(payload, fmt, table_fn, lark_fn) -> None:
    if fmt == "json":
        print(_json_dump(payload))
    elif fmt == "lark":
        _run_localized(lark_fn, payload)
    else:
        _run_localized(table_fn, payload)


def _emit(obj, as_json: bool, render):
    if as_json:
        print(_json_dump(obj))
    else:
        _run_localized(render, obj)


def _persist_cash(client: PayPayClient, cash):
    """現金 with a persistent fallback for when the settlement ledger throttles to
    empty. Pass the freshly-parsed cash (may be None); on success it's cached to
    disk, on failure we fall back to the last good value. Returns (yen, fresh) —
    fresh=False means the number is a stale disk value, not live; None = nothing
    on record yet, so callers must NOT silently treat it as ¥0."""
    path = client.session_file.parent / "last_cash.json"
    if cash is not None:
        try:
            path.write_text(json.dumps({"cash": int(cash)}), encoding="utf-8")
        except OSError:
            pass
        return cash, True
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("cash"), False
    except (OSError, ValueError):
        return None, False


def _expected_phrase(req) -> str:
    qty_or_amt = req.qty if req.qty is not None else req.amount_jpy
    # normalize numbers like 1.0 -> "1"
    q = int(qty_or_amt) if float(qty_or_amt).is_integer() else qty_or_amt
    return f"{req.symbol} {q}"


def make_confirmer(reader=input):
    """Return confirmer(req, preview) -> bool. Requires the user to type the exact
    '<SYMBOL> <qty|amount>' phrase — defends against a reflexive Enter."""
    def confirmer(req, preview) -> bool:
        want = _expected_phrase(req)
        side = req.side.value.upper()
        total = (preview or {}).get("total_jpy")
        size = f"qty {req.qty}" if req.qty is not None else f"¥{req.amount_jpy:,}"
        total_frag = f"  est. total ¥{total:,}" if total is not None else ""
        print(f"\n⚠ LIVE ORDER — {side} {req.symbol} {size}{total_frag}")
        print(f"  To place this order, type exactly:  {want}")
        try:
            typed = (reader(f"  confirm> ") or "").strip()
        except (EOFError, KeyboardInterrupt):
            return False
        return typed == want
    return confirmer


def cmd_login(client: PayPayClient, args) -> int:
    info = client.login()
    out = {
        "status": bool(info.get("STATUS")),
        "need_sms": bool(info.get("IF_NEED_SMS_FLG")),
        "token_acquired": bool(client.token),
        "device_trusted": client.settings.has_device_token,
    }
    _emit(out, args.json, lambda o: print(
        f"login: {'OK' if o['status'] else 'FAILED'}  "
        f"sms_required={o['need_sms']}  token={'yes' if o['token_acquired'] else 'no'}  "
        f"device_trusted={o['device_trusted']}"))
    return 0 if out["status"] else 1


def cmd_logout(client: PayPayClient, args) -> int:
    removed = client.clear_session()
    _emit({"cleared": removed}, args.json,
          lambda o: print("session cache cleared" if o["cleared"] else "no cached session"))
    return 0


def cmd_fees(client: PayPayClient, args) -> int:
    txns = parsers.parse_transactions(client.settlement_records(max_pages=_pages(args, 3)))
    trade_dates = [t["date"] for t in txns if t["type"] in costs.TRADE_TYPES and t["date"]]
    series = market.usdjpy_series(min(trade_dates), max(trade_dates)) if trade_dates else {}
    result = costs.compute_costs(txns, series)

    pps_pct = getattr(args, "price_spread_pct", 0.0) or 0.0
    pps = None
    if pps_pct and result["usd_notional"] and result["fx_rows"]:
        avg_mid = sum(r["market_mid"] for r in result["fx_rows"]) / len(result["fx_rows"])
        pps = costs.price_spread_estimate(result["usd_notional"], avg_mid, pps_pct)
    payload = {**result, "price_spread_pct": pps_pct, "price_spread_estimate": pps}

    def render(p):
        print("PayPay証券 — cost analysis (read-only)\n")
        print(f"  手数料/税 (explicit)    : {_yen(p['explicit_fees'])}")
        if p["fx_available"]:
            print(f"  為替スプレッド (measured) : {_yen(p['fx_spread_cost'])}"
                  f"   [{p['fx_trades']} trades, ${p['usd_notional']:,.0f}, ~{p['avg_spread_per_usd']} JPY/USD]")
        else:
            print("  為替スプレッド           : n/a (market data unavailable — offline/blocked?)")
        print(f"  {'─' * 36}")
        print(f"  測定済みコスト 合計      : {_yen(p['measured_total'])}")
        if p["price_spread_estimate"] is not None:
            print(f"\n  株価スプレッド (推定 {p['price_spread_pct']}%) : {_yen(p['price_spread_estimate'])}  (約定価格に内包・概算)")
        else:
            print("\n  注: 株価スプレッド(~0.5-0.7%, 約定価格に内包)は未測定。--price-spread-pct N で概算追加。")
        if getattr(args, "detail", False) and p["fx_rows"]:
            print("\n  FX detail:")
            print("  " + _lj("date", 12) + _lj("side", 6) + _lj("brand", 16)
                  + _rj("usd", 9) + _rj("applFX", 9) + _rj("mid", 9) + _rj("cost¥", 8))
            for r in p["fx_rows"]:
                print("  " + _lj(r["date"], 12) + _lj(r["side"], 6) + _lj(r["brand"] or "", 16)
                      + _rj(f"{r['usd']:.2f}", 9) + _rj(f"{r['applied_fx']:.2f}", 9)
                      + _rj(f"{r['market_mid']:.2f}", 9) + _rj(_yen(r["cost"]), 8))

    _emit(payload, args.json, render)
    return 0


def _gather(client: PayPayClient, pages: int = 8) -> dict:
    """Fetch everything a review needs, concurrently."""
    client.ensure_session()
    tasks = {
        "ledger": lambda: client.settlement_records(max_pages=pages),
        # 投信 ledger is a separate, churnier feed (≈14 pages); fetch generously —
        # it stops early on NEXT_FLG, so the cap is just an upper bound.
        "invledger": lambda: client.invtrust_settlement_records(max_pages=max(pages, 30)),
        "sec": lambda: parsers.parse_holdings(client.brands_html("usa")),
        "inv": lambda: parsers.parse_invtrust(client.invtrust_top()),
        "names": client.invtrust_brands,
    }
    out = {}
    with cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception:  # noqa: BLE001
                out[k] = None

    ledger = out.get("ledger") or []
    txns = parsers.parse_transactions(ledger)
    names = out.get("names") or {}
    holdings = []
    for h in (out.get("sec") or []):
        if h.is_cash:
            continue
        holdings.append({"name": h.name, "category": "証券", "valuation": h.valuation or 0,
                         "unrealized_pl": h.unrealized_pl})
    inv = out.get("inv")
    if inv:
        for h in inv.holdings:
            holdings.append({"name": names.get(str(h["brand_id"])) or f"投信#{h['brand_id']}",
                             "category": "投信", "valuation": h["valuation"] or 0,
                             "unrealized_pl": h["unrealized_pl"]})
    inv_txns = parsers.parse_invtrust_transactions(out.get("invledger") or [])
    cash, cash_fresh = _persist_cash(client, parsers.current_cash(ledger))
    total = sum(h["valuation"] for h in holdings) + (cash or 0)
    tdates = [t["date"] for t in txns if t["type"] in costs.TRADE_TYPES and t["date"]]
    series = market.usdjpy_series(min(tdates), max(tdates)) if tdates else {}
    sources = {
        "securities": "ok" if out.get("sec") is not None else "failed",
        "invtrust": "ok" if out.get("inv") is not None else "failed",
        "ledger": "ok" if out.get("ledger") else "failed",
        "invledger": "ok" if out.get("invledger") is not None else "failed",
        "cash": "live" if cash_fresh else ("stale" if cash is not None else "failed"),
        "fx": "ok" if series else "missing",
    }
    return {"txns": txns, "inv_txns": inv_txns, "holdings": holdings,
            "cash": cash or 0, "cash_fresh": cash_fresh, "total": total,
            "fx_series": series, "sources": sources}


def cmd_review(client: PayPayClient, args) -> int:
    g = _gather(client, pages=_pages(args, 8))
    agg = report.aggregate_trades(g["txns"])
    inv = report.aggregate_invtrust(g.get("inv_txns") or [])
    fx_cost = costs.compute_costs(g["txns"], g["fx_series"])["fx_spread_cost"]
    unreal = sum((h["unrealized_pl"] or 0) for h in g["holdings"])
    total, cash = g["total"], g["cash"]
    hold = sorted(g["holdings"], key=lambda x: -x["valuation"])
    for h in hold:
        h["pct"] = round(100.0 * h["valuation"] / total, 1) if total else 0

    # Realized P&L spans BOTH ledgers: 証券 (ajax_settlement) + 投信 (MARKET_ID=99).
    # Costs likewise add the 投信 譲渡益税 (tax withheld on 特定 sells) + 送金手数料.
    realized_sec, realized_inv = agg["realized_pl"], inv["realized_pl"]
    realized_total = realized_sec + realized_inv
    inv_tax, inv_transfer = inv["capital_gains_tax"], inv["transfer_fees"]
    # 投信譲渡益税 + 送金手数料 settle to the 証券 CASH ledger (特定口座 withholding /
    # account-wide fees), so they are ALREADY inside explicit_fees — adding them
    # again double-counts (verified on live data: explicit_fees == inv_tax+inv_transfer
    # for a 0-commission account). Treat them as a BREAKDOWN of the cash-ledger cost,
    # not extra cost. The only cost NOT in the ledger is the reconstructed FX spread.
    _mc = _measured_cost(agg["explicit_fees"], fx_cost, inv_tax, inv_transfer)
    total_cost, sec_fee_residual, cost_reconciles = (
        _mc["total_cost"], _mc["securities_fee_residual"], _mc["cost_reconciles"])

    # Bottom-line total return = total assets − net deposits (mark-to-market, after
    # all costs, since inception). Folding in 投信 realized shrinks the residual to
    # whatever is still unattributed (distributions, cost-basis gaps, FX timing).
    net_deposit = agg["deposits"] - agg["withdrawals"]
    total_return = total - net_deposit
    residual = total_return - (unreal + realized_total)
    # XIRR (money-weighted annualized return): dated external cashflows — 入金 is money
    # IN (negative), 出金 is money OUT (positive); both = -(ledger amount). The current
    # total value today is the closing positive cashflow. Needs the FULL deposit history
    # (use --all) to be accurate; flagged below when not fetched_all.
    _cf = [(t["date"], -(t["amount"] or 0)) for t in g["txns"]
           if t["type"] in ("入金", "出金") and t["date"]]
    _cf.append((_now_jst_str()[:10], total))
    xirr = report.xirr(_cf)
    # 評価損益率 (App頭条と同じ "含み損益 ÷ 取得原価") for the 持仓盈亏 block
    holdings_value = total - cash
    cost_basis = holdings_value - unreal
    unreal_pct = round(100.0 * unreal / cost_basis, 2) if cost_basis else 0.0
    p = {
        "as_of": _now_jst_str(), "sources": g.get("sources"),
        "period": {"from": agg["date_from"], "to": agg["date_to"]},
        "total_assets": total, "cash": cash, "cash_fresh": g.get("cash_fresh", True),
        "invested": total - cash,
        "unrealized_pl": unreal, "unrealized_pct": unreal_pct,
        "realized_sec": realized_sec, "realized_inv": realized_inv,
        "realized_total": realized_total, "inv_reconciles": inv["reconciles"],
        "sec_reconciles": agg["reconciles"], "fetched_all": getattr(args, "fetch_all", False),
        "explicit_fees": agg["explicit_fees"], "fx_spread_cost": fx_cost,
        "inv_capital_gains_tax": inv_tax, "inv_transfer_fees": inv_transfer,
        "total_cost": total_cost, "securities_fee_residual": sec_fee_residual,
        "cost_reconciles": cost_reconciles,
        "xirr": xirr,
        "deposits": agg["deposits"], "withdrawals": agg["withdrawals"],
        "net_deposit": net_deposit, "total_return": total_return,
        "ledger_residual": residual,
        "holdings": hold, "trades_by_brand": agg["brands"],
        "invtrust_trades_by_brand": inv["brands"], "invtrust_sells_yen": inv["sells_yen"],
        "note": "事実データのみ。投資助言・推奨ではありません。",
    }

    # Three numbers that answer three different questions — kept distinct so a
    # negative 評価損益 (holdings underwater) never gets read as an overall loss,
    # nor a positive 実現 as the final result (it's gross, before tax/fees).
    def table(p):
        pr = p["period"]
        cstale = "" if p.get("cash_fresh", True) else " ⚠stale(取得失敗)"
        rmark = "" if p["inv_reconciles"] else " ⚠過小評価"
        print(f"PayPay証券 復盘  (取引履歴 {pr['from']} 〜 {pr['to']})\n")

        print(f"① 持仓盈亏  評価損益(=App頭条, 今の保有)  : {_signed_yen(p['unrealized_pl'])} ({p['unrealized_pct']:+.2f}%)")
        for h in p["holdings"]:
            print("     " + _lj(h["name"], 30) + _rj(_yen(h["valuation"]), 11)
                  + _rj(_signed_yen(h["unrealized_pl"]), 10))

        print(f"\n② 累計実現  実現損益(売って確定, 税引前)  : {_signed_yen(p['realized_total'])}{rmark}")
        print(f"     証券 {_signed_yen(p['realized_sec'])} / 投信 {_signed_yen(p['realized_inv'])}"
              f"   ※移動平均推計; App「実現損益合計」が正(米株特定はFX差)")
        bh = _basis_hint(p.get("sec_reconciles", True) and p["inv_reconciles"], p.get("fetched_all", False))
        if bh:
            print("     " + bh)

        print(f"\n③ 整体盈亏  通算 = 総資産 − 純入金 ★最終  : {_signed_yen(p['total_return'])}")
        print(f"     総資産 {_yen(p['total_assets'])}(現金 {_yen(p['cash'])}{cstale}) − 純入金 {_yen(p['net_deposit'])}")
        print(f"     = 実現益から 譲渡益税 {_yen(p['inv_capital_gains_tax'])}・送金手数料 {_yen(p['inv_transfer_fees'])}・為替等を差引いた後の値")
        if p.get("xirr") is not None:
            an = "" if p.get("fetched_all") else "  ※--allで全入金取得すると正確"
            print(f"     資金加重収益率 (XIRR 年率): {p['xirr'] * 100:+.1f}%{an}")

        cmark = "" if p.get("cost_reconciles", True) else "  ⚠現金ledger不足(--allで再取得)"
        print(f"\n  測定コスト 合計 {_yen(p['total_cost'])}{cmark}")
        print(f"     現金側手数料/税 {_yen(p['explicit_fees'])}"
              f"  (うち 投信譲渡益税 {_yen(p['inv_capital_gains_tax'])} / 送金手数料 {_yen(p['inv_transfer_fees'])}"
              f" / 証券手数料 {_yen(p['securities_fee_residual'])})")
        print(f"     推定為替コスト  {_yen(p['fx_spread_cost'])}  (約定価格・為替レートに内包)")
        fl = []
        _freshness_block(p, fl)
        if fl:
            print()
            for line in fl:
                print(line)
        print(f"\n  注: {p['note']}")

    def lark(p):
        pr = p["period"]
        cstale = "" if p.get("cash_fresh", True) else " ⚠stale"
        rmark = "" if p["inv_reconciles"] else " ⚠過小評価"
        L = [f"**PayPay証券 復盘 (取引履歴 {pr['from']}〜{pr['to']})**", "",
             f"**① 持仓盈亏** 評価損益(=App頭条, 今の保有): **{_signed_yen(p['unrealized_pl'])}** ({p['unrealized_pct']:+.2f}%)"]
        for h in p["holdings"]:
            L.append(f"  - {h['name']}: {_yen(h['valuation'])} {_signed_yen(h['unrealized_pl'])}")
        bh = _basis_hint(p.get("sec_reconciles", True) and p["inv_reconciles"], p.get("fetched_all", False))
        L += [f"**② 累計実現** 実現損益(売って確定, 税引前): **{_signed_yen(p['realized_total'])}**{rmark}",
              f"  - 証券 {_signed_yen(p['realized_sec'])} / 投信 {_signed_yen(p['realized_inv'])} ※移動平均推計; App「実現損益合計」が正"]
        if bh:
            L.append(f"  - {bh}")
        L += [
              f"**③ 整体盈亏** 通算 = 総資産 − 純入金 ★最終: **{_signed_yen(p['total_return'])}**",
              f"  - 総資産 {_yen(p['total_assets'])}(現金 {_yen(p['cash'])}{cstale}) − 純入金 {_yen(p['net_deposit'])}",
              f"  - 実現益から 譲渡益税 {_yen(p['inv_capital_gains_tax'])}・送金手数料 {_yen(p['inv_transfer_fees'])}・為替等を差引いた後",
              *([f"  - 資金加重収益率 (XIRR 年率): **{p['xirr'] * 100:+.1f}%**"
                 + ("" if p.get("fetched_all") else " (※--allで正確)")] if p.get("xirr") is not None else []),
              f"- 測定コスト合計 **{_yen(p['total_cost'])}**: 現金側手数料/税 {_yen(p['explicit_fees'])}"
              f"(うち 投信譲渡益税 {_yen(p['inv_capital_gains_tax'])}/送金手数料 {_yen(p['inv_transfer_fees'])}"
              f"/証券手数料 {_yen(p['securities_fee_residual'])}) + 推定為替 {_yen(p['fx_spread_cost'])}"]
        _freshness_block(p, L)
        L.append(f"\n> {p['note']}")
        print("\n".join(L))

    _emit_fmt(p, _fmt(args), table, lark)
    return 0


def cmd_trades_summary(client: PayPayClient, args) -> int:
    txns = parsers.parse_transactions(client.settlement_records(max_pages=_pages(args, 8)))
    agg = report.aggregate_trades(txns)
    p = {"period": {"from": agg["date_from"], "to": agg["date_to"]},
         "deposits": agg["deposits"], "withdrawals": agg["withdrawals"],
         "explicit_fees": agg["explicit_fees"], "realized_pl": agg["realized_pl"],
         "reconciles": agg["reconciles"], "fetched_all": getattr(args, "fetch_all", False),
         "brands": agg["brands"]}

    def table(p):
        print(f"取引集計 ({p['period']['from']} 〜 {p['period']['to']})\n")
        print(_lj("BRAND", 26) + _rj("BUY", 11) + _rj("SELL", 11) + _rj("NET投入", 11)
              + _rj("NET株", 13) + _rj("実現損益", 10))
        print("-" * 82)
        for b in p["brands"]:
            print(_lj(b["name"], 26) + _rj(_yen(b["buy_yen"]), 11) + _rj(_yen(b["sell_yen"]), 11)
                  + _rj(_yen(b["net_invested"]), 11) + _rj(f"{b['net_shares']:.4f}", 13)
                  + _rj(_signed_yen(b["realized_pl"]), 10))
        print("-" * 82)
        print(f"  累計入金 {_yen(p['deposits'])} / 出金 {_yen(p['withdrawals'])} / "
              f"手数料 {_yen(p['explicit_fees'])} / 実現損益合計 {_signed_yen(p['realized_pl'])}")
        bh = _basis_hint(p.get("reconciles", True), p.get("fetched_all", False))
        if bh:
            print("  " + bh)

    def lark(p):
        L = [f"**取引集計 ({p['period']['from']}〜{p['period']['to']})**", ""]
        for b in p["brands"]:
            L.append(f"- **{b['name']}**: 買 {_yen(b['buy_yen'])} / 売 {_yen(b['sell_yen'])} / "
                     f"純投入 {_yen(b['net_invested'])} / 残 {b['net_shares']:.4f}株 / "
                     f"実現 {_signed_yen(b['realized_pl'])}")
        L.append(f"- 累計入金 **{_yen(p['deposits'])}** / 手数料 {_yen(p['explicit_fees'])} / "
                 f"実現損益合計 **{_signed_yen(p['realized_pl'])}**")
        bh = _basis_hint(p.get("reconciles", True), p.get("fetched_all", False))
        if bh:
            L.append(f"- {bh}")
        print("\n".join(L))

    _emit_fmt(p, _fmt(args), table, lark)
    return 0


def cmd_accounts(client, args) -> int:
    """List configured account profiles (needs no credentials)."""
    accts = config.list_accounts()
    active = getattr(args, "account", None) or os.environ.get("PAYPAY_ACCOUNT") or config.DEFAULT_ACCOUNT
    payload = {"accounts": accts, "active": active}

    def render(p):
        if not p["accounts"]:
            print("no accounts configured.")
            print("  default : add credentials to ~/.paypay-sec/.env")
            print("  named   : add ~/.paypay-sec/<name>.env, then use -a <name>")
            return
        print("configured accounts (* = active for this invocation):")
        for a in p["accounts"]:
            print(f"  {'*' if a == p['active'] else ' '} {a}")
        print("\nuse:  paypay -a <name> <command>   (or set PAYPAY_ACCOUNT)")

    _emit(payload, args.json, render)
    return 0


def cmd_doctor(client, args) -> int:
    """Diagnose local setup + login readiness. Needs no network — it inspects the
    account's .env, cookie, cached session, and cache dir, and says whether the
    next login would need an SMS code."""
    from .client import state_dir as _state_dir
    account = (getattr(args, "account", None) or os.environ.get("PAYPAY_ACCOUNT")
               or config.DEFAULT_ACCOUNT)
    env_path = config.env_file_for(account)
    config.load_dotenv(account)

    member_id = os.environ.get("PAYPAY_MEMBER_ID", "").strip()
    password_set = bool(os.environ.get("PAYPAY_PASSWORD", "").strip())
    cookie = os.environ.get("PAYPAY_COOKIE", "").strip()
    has_device_token = "SMS_AUTH_STRING" in cookie

    sd = _state_dir(account)
    session_file, cache_dir = sd / "session.json", sd / "cache"
    session_exists = session_file.exists()
    token_cached, last_session = False, None
    if session_exists:
        try:
            token_cached = bool(json.loads(session_file.read_text(encoding="utf-8")).get("token"))
        except (OSError, ValueError):
            pass
        try:
            mt = session_file.stat().st_mtime
            last_session = (datetime.fromtimestamp(mt, tz=timezone.utc)
                            .astimezone(_JST).strftime("%Y-%m-%d %H:%M JST"))
        except OSError:
            pass
    cache_count = len(list(cache_dir.glob("*.json"))) if cache_dir.exists() else 0

    # gating checks (define "ready to run unattended")
    checks = [
        {"ok": env_path is not None, "name": "credential file",
         "detail": str(env_path) if env_path else
                   f"missing — create {'~/.paypay-sec/.env' if account==config.DEFAULT_ACCOUNT else f'~/.paypay-sec/{account}.env'}"},
        {"ok": bool(member_id), "name": "PAYPAY_MEMBER_ID", "detail": "set" if member_id else "missing"},
        {"ok": password_set, "name": "PAYPAY_PASSWORD", "detail": "set" if password_set else "missing"},
        {"ok": bool(cookie), "name": "PAYPAY_COOKIE", "detail": "set" if cookie else "missing"},
        {"ok": has_device_token, "name": "trusted-device token",
         "detail": "cookie carries _SMS_AUTH_STRING (SMS skipped)" if has_device_token
                   else "cookie LACKS _SMS_AUTH_STRING → login will demand an SMS code"},
    ]
    # --online: actually hit the API to confirm the cached session is LIVE (not just
    # that the local files look right). Off by default — it touches the network.
    online = getattr(args, "online", False)
    online_checks = []
    if online:
        if not (member_id and password_set):
            online_checks.append({"ok": False, "name": "online probe",
                                  "detail": "credentials missing — can't probe"})
        else:
            from .client import SessionExpired
            try:
                c = PayPayClient(Settings.from_env(account))
                info = c.login()
                online_checks.append({
                    "ok": bool(info.get("STATUS")) and not info.get("IF_NEED_SMS_FLG"),
                    "name": "login (web)",
                    "detail": f"STATUS={bool(info.get('STATUS'))} sms_required={bool(info.get('IF_NEED_SMS_FLG'))}"})
                for label, probe in (("証券 page", lambda: c.portfolio_html("usa")),
                                     ("投信 API", c.invtrust_top)):
                    try:
                        probe()
                        online_checks.append({"ok": True, "name": label, "detail": "reachable / session valid"})
                    except SessionExpired:
                        online_checks.append({"ok": False, "name": label, "detail": "redirected to login (session expired)"})
                    except Exception as e:  # noqa: BLE001
                        online_checks.append({"ok": False, "name": label, "detail": type(e).__name__})
            except Exception as e:  # noqa: BLE001
                online_checks.append({"ok": False, "name": "login (web)", "detail": f"{type(e).__name__}: {e}"})

    offline_ready = all(c["ok"] for c in checks)
    online_ready = (not online) or all(c["ok"] for c in online_checks)
    payload = {
        "account": account, "env_file": str(env_path) if env_path else None,
        "member_id_set": bool(member_id), "password_set": password_set,
        "cookie_set": bool(cookie), "has_device_token": has_device_token,
        "sms_would_be_required": not has_device_token,
        "session_cached": session_exists, "session_token_cached": token_cached,
        "last_session_refresh": last_session,
        "cache_dir": str(cache_dir), "cache_files": cache_count,
        "accounts": config.list_accounts(),
        "checks": checks,
        "online": online, "online_checks": online_checks,
        "ready": offline_ready and online_ready,
    }

    def render(p):
        print(f"paypay doctor — account '{p['account']}'\n")
        for c in p["checks"]:
            print(f"  [{'OK' if c['ok'] else '!!'}] {_lj(c['name'], 22)} {c['detail']}")
        if p["online"]:
            print("\n  online probe:")
            for c in p["online_checks"]:
                print(f"  [{'OK' if c['ok'] else '!!'}] {_lj(c['name'], 22)} {c['detail']}")
        sess = (f"yes (token={'yes' if p['session_token_cached'] else 'no'}, "
                f"last {p['last_session_refresh'] or '?'})" if p["session_cached"]
                else "none yet — the first command will log in")
        print(f"\n  cached session             : {sess}")
        print(f"  SMS required on next login : "
              f"{'YES — cookie missing trusted-device token' if p['sms_would_be_required'] else 'no'}")
        print(f"  response cache             : {p['cache_files']} files in {p['cache_dir']}")
        print(f"  configured accounts        : {', '.join(p['accounts']) or '(none)'}")
        if not p["online"]:
            print("  (run `paypay doctor --online` to verify the session is actually live)")
        print(f"\n  → {'READY' if p['ready'] else 'NOT READY — resolve the !! items above'}")

    _emit(payload, args.json, render)
    return 0 if payload["ready"] else 1


def _build_snapshot(client: PayPayClient, args) -> dict:
    """Compute the account's headline numbers for a snapshot (review-level)."""
    g = _gather(client, pages=_pages(args, 8))
    agg = report.aggregate_trades(g["txns"])
    inv = report.aggregate_invtrust(g.get("inv_txns") or [])
    unreal = sum((h["unrealized_pl"] or 0) for h in g["holdings"])
    total, cash = g["total"], g["cash"]
    holdings = [{"name": h["name"], "category": h["category"],
                 "valuation": h["valuation"], "unrealized_pl": h["unrealized_pl"]}
                for h in sorted(g["holdings"], key=lambda x: -x["valuation"])]
    return {
        "ts": snapshots.now_ts(), "as_of": _now_jst_str(),
        "grand_total": total, "cash": cash, "invested": total - cash,
        "unrealized_pl": unreal,
        "realized_total": agg["realized_pl"] + inv["realized_pl"],
        "net_deposit": agg["deposits"] - agg["withdrawals"],
        "deposits": agg["deposits"], "withdrawals": agg["withdrawals"],
        "holdings": holdings, "sources": g.get("sources"),
    }


def cmd_snapshot(client: PayPayClient, args) -> int:
    """Save / list account snapshots — the CLI's own long-term time series."""
    account = getattr(args, "account", None)
    if getattr(args, "snap_cmd", "save") == "list":
        paths = snapshots.list_paths(account)
        rows = []
        for p in paths:
            s = snapshots.load(p)
            rows.append({k: s.get(k) for k in ("ts", "as_of", "grand_total", "cash", "realized_total")})
        payload = {"snapshots": rows}

        def render(pl):
            if not pl["snapshots"]:
                print("no snapshots yet — run `paypay snapshot save`")
                return
            print("saved snapshots:\n")
            print("  " + _lj("ID", 18) + _lj("AS_OF", 22) + _rj("総資産", 12) + _rj("実現", 11))
            for s in pl["snapshots"]:
                print("  " + _lj(s["ts"] or "", 18) + _lj(s.get("as_of") or "", 22)
                      + _rj(_yen(s.get("grand_total")), 12) + _rj(_signed_yen(s.get("realized_total")), 11))

        _emit(payload, args.json, render)
        return 0

    snap = _build_snapshot(client, args)
    path = snapshots.save(account, snap)
    payload = {"saved": str(path), **snap}

    def render(p):
        print(f"snapshot saved: {p['saved']}")
        print(f"  総資産 {_yen(p['grand_total'])}  現金 {_yen(p['cash'])}  "
              f"実現 {_signed_yen(p['realized_total'])}  純入金 {_yen(p['net_deposit'])}")

    _emit(payload, args.json, render)
    return 0


def _diff_payload(base: dict, cur: dict) -> dict:
    """Pure diff of two snapshot dicts. Separated for unit testing."""
    bmap = {h["name"]: h for h in base.get("holdings", [])}
    cmap = {h["name"]: h for h in cur.get("holdings", [])}
    hold_changes = []
    for name in sorted(set(bmap) | set(cmap)):
        bv = (bmap.get(name) or {}).get("valuation") or 0
        cv = (cmap.get(name) or {}).get("valuation") or 0
        if bv != cv:
            hold_changes.append({"name": name, "from": bv, "to": cv, "delta": cv - bv})
    keys = ("grand_total", "invested", "cash", "net_deposit", "unrealized_pl", "realized_total")
    return {
        "from": {"ts": base.get("ts"), "as_of": base.get("as_of")},
        "to": {"ts": cur.get("ts"), "as_of": cur.get("as_of")},
        "delta": {k: (cur.get(k) or 0) - (base.get(k) or 0) for k in keys},
        "holdings_changed": hold_changes,
        "sources": cur.get("sources"),
        "note": "事実の差分のみ。投資助言ではありません。",
    }


def cmd_diff(client: PayPayClient, args) -> int:
    """Diff a live read against a saved snapshot baseline (--days N, else latest)."""
    account = getattr(args, "account", None)
    days = getattr(args, "days", None)
    base = snapshots.nearest_before(account, days) if days else snapshots.latest(account)
    if not base:
        print("no baseline snapshot — run `paypay snapshot save` first", file=sys.stderr)
        return 1
    cur = _build_snapshot(client, args)
    payload = _diff_payload(base, cur)

    def table(p):
        fr, to, d = p["from"], p["to"], p["delta"]
        print(f"PayPay証券 — 差分  {fr['as_of'] or fr['ts']}  →  {to['as_of'] or to['ts']}\n")
        print(f"  総資産     : {_signed_yen(d['grand_total'])}")
        print(f"  投資資産   : {_signed_yen(d['invested'])}")
        print(f"  現金       : {_signed_yen(d['cash'])}")
        print(f"  純入金     : {_signed_yen(d['net_deposit'])}  (この間の入出金)")
        print(f"  評価損益   : {_signed_yen(d['unrealized_pl'])}")
        print(f"  実現損益   : {_signed_yen(d['realized_total'])}  (累計の増分 = この間の確定)")
        if p["holdings_changed"]:
            print("\n  持仓变化:")
            for h in p["holdings_changed"]:
                print("    " + _lj(h["name"], 28) + _rj(_yen(h["from"]), 12) + " → "
                      + _rj(_yen(h["to"]), 12) + "  " + _signed_yen(h["delta"]))
        fl = []
        _freshness_block({"sources": p.get("sources")}, fl)
        if fl:
            print()
            for line in fl:
                print(line)
        print(f"\n  注: {p['note']}")

    def lark(p):
        fr, to, d = p["from"], p["to"], p["delta"]
        L = [f"**PayPay証券 差分 {fr['as_of'] or fr['ts']} → {to['as_of'] or to['ts']}**", "",
             f"- 総資産: **{_signed_yen(d['grand_total'])}**",
             f"- 投資資産: {_signed_yen(d['invested'])} / 現金: {_signed_yen(d['cash'])}",
             f"- 純入金(この間): {_signed_yen(d['net_deposit'])}",
             f"- 評価損益: {_signed_yen(d['unrealized_pl'])} / 実現損益(増分): **{_signed_yen(d['realized_total'])}**"]
        if p["holdings_changed"]:
            L.append("- 持仓变化:")
            for h in p["holdings_changed"]:
                L.append(f"  - {h['name']}: {_yen(h['from'])} → {_yen(h['to'])} ({_signed_yen(h['delta'])})")
        L.append(f"\n> {p['note']}")
        print("\n".join(L))

    _emit_fmt(payload, _fmt(args), table, lark)
    return 0


def cmd_cache_clear(client: PayPayClient, args) -> int:
    n = client.clear_cache()
    _emit({"removed": n}, args.json, lambda o: print(f"cleared {o['removed']} cached responses"))
    return 0


def cmd_balance(client: PayPayClient, args) -> int:
    summary = parsers.parse_summary(client.portfolio_html(args.market))
    _emit(summary.to_dict(), args.json, lambda s: print(
        f"member        : {s['member_id']}\n"
        f"valuation     : {_yen(s['total_valuation'])}\n"
        f"principal     : {_yen(s['principal'])}\n"
        f"unrealized P&L: {_yen(s['unrealized_pl'])}"))
    return 0


def cmd_portfolio(client: PayPayClient, args) -> int:
    summary = parsers.parse_summary(client.portfolio_html(args.market))
    holdings = parsers.parse_holdings(client.brands_html(args.market))
    payload = {"summary": summary.to_dict(), "holdings": [h.to_dict() for h in holdings]}

    def render(p):
        s = p["summary"]
        print(f"{s['member_id']}  valuation {_yen(s['total_valuation'])}  "
              f"principal {_yen(s['principal'])}  P&L {_yen(s['unrealized_pl'])}\n")
        print(_lj("NAME", 18) + _rj("VALUATION", 12) + _rj("WEIGHT", 8)
              + _rj("SHARES", 14) + _rj("PRINCIPAL", 12) + _rj("P&L", 10) + "  ACCT")
        print("-" * 88)
        for h in p["holdings"]:
            wt = f"{h['weight_pct']:.1f}%" if h["weight_pct"] is not None else "—"
            sh = f"{h['shares']:.6f}" if h["shares"] is not None else "—"
            print(_lj(h["name"] or "", 18) + _rj(_yen(h["valuation"]), 12) + _rj(wt, 8)
                  + _rj(sh, 14) + _rj(_yen(h["principal"]), 12) + _rj(_yen(h["unrealized_pl"]), 10)
                  + "  " + (",".join(h["account_types"]) or "—"))

    _emit(payload, args.json, render)
    return 0


def cmd_invtrust(client: PayPayClient, args) -> int:
    inv = parsers.parse_invtrust(client.invtrust_top())
    try:
        names = client.invtrust_brands()
    except (requests.RequestException, ValueError):
        names = {}
    d = inv.to_dict()
    for h in d["holdings"]:
        h["name"] = names.get(str(h["brand_id"]))
    # 定投/つみたて: funds with an active recurring-buy plan (even if not yet held)
    d["reserve_plans"] = [{"brand_id": b, "name": names.get(str(b)) or f"brand#{b}"}
                          for b in d.get("reserve_brand_ids", [])]

    def render(d):
        print("投信 (mutual funds)")
        print(f"  評価額       : {_yen(d['valuation'])}")
        print(f"  投資元本     : {_yen(d['principal'])}")
        print(f"  含み損益     : {_yen(d['unrealized_pl'])}")
        print(f"  売却申込中   : {_yen(d['sell_order_pending'])}")
        print(f"  買付可能現金 : {_yen(d['buyable_cash'])}")
        if d["holdings"]:
            print("  保有:")
            for h in d["holdings"]:
                label = h.get("name") or f"brand#{h['brand_id']}"
                tag = "  📅定投" if h.get("reserve_plan") else ""
                print("    " + _lj(label, 34) + _rj(_yen(h["valuation"]), 12)
                      + "  含み損益 " + _yen(h["unrealized_pl"]) + tag)
        if d.get("reserve_plans"):
            print("\n  定投/つみたて 設定中の銘柄:")
            for r in d["reserve_plans"]:
                print(f"    📅 {r['name']}")
            print("    注: 設定金額・頻度は web API 非公開。実際の積立額は invtrust-history で確認。")

    _emit(d, args.json, render)
    return 0


def _plans_payload(client: PayPayClient, args) -> dict:
    """定投/つみたて plans for one account: active-plan flag (pc_invest_top) +
    run-rate inferred from executed つみたて buys (invtrust-history)."""
    inv = parsers.parse_invtrust(client.invtrust_top())
    try:
        names = client.invtrust_brands()
    except (requests.RequestException, ValueError):
        names = {}
    txns = parsers.parse_invtrust_transactions(client.invtrust_settlement_records(max_pages=_pages(args, 30)))
    rr_by = {r["brand"]: r for r in report.tsumitate_runrate(txns)}
    active = {names.get(str(b)) or f"brand#{b}" for b in inv.reserve_brand_ids}
    plans = []
    for nm in sorted(active | set(rr_by)):
        rr = rr_by.get(nm)
        plans.append({"name": nm, "active": nm in active,
                      "monthly_estimate": rr["monthly_estimate"] if rr else None,
                      "annualized": rr["annualized"] if rr else None,
                      "months": rr["months"] if rr else 0,
                      "total_invested": rr["total_invested"] if rr else None,
                      "last_date": rr["last_date"] if rr else None})
    monthly = sum(p["monthly_estimate"] or 0 for p in plans)
    return {"plans": plans, "monthly_total_estimate": monthly,
            "annualized_total_estimate": monthly * 12}


def cmd_plans(client: PayPayClient, args) -> int:
    """定投/つみたて plans — active recurring-buy funds + inferred monthly run-rate
    (from real executions; the configured amount/cycle isn't in the web API).
    FACTS only — no advice."""
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_plans, _plans_payload, merge=_merge_plans)
    payload = {"as_of": _now_jst_str(), **_plans_payload(client, args),
               "note": "定投額は実際のNISAつみたて買付からの推計(設定値はAPI非公開)。事実のみ、助言ではありません。"}
    _emit_fmt(payload, _fmt(args), lambda p: _render_plans(p, "table"),
              lambda p: _render_plans(p, "lark"))
    return 0


def _render_plans(p: dict, fmt: str) -> None:
    head = f"PayPay証券 — 定投/つみたて 計画 (推計, 事実のみ)"
    if fmt == "lark":
        L = [f"**{head}**", "",
             f"- 月定投 合計(推計): **{_yen(p['monthly_total_estimate'])}** / 年化 {_yen(p['annualized_total_estimate'])}"]
        for pl in p["plans"]:
            tag = "📅" if pl["active"] else "·"
            me = _yen(pl["monthly_estimate"]) if pl["monthly_estimate"] is not None else "—"
            L.append(f"  - {tag} {pl['name']}: 月額(推) {me}"
                     + (f" / 累計 {_yen(pl['total_invested'])} / {pl['months']}ヶ月" if pl["months"] else "")
                     + ("" if pl["active"] else " (設定なし/停止?)"))
        if p.get("accounts"):
            L.append("- 口座別: " + " / ".join(f"{a}:{_yen(v)}/月" for a, v in p["accounts"].items()))
        L.append(f"\n> {p.get('note','')}")
        print("\n".join(L))
        return
    print(f"{head}\n")
    print(f"  月定投 合計(推計): {_yen(p['monthly_total_estimate'])}   (年化 {_yen(p['annualized_total_estimate'])})")
    if p.get("accounts"):
        print("  口座別/月: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
    print("\n  " + _lj("銘柄", 32) + _lj("定投", 6) + _rj("月額(推)", 11)
          + _rj("年化", 12) + _rj("月数", 6) + _rj("累計投入", 12))
    for pl in p["plans"]:
        me = _yen(pl["monthly_estimate"]) if pl["monthly_estimate"] is not None else "—"
        print("  " + _lj(pl["name"] or "", 32) + _lj("📅" if pl["active"] else "—", 6)
              + _rj(me, 11) + _rj(_yen(pl["annualized"]) if pl["annualized"] else "—", 12)
              + _rj(str(pl["months"]), 6) + _rj(_yen(pl["total_invested"]), 12))
    print(f"\n  注: {p.get('note','')}")


def cmd_tax(client: PayPayClient, args) -> int:
    """Per calendar-year tax view: 売却 proceeds, 譲渡益税 withheld, 分配金 — the
    figures the 年間取引報告書 shows. FACTS only (NISA is tax-free; 特定 is withheld)."""
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_tax, _tax_payload, merge=_merge_tax)
    payload = {"as_of": _now_jst_str(), **_tax_payload(client, args),
               "note": "事実のみ。年間取引報告書の参考値。NISA口座は非課税、特定口座は源泉徴収。"}
    _emit_fmt(payload, _fmt(args), lambda p: _render_tax(p, "table"),
              lambda p: _render_tax(p, "lark"))
    return 0


def _tax_payload(client: PayPayClient, args) -> dict:
    sec = parsers.parse_transactions(client.settlement_records(max_pages=_pages(args, 8)))
    inv = parsers.parse_invtrust_transactions(client.invtrust_settlement_records(max_pages=_pages(args, 30)))
    return {"tax_years": report.tax_summary(sec, inv)}


def _render_tax(p: dict, fmt: str) -> None:
    head = "PayPay証券 — 税務サマリー (年別, 事実のみ)"
    rows = p["tax_years"]
    if fmt == "lark":
        L = [f"**{head}**", ""]
        for r in rows:
            L.append(f"- **{r['year']}**: 売却 証券{_yen(r['sec_sell'])}/投信{_yen(r['inv_sell'])} "
                     f"・譲渡益税 {_yen(r['capital_gains_tax'])}・分配金 {_yen(r['distributions'])}")
        L.append(f"\n> {p.get('note','')}")
        print("\n".join(L))
        return
    print(f"{head}\n")
    print("  " + _lj("年", 8) + _rj("証券売却", 12) + _rj("投信売却", 12)
          + _rj("譲渡益税", 11) + _rj("分配金", 10))
    for r in rows:
        print("  " + _lj(r["year"], 8) + _rj(_yen(r["sec_sell"]), 12) + _rj(_yen(r["inv_sell"]), 12)
              + _rj(_yen(r["capital_gains_tax"]), 11) + _rj(_yen(r["distributions"]), 10))
    if not rows:
        print("  (売却/課税の記録なし)")
    print(f"\n  注: {p.get('note','')}")


def _account_clients(args):
    """Yield (account_name, client) for every configured profile (for -a all)."""
    for a in config.list_accounts():
        try:
            yield a, PayPayClient(Settings.from_env(a),
                                  cache_ttl=0 if getattr(args, "no_cache", False) else None)
        except Exception:  # noqa: BLE001 — skip a profile that won't load
            yield a, None


def _all_accounts(args, render, payload_fn, merge) -> int:
    """Run payload_fn per account, merge, and emit. Shared by -a all commands."""
    per = {}
    for name, client in _account_clients(args):
        if client is None:
            continue
        try:
            per[name] = payload_fn(client, args)
        except Exception as e:  # noqa: BLE001
            per[name] = {"_error": type(e).__name__}
    merged = merge(per)
    merged["as_of"] = _now_jst_str()
    _emit_fmt(merged, _fmt(args), lambda p: render(p, "table"), lambda p: render(p, "lark"))
    return 0


def _merge_plans(per: dict) -> dict:
    by_name = {}
    acct_monthly = {}
    for acct, pl in per.items():
        if pl.get("_error"):
            continue
        acct_monthly[acct] = pl.get("monthly_total_estimate", 0)
        for r in pl.get("plans", []):
            cur = by_name.setdefault(r["name"], {"name": r["name"], "active": False,
                                                 "monthly_estimate": 0, "annualized": 0,
                                                 "months": 0, "total_invested": 0, "last_date": None})
            cur["active"] = cur["active"] or r["active"]
            cur["monthly_estimate"] += r.get("monthly_estimate") or 0
            cur["annualized"] += r.get("annualized") or 0
            cur["months"] = max(cur["months"], r.get("months") or 0)
            cur["total_invested"] += r.get("total_invested") or 0
    plans = sorted(by_name.values(), key=lambda x: -(x["total_invested"] or 0))
    monthly = sum(p["monthly_estimate"] or 0 for p in plans)
    return {"plans": plans, "monthly_total_estimate": monthly,
            "annualized_total_estimate": monthly * 12, "accounts": acct_monthly,
            "note": "全口座合算。定投額は実際のNISAつみたて買付からの推計。事実のみ。"}


def _merge_tax(per: dict) -> dict:
    years = {}
    for pl in per.values():
        for r in pl.get("tax_years", []) if not pl.get("_error") else []:
            y = years.setdefault(r["year"], {"year": r["year"], "sec_sell": 0, "inv_sell": 0,
                                             "capital_gains_tax": 0, "distributions": 0})
            for k in ("sec_sell", "inv_sell", "capital_gains_tax", "distributions"):
                y[k] += r.get(k, 0)
    return {"tax_years": [years[y] for y in sorted(years)],
            "note": "全口座合算。年間取引報告書の参考値。事実のみ。"}


def _total_payload(client: PayPayClient, args) -> dict:
    sec_by_market, errors = {}, []
    for mkt in ("usa", "japan"):
        try:
            s = parsers.parse_summary(client.portfolio_html(mkt))
            sec_by_market[mkt] = s.total_valuation or 0
        except (requests.HTTPError, requests.RequestException) as e:
            sec_by_market[mkt] = None
            errors.append(f"{mkt}: {type(e).__name__}")
    sec_total = sum(v for v in sec_by_market.values() if v)
    inv = parsers.parse_invtrust(client.invtrust_top())
    invested = sec_total + (inv.valuation or 0)
    cash, cash_fresh = _persist_cash(client, parsers.current_cash(client.settlement_records(max_pages=1)))
    if not cash_fresh:
        errors.append("cash: throttled→stale")
    return {
        "securities_by_market": sec_by_market, "securities_total": sec_total,
        "invtrust_valuation": inv.valuation, "invested_total": invested,
        "cash": cash, "cash_fresh": cash_fresh, "grand_total": invested + (cash or 0),
        "invtrust_sell_pending": inv.sell_order_pending, "errors": errors,
        "sources": {
            "securities": "ok" if any(v for v in sec_by_market.values()) else "failed",
            "invtrust": "ok" if inv.valuation is not None else "failed",
            "cash": "live" if cash_fresh else ("stale" if cash is not None else "failed")},
        "note": ("grand_total = 証券 + 投信 holdings + 証券 cash balance, matching the "
                 "app's 保有資産 total. CFD (separate login) is not included."),
    }


def _render_total(p: dict, fmt: str) -> None:
    if fmt == "lark":
        cstale = "" if p.get("cash_fresh", True) else " ⚠stale"
        L = ["**PayPay証券 総資産" + ("(全口座)" if p.get("accounts") else "") + "**", "",
             f"- 証券(株+ETF): {_yen(p['securities_total'])}",
             f"- 投信(基金): {_yen(p['invtrust_valuation'])}",
             f"- 現金: {_yen(p['cash'])}{cstale}",
             f"- **総資産 合計: {_yen(p['grand_total'])}**",
             f"  (投資資産 {_yen(p['invested_total'])} / 投信 売却申込中 {_yen(p['invtrust_sell_pending'])})"]
        if p.get("accounts"):
            L.append("- 口座別: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
        if p["errors"]:
            L.append(f"- ⚠ {', '.join(p['errors'])}")
        _freshness_block(p, L)
        L.append("\n> CFD(別ログイン)は未集計")
        print("\n".join(L))
        return
    cstale = "" if p.get("cash_fresh", True) else "  ⚠stale(取得失敗)"
    print("PayPay証券 — total assets" + ("(全口座)" if p.get("accounts") else "") + " (read-only)\n")
    print(f"  証券 (株+ETF)   : {_yen(p['securities_total'])}")
    print(f"  投信 (基金)      : {_yen(p['invtrust_valuation'])}")
    print(f"  現金            : {_yen(p['cash'])}{cstale}")
    print(f"  {'─' * 30}")
    print(f"  総資産 合計      : {_yen(p['grand_total'])}")
    print(f"\n  (うち投資資産 {_yen(p['invested_total'])} / 投信 売却申込中 {_yen(p['invtrust_sell_pending'])} settling)")
    if p.get("accounts"):
        print("  口座別: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
    if p["errors"]:
        print(f"\n  ⚠ 取得に失敗(集計から除外): {', '.join(p['errors'])}")
    print("\n  注: CFD は別ログインのため未集計。")
    fl = []
    _freshness_block(p, fl)
    if fl:
        print()
        for line in fl:
            print(line)


def _merge_total(per: dict) -> dict:
    keys = ("securities_total", "invtrust_valuation", "invested_total", "cash",
            "grand_total", "invtrust_sell_pending")
    out = {k: 0 for k in keys}
    accounts, errors = {}, []
    for acct, p in per.items():
        if p.get("_error"):
            errors.append(f"{acct}: {p['_error']}"); continue
        for k in keys:
            out[k] += p.get(k) or 0
        accounts[acct] = p.get("grand_total") or 0
    out.update({"accounts": accounts, "errors": errors, "cash_fresh": True,
                "sources": {}, "note": "全口座合算。CFD は別ログイン未集計。"})
    return out


def cmd_total(client: PayPayClient, args) -> int:
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_total, _total_payload, merge=_merge_total)
    payload = {"as_of": _now_jst_str(), **_total_payload(client, args)}
    _emit_fmt(payload, _fmt(args), lambda p: _render_total(p, "table"),
              lambda p: _render_total(p, "lark"))
    return 0


def _consolidated_holdings(client: PayPayClient):
    """Concurrent fetch of 証券(usa) + 投信 holdings + securities cash. Degrades
    per source (a failed feed → that source marked 'failed', not a crash).
    Returns (rows, cash, cash_fresh, invtrust_sell_pending, sources)."""
    client.ensure_session()
    tasks = {
        "sec_usa": lambda: parsers.parse_holdings(client.brands_html("usa")),
        "inv_top": client.invtrust_top,
        "cash": lambda: parsers.current_cash(client.settlement_records(max_pages=1)),
        "names": client.invtrust_brands,
    }
    out = {}
    with cf.ThreadPoolExecutor(max_workers=len(tasks)) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, f in futs.items():
            try:
                out[k] = f.result()
            except Exception:  # noqa: BLE001 — degrade gracefully per source
                out[k] = None

    inv = parsers.parse_invtrust(out["inv_top"]) if out.get("inv_top") else None
    names = out.get("names") or {}
    cash, cash_fresh = _persist_cash(client, out.get("cash"))
    rows = []
    if inv:
        for h in inv.holdings:
            rows.append({"category": "投信", "name": names.get(str(h["brand_id"])) or f"#{h['brand_id']}",
                         "valuation": h["valuation"], "unrealized_pl": h["unrealized_pl"],
                         "account_types": []})   # 投信 口座別 needs the ledger (see _invtrust_lots)
    for h in (out.get("sec_usa") or []):
        if h.is_cash:
            continue
        rows.append({"category": "証券", "name": h.name,
                     "valuation": h.valuation, "unrealized_pl": h.unrealized_pl,
                     "account_types": list(h.account_types or [])})
    sources = {
        "securities": "ok" if out.get("sec_usa") is not None else "failed",
        "invtrust": "ok" if out.get("inv_top") is not None else "failed",
        "cash": "live" if cash_fresh else ("stale" if cash is not None else "failed"),
    }
    return rows, cash, cash_fresh, (inv.sell_order_pending if inv else None), sources


# Best-effort ETF classification (factual labelling, not a judgment): a holding is
# called an ETF when its name is a known ETF ticker or contains "ETF".
_ETF_TICKERS = {
    "QQQ", "QQQM", "TQQQ", "SQQQ", "SPY", "SPLG", "VOO", "VTI", "IVV", "SPXL",
    "SOXL", "SOXX", "SMH", "DIA", "IWM", "ARKK", "VGT", "XLK", "SCHD", "JEPI",
    "JEPQ", "VYM", "VT", "VEA", "VWO", "GLD", "SLV", "TLT", "AGG", "BND", "VUG",
}


def _kind_of(row: dict) -> str:
    if row.get("category") == "投信":
        return "投信"
    name = (row.get("name") or "").upper()
    if "ETF" in name:
        return "ETF"
    # Pull the ASCII ticker run(s) out of a mixed name (e.g. インベスコQQQ → QQQ,
    # "SPDR S&P500" → S, P500…) so a fund whose ticker is embedded still classifies.
    runs = re.findall(r"[A-Z0-9]{2,}", name)
    if any(r in _ETF_TICKERS for r in runs):
        return "ETF"
    return "個股"


# Fund-name hints that a JPY-priced 投信 actually holds US equity underneath, so its
# *underlying* exposure is American even though its *quotation currency* is JPY.
_US_FUND_HINTS = ("S&P", "SP500", "NASDAQ", "ナスダック", "米国", "全米", "アメリカ", "ダウ", "DOW")


def _us_underlying(row: dict) -> bool:
    """True when the holding's UNDERLYING is US equity (vs its quotation currency).
    PayPay 米国株 (証券) are US-listed; a 投信 is judged by name (best-effort)."""
    if row.get("category") == "証券":
        return True
    name = (row.get("name") or "").upper()
    return any(h.upper() in name for h in _US_FUND_HINTS)


def _risk_payload(rows: list, cash, sell_pending, sources: dict) -> dict:
    """Pure structural-exposure math over consolidated holdings (FACTS ONLY).
    Separated from cmd_risk so it's unit-testable without network."""
    invested = sum(r.get("valuation") or 0 for r in rows)
    total = invested + (cash or 0)

    def pct(x):
        return round(100.0 * (x or 0) / total, 1) if total else 0.0

    positions = sorted(
        ({"name": r.get("name"), "category": r.get("category"), "kind": _kind_of(r),
          "valuation": r.get("valuation") or 0, "weight_pct": pct(r.get("valuation"))} for r in rows),
        key=lambda x: -x["valuation"])
    by_kind, by_cat = {}, {}
    for pos in positions:
        by_kind[pos["kind"]] = by_kind.get(pos["kind"], 0) + pos["valuation"]
        by_cat[pos["category"]] = by_cat.get(pos["category"], 0) + pos["valuation"]
    if cash:
        by_kind["現金"] = by_kind.get("現金", 0) + cash
        by_cat["現金"] = by_cat.get("現金", 0) + cash
    usd_assets = sum(r.get("valuation") or 0 for r in rows if r.get("category") == "証券")  # US-listed (JPY→USD quote)
    us_underlying = sum(r.get("valuation") or 0 for r in rows if _us_underlying(r))  # incl. S&P500等 投信
    largest = positions[0] if positions else None

    def topn_pct(n):
        # sum RAW valuations then round once (summing pre-rounded weights overshoots
        # 100%); clamp so it never reads e.g. 100.1%.
        return min(100.0, pct(sum(p["valuation"] for p in positions[:n])))
    return {
        "as_of": _now_jst_str(),
        "grand_total": total, "invested_total": invested,
        "cash": cash, "cash_pct": pct(cash or 0),
        "largest_position": ({"name": largest["name"], "weight_pct": largest["weight_pct"]}
                             if largest else None),
        "top1_pct": topn_pct(1),
        "top3_pct": topn_pct(3),
        "top5_pct": topn_pct(5),
        "usd_asset_pct": pct(usd_assets),
        "us_underlying_pct": pct(us_underlying),
        "by_kind_pct": {k: round(100.0 * v / total, 1) for k, v in by_kind.items()} if total else {},
        "by_category_pct": {k: round(100.0 * v / total, 1) for k, v in by_cat.items()} if total else {},
        "positions": positions,
        "invtrust_sell_pending": sell_pending,
        "sources": sources,
        "note": "構造・ウェイトの事実のみ。リスク評価・推奨・売買助言ではありません。",
    }


def _account_split(rows, invtrust_lots, cash, total) -> dict:
    """口座区分 (特定 / NISA成長 / つみたて / 現金) breakdown by valuation %. 証券 from
    each holding's account_types; 投信 from the ledger lots; cash → 特定(現金)."""
    by: dict = {}

    def add(acct, val):
        by[acct] = by.get(acct, 0) + (val or 0)

    for r in rows:
        if r.get("category") == "証券":
            accts = r.get("account_types") or ["特定"]
            add(accts[0], r.get("valuation"))   # usually a single 口座; attribute to it
    for lot in (invtrust_lots or []):
        add(lot.get("acct") or "特定", lot.get("valuation"))
    if cash:
        add("特定(現金)", cash)
    return {k: round(100.0 * v / total, 1) for k, v in by.items()} if total else {}


def cmd_risk(client: PayPayClient, args) -> int:
    """Structural exposure of the account — FACTS ONLY (weights, concentration,
    category/currency split). No risk verdicts, no buy/sell advice."""
    rows, cash, cash_fresh, sell_pending, sources = _consolidated_holdings(client)
    payload = _risk_payload(rows, cash, sell_pending, sources)
    # --accounts: add the 特定/NISA 口座 breakdown (needs the heavier 投信 ledger).
    if getattr(args, "accounts", False):
        try:
            lots, _, _ = _invtrust_lots(client, _pages(args, 30))
        except Exception:  # noqa: BLE001 — degrade: 証券 split only
            lots = []
            payload["sources"] = {**(payload.get("sources") or {}), "invledger": "failed"}
        payload["by_account_pct"] = _account_split(rows, lots, cash, payload["grand_total"])

    def table(p):
        print(f"PayPay証券 — 持仓结构 / exposure (事実のみ)\n")
        print(f"  総資産 {_yen(p['grand_total'])}  (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])} = {p['cash_pct']}%)")
        lg = p["largest_position"]
        print(f"  最大单一持仓 : {lg['name'] if lg else '—'} {lg['weight_pct'] if lg else 0}%")
        print(f"  集中度       : top1 {p['top1_pct']}% / top3 {p['top3_pct']}% / top5 {p['top5_pct']}%")
        print(f"  米国 計価(証券) : {p['usd_asset_pct']}%   (USD建で値付け)")
        print(f"  米国株 底層暴露 : {p['us_underlying_pct']}%   (S&P500等の投信も含む実質米株)")
        print(f"  种类构成     : " + " / ".join(f"{k} {v}%" for k, v in p["by_kind_pct"].items()))
        print(f"  账户构成     : " + " / ".join(f"{k} {v}%" for k, v in p["by_category_pct"].items()))
        if p.get("by_account_pct"):
            print(f"  口座区分     : " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        print("\n  持仓ウェイト:")
        print("    " + _lj("NAME", 28) + _lj("种类", 8) + _rj("市值", 12) + _rj("占比", 8))
        for pos in p["positions"]:
            print("    " + _lj(pos["name"] or "", 28) + _lj(pos["kind"], 8)
                  + _rj(_yen(pos["valuation"]), 12) + _rj(f"{pos['weight_pct']}%", 8))
        if p["cash"]:
            print("    " + _lj("現金", 28) + _lj("現金", 8)
                  + _rj(_yen(p["cash"]), 12) + _rj(f"{p['cash_pct']}%", 8))
        fl = []
        _freshness_block(p, fl)
        if fl:
            print()
            for line in fl:
                print(line)
        print(f"\n  注: {p['note']}")

    def lark(p):
        lg = p["largest_position"]
        L = [f"**PayPay証券 持仓结构 (事実のみ)**", "",
             f"- 総資産 **{_yen(p['grand_total'])}** (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])} = {p['cash_pct']}%)",
             f"- 最大单一持仓: **{lg['name'] if lg else '—'} {lg['weight_pct'] if lg else 0}%**",
             f"- 集中度: top1 {p['top1_pct']}% / top3 {p['top3_pct']}% / top5 {p['top5_pct']}%",
             f"- 米国 計価(証券): {p['usd_asset_pct']}% / 米国株 底層暴露: **{p['us_underlying_pct']}%** (S&P500等の投信も実質米株)",
             f"- 种类构成: " + " / ".join(f"{k} {v}%" for k, v in p["by_kind_pct"].items()),
             f"- 账户构成: " + " / ".join(f"{k} {v}%" for k, v in p["by_category_pct"].items())]
        if p.get("by_account_pct"):
            L.append("- 口座区分: " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        L += ["- 持仓ウェイト:"]
        for pos in p["positions"]:
            L.append(f"  - {pos['name']} ({pos['kind']}): {_yen(pos['valuation'])} / {pos['weight_pct']}%")
        if p["cash"]:
            L.append(f"  - 現金: {_yen(p['cash'])} / {p['cash_pct']}%")
        _freshness_block(p, L)
        L.append(f"\n> {p['note']}")
        print("\n".join(L))

    _emit_fmt(payload, _fmt(args), table, lark)
    return 0


def cmd_assets(client: PayPayClient, args) -> int:
    """One-shot consolidated view: 証券 + 投信 holdings + securities cash.
    Independent fetches run concurrently (login is established first)."""
    rows, cash, cash_fresh, sell_pending, sources = _consolidated_holdings(client)
    invested = sum(r["valuation"] or 0 for r in rows)
    grand_total = invested + (cash or 0)
    payload = {
        "as_of": _now_jst_str(),
        "holdings": rows,
        "invested_total": invested,
        "cash": cash, "cash_fresh": cash_fresh,
        "grand_total": grand_total,
        "invtrust_sell_pending": sell_pending,
        "sources": sources,
        "note": "grand_total = invested holdings + 証券 cash balance, matching the "
                "app's 保有資産 total. CFD (separate login) is not included.",
    }
    if getattr(args, "accounts", False):
        try:
            lots, _, _ = _invtrust_lots(client, _pages(args, 30))
        except Exception:  # noqa: BLE001
            lots = []
            payload["sources"] = {**payload["sources"], "invledger": "failed"}
        payload["by_account_pct"] = _account_split(rows, lots, cash, payload["grand_total"])

    def render(p):
        # Weights are over the GRAND TOTAL (incl. cash) to match the app's 資産割合
        # donut — the app's denominator is 総資産, not just 投資資産.
        denom = p["grand_total"] or 0
        print("PayPay証券 — consolidated assets (read-only)\n")
        print(_lj("CATEGORY", 9) + _lj("NAME", 34) + _rj("VALUATION", 12)
              + _rj("WEIGHT", 8) + _rj("P&L", 10))
        print("-" * 73)
        for r in sorted(p["holdings"], key=lambda x: -(x["valuation"] or 0)):
            wt = f"{100 * (r['valuation'] or 0) / denom:.1f}%" if denom else "—"
            print(_lj(r["category"], 9) + _lj(r["name"] or "", 34)
                  + _rj(_yen(r["valuation"]), 12) + _rj(wt, 8) + _rj(_yen(r["unrealized_pl"]), 10))
        if p["cash"]:
            cash_wt = f"{100 * p['cash'] / denom:.1f}%" if denom else "—"
            print(_lj("現金", 9) + _lj("(buyable cash)", 34)
                  + _rj(_yen(p["cash"]), 12) + _rj(cash_wt, 8) + _rj("—", 10))
        print("-" * 73)
        cstale = "" if p.get("cash_fresh", True) else "  ⚠stale(取得失敗)"
        print(f"  投資資産 : {_yen(p['invested_total'])}")
        print(f"  現金     : {_yen(p['cash'])}{cstale}")
        print(f"  総資産合計: {_yen(p['grand_total'])}")
        if p.get("by_account_pct"):
            print("  口座区分 : " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        print(f"\n  投信 売却申込中 (settling): {_yen(p['invtrust_sell_pending'])}")
        print("  注: CFD(別ログイン)は未集計。")
        fl = []
        _freshness_block(p, fl)
        if fl:
            print()
            for line in fl:
                print(line)

    def lark(p):
        denom = p["grand_total"] or 0
        L = ["**PayPay証券 资产总览**", "",
             f"- 総資産: **{_yen(p['grand_total'])}** (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])})"]
        for r in sorted(p["holdings"], key=lambda x: -(x["valuation"] or 0)):
            wt = f"{100 * (r['valuation'] or 0) / denom:.1f}%" if denom else "—"
            L.append(f"  - [{r['category']}] {r['name']}: {_yen(r['valuation'])} ({wt}) {_signed_yen(r['unrealized_pl'])}")
        if p["cash"]:
            cw = f"{100 * p['cash'] / denom:.1f}%" if denom else "—"
            L.append(f"  - [現金] buyable: {_yen(p['cash'])} ({cw})")
        if p.get("by_account_pct"):
            L.append("- 口座区分: " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        L.append(f"- 投信 売却申込中: {_yen(p['invtrust_sell_pending'])}")
        _freshness_block(p, L)
        L.append("\n> CFD(別ログイン)は未集計")
        print("\n".join(L))

    _emit_fmt(payload, _fmt(args), render, lark)
    return 0


def cmd_trades(client: PayPayClient, args) -> int:
    recs = client.settlement_records(max_pages=_pages(args, 2))
    txns = parsers.parse_transactions(recs)
    payload = {"current_cash": parsers.current_cash(recs), "transactions": txns}

    def render(p):
        print(f"取引履歴 (transaction ledger)   現金残高: {_yen(p['current_cash'])}\n")
        print(_lj("DATE", 12) + _lj("TYPE", 11) + _lj("BRAND", 28)
              + _rj("AMOUNT", 11) + _rj("PRICE", 9) + _rj("FX", 8) + _rj("BALANCE", 12))
        print("-" * 91)
        for t in p["transactions"]:
            price = f"${t['price']:.2f}" if t["price"] else "—"
            fx = f"{t['fx']:.1f}" if t["fx"] else "—"
            print(_lj(t["date"] or "", 12) + _lj(t["type"] or "", 11) + _lj(t["brand"] or "", 28)
                  + _rj(_yen(t["amount"]), 11) + _rj(price, 9) + _rj(fx, 8)
                  + _rj(_yen(t["cash_balance"]), 12))

    _emit(payload, args.json, render)
    return 0


def cmd_history(client: PayPayClient, args) -> int:
    hist = parsers.parse_history_series(client.history_html(args.market))

    def render(h):
        print(f"asset/cash daily series  {h['from_date']} ~ {h['end_date']}\n")
        print(f"{'DATE':<12}{'ASSET':>12}{'CASH':>12}")
        print("-" * 36)
        for p in h["points"]:
            print(f"{p['label']:<12}{_yen(p['asset_yen']):>12}{_yen(p['cash_yen']):>12}")

    _emit(hist, args.json, render)
    return 0


def _invtrust_lots(client: PayPayClient, pages: int = 30):
    """投信 current holdings split by 口座区分 (特定 / NISA成長 / つみたて) via the
    MARKET_ID=99 ledger lots, each lot's valuation apportioned by its net 口数 (all
    口 of a fund share one 基準価額, so the split is exact). Returns (holdings, agg, txns)."""
    recs = client.invtrust_settlement_records(max_pages=pages)
    txns = parsers.parse_invtrust_transactions(recs)
    agg = report.aggregate_invtrust(txns)
    inv = parsers.parse_invtrust(client.invtrust_top())
    names = client.invtrust_brands()
    fund_val = {names.get(str(h["brand_id"])): (h["valuation"] or 0)
                for h in (inv.holdings if inv else []) if names.get(str(h["brand_id"]))}
    lots = [b for b in agg["brands"] if b["net_shares"] > 1e-6]
    brand_shares: dict = {}
    for b in lots:
        brand_shares[b["brand"]] = brand_shares.get(b["brand"], 0.0) + b["net_shares"]
    holdings = []
    for b in lots:
        tot = brand_shares.get(b["brand"]) or 0
        val = round(fund_val.get(b["brand"], 0) * b["net_shares"] / tot) if tot else 0
        holdings.append({"brand": b["brand"], "acct": b["acct"], "net_shares": b["net_shares"],
                         "cost": b["net_invested"], "valuation": val,
                         "unrealized_pl": val - b["net_invested"]})
    holdings.sort(key=lambda x: -x["valuation"])
    return holdings, agg, txns


def cmd_invtrust_history(client: PayPayClient, args) -> int:
    """投信 (mutual-fund) transaction ledger — the MARKET_ID=99 settlements feed
    that the 証券 ajax ledger never shows: 買付/売却/入金/譲渡益税/送金手数料."""
    holdings, agg, txns = _invtrust_lots(client, _pages(args, 20))

    payload = {"transactions": txns, "holdings_by_lot": holdings,
               "summary": {k: agg[k] for k in ("realized_pl", "reconciles",
                           "capital_gains_tax", "transfer_fees", "deposits_gross",
                           "distributions", "buys_yen", "sells_yen", "brands",
                           "date_from", "date_to")}}

    def render(p):
        s = p["summary"]
        print(f"投信 取引明細 (MARKET_ID=99)  {s['date_from']} 〜 {s['date_to']}\n")
        print(_lj("DATE", 12) + _lj("TYPE", 10) + _lj("口座", 12) + _lj("BRAND", 26)
              + _rj("口数", 11) + _rj("AMOUNT", 11) + _rj("BALANCE", 12))
        print("-" * 94)
        for t in p["transactions"]:
            qty = f"{t['qty']:,.0f}" if t["qty"] else "—"
            print(_lj(t["date"] or "", 12) + _lj(t["type"] or "", 10)
                  + _lj(str(t["account_type"] or "—"), 12) + _lj((t["brand"] or "—")[:24], 26)
                  + _rj(qty, 11) + _rj(_yen(t["amount"]), 11) + _rj(_yen(t["cash_balance"]), 12))
        print("-" * 94)
        print(f"  買付 {_yen(s['buys_yen'])}  /  売却 {_yen(s['sells_yen'])}  /  入金(振替含) {_yen(s['deposits_gross'])}")
        print(f"  譲渡益税 {_yen(s['capital_gains_tax'])}  /  送金手数料 {_yen(s['transfer_fees'])}  /  分配金 {_yen(s['distributions'])}")
        mark = "" if s["reconciles"] else "   ⚠ 一部ロットで売却口数>取得口数(取得単価不足→過小評価)"
        print(f"  実現損益(移動平均): {_signed_yen(s['realized_pl'])}{mark}")
        if p["holdings_by_lot"]:
            print("\n  現保有(口座別):")
            print("    " + _lj("BRAND", 32) + _lj("口座", 14) + _rj("口数", 11)
                  + _rj("取得額", 11) + _rj("評価額", 11) + _rj("含み損益", 10))
            for h in p["holdings_by_lot"]:
                print("    " + _lj((h["brand"] or "")[:30], 32) + _lj(h["acct"] or "—", 14)
                      + _rj(f"{h['net_shares']:,.0f}", 11) + _rj(_yen(h["cost"]), 11)
                      + _rj(_yen(h["valuation"]), 11) + _rj(_signed_yen(h["unrealized_pl"]), 10))
        if s["brands"]:
            print("\n  銘柄×口座別 実現損益:")
            for b in s["brands"]:
                print("    " + _lj(b["name"], 42) + _rj(_signed_yen(b["realized_pl"]), 11))

    _emit(payload, args.json, render)
    return 0


def _guard_ctx(client, args) -> "_guards.GuardContext":
    acct = getattr(args, "account", None)
    return _guards.GuardContext(
        now=datetime.now(timezone.utc),
        today_order_count=_audit.count_today(account=acct, kind="submit"),
        current_quote=None,        # Task 13: fill from a quote fetch
        portfolio_total_jpy=None,  # Task 13: fill from portfolio total
    )


def _order_common(client, args, side: str) -> int:
    try:
        req = _orders.build(market=args.market, symbol=args.symbol, side=side,
                            qty=args.qty, amount_jpy=args.amount, limit=args.limit,
                            market_order=getattr(args, "market_order", False),
                            account=getattr(args, "account", None),
                            account_type=getattr(args, "account_type", 2))
    except _orders.OrderError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    cfg = _guards.load_trade_config(getattr(args, "account", None))
    ctx = _guard_ctx(client, args)
    confirm = lambda r: client.order_confirm(r)
    # TRADE_PASSWORD (取引パスワード) is prompted (not echoed, never stored) only if/when
    # we actually submit — the agent never supplies it; the human does, at the keyboard.
    _pw = {}
    def _trade_pw():
        if "v" not in _pw:
            try:
                _pw["v"] = getpass.getpass("  取引パスワード (trade password): ")
            except (EOFError, KeyboardInterrupt):
                _pw["v"] = ""
        return _pw["v"]
    try:
        if getattr(args, "execute", False):
            res = _orders.place(req, cfg, ctx, confirm=confirm,
                                submit=lambda tok, r: client.order_submit(tok, r, trade_password=_trade_pw()),
                                confirmer=make_confirmer())
        else:
            res = _orders.dry_run(req, cfg, ctx, confirm=confirm)
    except NotImplementedError as e:
        print(f"[pending Phase 0] guards OK, but {e}", file=sys.stderr)
        _audit.record({"kind": "dry_run", "symbol": req.symbol, "side": side,
                       "qty": req.qty, "amount_jpy": req.amount_jpy,
                       "limit": req.limit_price, "status": "confirm_pending_phase0"},
                      account=getattr(args, "account", None))
        return 3
    except (RuntimeError, requests.RequestException) as e:
        # confirm rejected (e.g. market closed / over buyable), session, or submit error
        print(f"error: {e}", file=sys.stderr)
        _audit.record({"kind": "error", "symbol": req.symbol, "side": side,
                       "qty": req.qty, "amount_jpy": req.amount_jpy,
                       "limit": req.limit_price, "error": str(e)[:200]},
                      account=getattr(args, "account", None))
        return 1

    _audit.record({"kind": "submit" if res.submitted else "dry_run",
                   "symbol": req.symbol, "side": side, "qty": req.qty,
                   "amount_jpy": req.amount_jpy, "limit": req.limit_price,
                   "violations": res.violations, "order_id": res.order_id},
                  account=getattr(args, "account", None))

    def render(d):
        # d is res.__dict__ (a plain dict — _emit also json-serializes it)
        if d.get("violations"):
            print("⛔ order blocked by guards:")
            for v in d["violations"]:
                print(f"   - {v}")
            return
        pv = d.get("preview") or {}
        print(f"{'✅ SUBMITTED' if d.get('submitted') else '🔎 DRY-RUN (not sent)'}  "
              f"{side.upper()} {req.symbol}")
        if pv.get("total_jpy") is not None:
            print(f"   est. total : ¥{pv['total_jpy']:,}   est. price: {pv.get('est_price')}   fee: ¥{pv.get('fee_jpy', 0):,}")
        if d.get("submitted"):
            print(f"   order id   : {d.get('order_id')}")
        elif not getattr(args, 'execute', False):
            print("   (re-run with --execute to place; you will be asked to type a confirmation)")

    _emit(res.__dict__, getattr(args, "json", False), render)
    return 0 if (res.submitted or not res.violations) else 1


def cmd_buy(client, args) -> int:
    return _order_common(client, args, "buy")


def cmd_sell(client, args) -> int:
    return _order_common(client, args, "sell")


def cmd_orders(client, args) -> int:
    try:
        rows = client.open_orders(args.market)
    except NotImplementedError as e:
        print(f"[pending Phase 0] {e}", file=sys.stderr); return 3
    payload = {"open_orders": rows}

    def render(p):
        print("予約注文 (open orders)\n")
        if not p["open_orders"]:
            print("  (なし)")
            return
        print(_lj("ID", 14) + _lj("受付日時", 18) + _lj("銘柄", 12) + _lj("売買", 6)
              + _lj("口座", 10) + _rj("金額・株数", 14) + "  ステータス")
        for r in p["open_orders"]:
            print(_lj(str(r.get("order_id", "") or "—"), 14) + _lj(r.get("datetime", ""), 18)
                  + _lj(r.get("symbol", ""), 12) + _lj(r.get("side", ""), 6)
                  + _lj(r.get("account_type", ""), 10) + _rj(r.get("size", ""), 14)
                  + "  " + str(r.get("status", "")) + (f"  {r['note']}" if r.get("note") else ""))

    _emit(payload, getattr(args, "json", False), render)
    return 0


def cmd_cancel(client, args) -> int:
    if not getattr(args, "execute", False):
        print(f"🔎 DRY-RUN: would cancel order {args.order_id}. Re-run with --execute.")
        return 0
    try:
        typed = input(f"  To cancel, type the order id exactly: {args.order_id}\n  confirm> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("cancel aborted"); return 1
    if typed != str(args.order_id):
        print("cancel aborted (phrase mismatch)"); return 1
    try:
        out = client.order_cancel(args.order_id, args.market)
    except NotImplementedError as e:
        print(f"[pending Phase 0] {e}", file=sys.stderr); return 3
    _audit.record({"kind": "cancel", "order_id": args.order_id, "response": out},
                  account=getattr(args, "account", None))
    print(f"✅ cancel requested for {args.order_id}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # shared flags live on a parent so they work AFTER the subcommand
    # (e.g. `paypay portfolio -m usa --json`)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    common.add_argument("--format", dest="fmt", choices=("table", "lark", "json"), default=None,
                        help="output format: table (default) | lark (Feishu bullets) | json")
    common.add_argument("-m", "--market", default="usa",
                        help="market: usa | japan (aliases: jp, us, 米国株, 日本株). default usa")
    common.add_argument("--no-cache", action="store_true",
                        help="bypass the local response cache (always hit the API)")
    common.add_argument("-a", "--account", default=None,
                        help="account profile: reads ~/.paypay-sec/<name>.env "
                             "(default account uses ~/.paypay-sec/.env)")
    common.add_argument("--lang", choices=("ja", "zh"), default="ja",
                        help="language for table/lark labels: ja (default) | zh (中文). "
                             "JSON output keys stay English.")
    common.add_argument("--all", dest="fetch_all", action="store_true",
                        help="fetch the FULL ledger history (page until NEXT_FLG=false) "
                             "instead of the default page cap — for complete realized P&L")

    p = argparse.ArgumentParser(prog="paypay", description="Read-only PayPay証券 client (Phase 1)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn in (("login", cmd_login), ("logout", cmd_logout),
                     ("balance", cmd_balance), ("portfolio", cmd_portfolio),
                     ("history", cmd_history), ("invtrust", cmd_invtrust),
                     ("total", cmd_total), ("assets", cmd_assets), ("trades", cmd_trades),
                     ("invtrust-history", cmd_invtrust_history),
                     ("fees", cmd_fees), ("review", cmd_review),
                     ("trades-summary", cmd_trades_summary), ("risk", cmd_risk),
                     ("plans", cmd_plans), ("tax", cmd_tax),
                     ("accounts", cmd_accounts), ("doctor", cmd_doctor),
                     ("cache-clear", cmd_cache_clear)):
        sp = sub.add_parser(name, parents=[common])
        sp.set_defaults(func=fn)
        if name == "trades":
            sp.add_argument("--pages", type=int, default=2,
                            help="how many pages of ledger history to fetch (20 rows each)")
        if name in ("review", "trades-summary"):
            sp.add_argument("--pages", type=int, default=8,
                            help="how many pages of ledger history to scan (20 rows each)")
        if name == "invtrust-history":
            sp.add_argument("--pages", type=int, default=20,
                            help="how many pages of 投信 ledger to fetch (20 rows each)")
        if name in ("plans", "tax"):
            sp.add_argument("--pages", type=int, default=30,
                            help="投信 ledger pages to scan (20 rows each)")
        if name == "doctor":
            sp.add_argument("--online", action="store_true",
                            help="also probe the API (login + 証券/投信 fetch) to confirm the session is live")
        if name in ("risk", "assets"):
            sp.add_argument("--accounts", action="store_true",
                            help="add the 口座区分 (特定/NISA成長/つみたて) split — fetches the 投信 ledger")
            sp.add_argument("--pages", type=int, default=30,
                            help="投信 ledger pages for the 口座 split (with --accounts)")
        if name == "fees":
            sp.add_argument("--pages", type=int, default=4,
                            help="how many pages of ledger history to scan")
            sp.add_argument("--detail", action="store_true", help="show per-trade FX spread")
            sp.add_argument("--price-spread-pct", type=float, default=0.0,
                            help="add an estimated price-spread cost at this %% of US turnover")

    snap = sub.add_parser("snapshot", parents=[common]); snap.set_defaults(func=cmd_snapshot)
    snap.add_argument("snap_cmd", nargs="?", choices=("save", "list"), default="save",
                      help="save (default) a snapshot of the account, or list saved snapshots")
    snap.add_argument("--pages", type=int, default=8, help="ledger pages to scan for the snapshot")

    dff = sub.add_parser("diff", parents=[common]); dff.set_defaults(func=cmd_diff)
    dff.add_argument("--days", type=int, default=None,
                     help="compare a live read against the snapshot ~N days old (default: the latest snapshot)")
    dff.add_argument("--since", default=None,
                     help="baseline selector; 'last' = latest snapshot (the default)")
    dff.add_argument("--pages", type=int, default=8, help="ledger pages to scan for the live read")

    buy = sub.add_parser("buy", parents=[common]); buy.set_defaults(func=cmd_buy)
    sell = sub.add_parser("sell", parents=[common]); sell.set_defaults(func=cmd_sell)
    for sp_ in (buy, sell):
        sp_.add_argument("symbol", help="ticker, e.g. TSLA")
        g = sp_.add_mutually_exclusive_group(required=True)
        g.add_argument("--qty", type=float, help="number of shares (株数指定)")
        g.add_argument("--amount", type=int, help="order amount in JPY (金額指定)")
        sp_.add_argument("--limit", type=float, default=None, help="limit price (指値); optional — US fills at the quote, omit for a market-priced order")
        sp_.add_argument("--market-order", action="store_true", help="place a market order (成行) — blocked unless allowed in trade.json")
        sp_.add_argument("--account-type", dest="account_type", type=int, choices=(2, 3, 4), default=2,
                         help="brokerage account: 2=特定(cash, default) | 3=成長投資枠NISA | 4=つみたて")
        sp_.add_argument("--execute", action="store_true", help="actually place the order (default is dry-run)")

    ords = sub.add_parser("orders", parents=[common]); ords.set_defaults(func=cmd_orders)
    canc = sub.add_parser("cancel", parents=[common]); canc.set_defaults(func=cmd_cancel)
    canc.add_argument("order_id", help="order id from `paypay orders`")
    canc.add_argument("--execute", action="store_true", help="actually cancel (default is dry-run)")

    return p


# Trading commands are OFF by default — a read-only/复盘 invocation should not even
# be able to place an order. They run only when the human sets PAYPAY_TRADING_ENABLED=1
# (in ~/.paypay-sec/.env or the shell). This is a capability gate on top of the
# existing dry-run-default + typed-confirm + TRADE_PASSWORD wall.
_TRADING_CMDS = {cmd_buy, cmd_sell, cmd_orders, cmd_cancel}


def _trading_enabled() -> bool:
    return os.environ.get("PAYPAY_TRADING_ENABLED", "").strip() == "1"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    global _LANG
    _LANG = getattr(args, "lang", "ja")
    # `accounts` / `doctor` only inspect local config — no credentials / network.
    if args.func is cmd_accounts:
        return cmd_accounts(None, args)
    if args.func is cmd_doctor:
        return cmd_doctor(None, args)
    # -a all: consolidate across every configured profile (handled inside the cmd).
    if getattr(args, "account", None) == "all":
        if args.func not in (cmd_total, cmd_plans, cmd_tax):
            print("error: -a all is supported only for total / plans / tax", file=sys.stderr)
            return 2
        return args.func(None, args)
    try:
        settings = Settings.from_env(getattr(args, "account", None))  # also loads .env
        if args.func in _TRADING_CMDS and not _trading_enabled():
            print("error: trading commands (buy/sell/orders/cancel) are DISABLED.\n"
                  "       Set PAYPAY_TRADING_ENABLED=1 in ~/.paypay-sec/.env (or export it) "
                  "to enable.\n       Read-only / 复盘 commands need nothing.", file=sys.stderr)
            return 2
        client = PayPayClient(settings,
                              cache_ttl=0 if getattr(args, "no_cache", False) else None)
        return args.func(client, args)
    except LoginError as e:
        print(f"error: login failed — {e}", file=sys.stderr)
        return 1
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"error: HTTP {code} fetching {e.request.url if e.request else ''} "
              f"— check the --market value (try 'usa' or 'japan')", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
