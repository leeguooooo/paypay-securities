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
import json
import os
import sys
import unicodedata

import requests

from .client import LoginError, PayPayClient
from .config import Settings
from . import parsers, costs, market, config, report


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


def _emit_fmt(payload, fmt, table_fn, lark_fn) -> None:
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif fmt == "lark":
        lark_fn(payload)
    else:
        table_fn(payload)


def _emit(obj, as_json: bool, render):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        render(obj)


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
    txns = parsers.parse_transactions(client.settlement_records(max_pages=getattr(args, "pages", 3)))
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
    for h in holdings:
        h["kind"] = report.classify(h["name"], h["category"])
        h["is_stock"] = h["kind"] == "stock"
    cash = parsers.current_cash(ledger) or 0
    total = sum(h["valuation"] for h in holdings) + cash
    tdates = [t["date"] for t in txns if t["type"] in costs.TRADE_TYPES and t["date"]]
    series = market.usdjpy_series(min(tdates), max(tdates)) if tdates else {}
    return {"txns": txns, "holdings": holdings, "cash": cash, "total": total, "fx_series": series}


def cmd_review(client: PayPayClient, args) -> int:
    g = _gather(client, pages=getattr(args, "pages", 8))
    agg = report.aggregate_trades(g["txns"])
    fx_cost = costs.compute_costs(g["txns"], g["fx_series"])["fx_spread_cost"]
    rules = report.load_rules()
    risk = report.evaluate_risk(g["holdings"], g["total"], g["cash"], rules)
    unreal = sum((h["unrealized_pl"] or 0) for h in g["holdings"])
    total, cash = g["total"], g["cash"]
    hold = sorted(g["holdings"], key=lambda x: -x["valuation"])
    for h in hold:
        h["pct"] = round(100.0 * h["valuation"] / total, 1) if total else 0
    p = {
        "period": {"from": agg["date_from"], "to": agg["date_to"]},
        "total_assets": total, "cash": cash, "invested": total - cash,
        "unrealized_pl": unreal, "realized_pl": agg["realized_pl"],
        "explicit_fees": agg["explicit_fees"], "fx_spread_cost": fx_cost,
        "total_cost": agg["explicit_fees"] + fx_cost,
        "deposits": agg["deposits"], "withdrawals": agg["withdrawals"],
        "holdings": hold, "trades_by_brand": agg["brands"], "risk": risk,
        "note": "事実とユーザー定義ルールの照合のみ。売買助言ではありません。",
    }

    def table(p):
        pr = p["period"]
        print(f"PayPay証券 復盘  ({pr['from']} 〜 {pr['to']})\n")
        print(f"  総資産   : {_yen(p['total_assets'])}   (投資 {_yen(p['invested'])} / 現金 {_yen(p['cash'])})")
        print(f"  未実現損益: {_signed_yen(p['unrealized_pl'])}")
        print(f"  実現損益  : {_signed_yen(p['realized_pl'])}  (平均取得単価ベース)")
        print(f"  取引コスト: {_yen(p['total_cost'])}  (手数料 {_yen(p['explicit_fees'])} + 為替 {_yen(p['fx_spread_cost'])})")
        print(f"  累計入金  : {_yen(p['deposits'])}   出金: {_yen(p['withdrawals'])}")
        print("\n  保有:")
        for h in p["holdings"]:
            print("    " + _lj(h["name"], 30) + _rj(_yen(h["valuation"]), 11)
                  + _rj(f"{h['pct']}%", 7) + _rj(_signed_yen(h["unrealized_pl"]), 10))
        print("\n  リスク照合 (ユーザールール):")
        for c in p["risk"]["checks"]:
            print(f"    {'✅' if c['ok'] else '⚠️ '} {c['rule']}: {c['metric']} (上限/下限 {c['limit']})")
        print(f"\n  注: {p['note']}")

    def lark(p):
        pr = p["period"]
        L = [f"**PayPay証券 復盘 ({pr['from']}〜{pr['to']})**", "",
             "**資産**",
             f"- 総資産: **{_yen(p['total_assets'])}**(投資 {_yen(p['invested'])} / 現金 {_yen(p['cash'])})",
             "**損益**",
             f"- 未実現: **{_signed_yen(p['unrealized_pl'])}**",
             f"- 実現(平均取得単価): **{_signed_yen(p['realized_pl'])}**",
             f"- 取引コスト: **{_yen(p['total_cost'])}**(手数料 {_yen(p['explicit_fees'])}+為替 {_yen(p['fx_spread_cost'])})",
             f"- 累計入金: **{_yen(p['deposits'])}**",
             "**保有**"]
        for h in p["holdings"]:
            L.append(f"- {h['name']}: **{_yen(h['valuation'])}** ({h['pct']}%) {_signed_yen(h['unrealized_pl'])}")
        L.append("**リスク照合**")
        for c in p["risk"]["checks"]:
            L.append(f"- {'✅' if c['ok'] else '⚠️'} {c['rule']}: {c['metric']} / {c['limit']}")
        L.append(f"\n> {p['note']}")
        print("\n".join(L))

    _emit_fmt(p, _fmt(args), table, lark)
    return 0


def cmd_trades_summary(client: PayPayClient, args) -> int:
    txns = parsers.parse_transactions(client.settlement_records(max_pages=getattr(args, "pages", 8)))
    agg = report.aggregate_trades(txns)
    p = {"period": {"from": agg["date_from"], "to": agg["date_to"]},
         "deposits": agg["deposits"], "withdrawals": agg["withdrawals"],
         "explicit_fees": agg["explicit_fees"], "realized_pl": agg["realized_pl"],
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

    def lark(p):
        L = [f"**取引集計 ({p['period']['from']}〜{p['period']['to']})**", ""]
        for b in p["brands"]:
            L.append(f"- **{b['name']}**: 買 {_yen(b['buy_yen'])} / 売 {_yen(b['sell_yen'])} / "
                     f"純投入 {_yen(b['net_invested'])} / 残 {b['net_shares']:.4f}株 / "
                     f"実現 {_signed_yen(b['realized_pl'])}")
        L.append(f"- 累計入金 **{_yen(p['deposits'])}** / 手数料 {_yen(p['explicit_fees'])} / "
                 f"実現損益合計 **{_signed_yen(p['realized_pl'])}**")
        print("\n".join(L))

    _emit_fmt(p, _fmt(args), table, lark)
    return 0


def cmd_risk(client: PayPayClient, args) -> int:
    g = _gather(client, pages=1)
    rules = report.load_rules()
    risk = report.evaluate_risk(g["holdings"], g["total"], g["cash"], rules)
    p = {"total_assets": g["total"], **risk, "rules": rules,
         "note": "ユーザー定義ルールとの照合のみ。売買助言ではありません。"}

    def table(p):
        print(f"リスク照合  (総資産 {_yen(p['total_assets'])})\n")
        for c in p["checks"]:
            print(f"  {'✅ PASS' if c['ok'] else '⚠️  OVER'}  {c['rule']:<24} {c['metric']}  (基準 {c['limit']})")
        if p["breaches"]:
            print(f"\n  超過 {len(p['breaches'])} 件。閾値は ~/.paypay-sec/rules.json で調整可。")
        print(f"\n  注: {p['note']}")

    def lark(p):
        L = [f"**リスク照合**(総資産 {_yen(p['total_assets'])})", ""]
        for c in p["checks"]:
            L.append(f"- {'✅' if c['ok'] else '⚠️'} {c['rule']}: {c['metric']} / 基準 {c['limit']}")
        L.append(f"\n> {p['note']}")
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

    def render(d):
        print("投信 (mutual funds)")
        print(f"  valuation      : {_yen(d['valuation'])}")
        print(f"  principal      : {_yen(d['principal'])}")
        print(f"  unrealized P&L : {_yen(d['unrealized_pl'])}")
        print(f"  sell pending   : {_yen(d['sell_order_pending'])}")
        print(f"  buyable cash   : {_yen(d['buyable_cash'])}")
        if d["holdings"]:
            print("  holdings:")
            for h in d["holdings"]:
                label = h.get("name") or f"brand#{h['brand_id']}"
                print("    " + _lj(label, 34) + _rj(_yen(h["valuation"]), 12)
                      + "  P&L " + _yen(h["unrealized_pl"]))

    _emit(d, args.json, render)
    return 0


def cmd_total(client: PayPayClient, args) -> int:
    # securities (stocks + ETF) total = sum across markets that hold positions.
    # Tolerate a transient per-market failure (e.g. 502) instead of aborting.
    sec_by_market = {}
    errors = []
    for mkt in ("usa", "japan"):
        try:
            s = parsers.parse_summary(client.portfolio_html(mkt))
            sec_by_market[mkt] = s.total_valuation or 0
        except (requests.HTTPError, requests.RequestException) as e:
            sec_by_market[mkt] = None
            errors.append(f"{mkt}: {type(e).__name__}")
    sec_total = sum(v for v in sec_by_market.values() if v)
    inv = parsers.parse_invtrust(client.invtrust_top())
    inv_val = inv.valuation or 0
    invested = sec_total + inv_val
    cash = parsers.current_cash(client.settlement_records(max_pages=1))
    grand_total = invested + (cash or 0)
    payload = {
        "securities_by_market": sec_by_market,
        "securities_total": sec_total,
        "invtrust_valuation": inv.valuation,
        "invested_total": invested,
        "cash": cash,
        "grand_total": grand_total,
        "invtrust_sell_pending": inv.sell_order_pending,
        "errors": errors,
        "note": ("grand_total = 証券 + 投信 holdings + 証券 cash balance, matching the "
                 "app's 保有資産 total. CFD (separate login) is not included."),
    }

    def render(p):
        print("PayPay証券 — total assets (read-only)\n")
        print(f"  証券 (株+ETF)   : {_yen(p['securities_total'])}")
        print(f"  投信 (基金)      : {_yen(p['invtrust_valuation'])}")
        print(f"  現金            : {_yen(p['cash'])}")
        print(f"  {'─' * 30}")
        print(f"  総資産 合計      : {_yen(p['grand_total'])}")
        print(f"\n  (うち投資資産 {_yen(p['invested_total'])} / 投信 売却申込中 {_yen(p['invtrust_sell_pending'])} settling)")
        if p["errors"]:
            print(f"\n  ⚠ 一部市場の取得に失敗(集計から除外): {', '.join(p['errors'])}")
        print("\n  注: CFD は別ログインのため未集計。")

    _emit(payload, args.json, render)
    return 0


def cmd_assets(client: PayPayClient, args) -> int:
    """One-shot consolidated view: 証券 + 投信 holdings + securities cash.
    Independent fetches run concurrently (login is established first)."""
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
    sec = out.get("sec_usa") or []
    cash = out.get("cash")

    rows = []
    if inv:
        for h in inv.holdings:
            rows.append({"category": "投信", "name": names.get(str(h["brand_id"])) or f"#{h['brand_id']}",
                         "valuation": h["valuation"], "unrealized_pl": h["unrealized_pl"]})
    for h in sec:
        if h.is_cash:
            continue
        rows.append({"category": "証券", "name": h.name,
                     "valuation": h.valuation, "unrealized_pl": h.unrealized_pl})
    invested = sum(r["valuation"] or 0 for r in rows)
    grand_total = invested + (cash or 0)
    payload = {
        "holdings": rows,
        "invested_total": invested,
        "cash": cash,
        "grand_total": grand_total,
        "invtrust_sell_pending": inv.sell_order_pending if inv else None,
        "note": "grand_total = invested holdings + 証券 cash balance, matching the "
                "app's 保有資産 total. CFD (separate login) is not included.",
    }

    def render(p):
        print("PayPay証券 — consolidated assets (read-only)\n")
        print(_lj("CATEGORY", 9) + _lj("NAME", 34) + _rj("VALUATION", 12)
              + _rj("WEIGHT", 8) + _rj("P&L", 10))
        print("-" * 73)
        for r in sorted(p["holdings"], key=lambda x: -(x["valuation"] or 0)):
            wt = f"{100 * (r['valuation'] or 0) / invested:.1f}%" if invested else "—"
            print(_lj(r["category"], 9) + _lj(r["name"] or "", 34)
                  + _rj(_yen(r["valuation"]), 12) + _rj(wt, 8) + _rj(_yen(r["unrealized_pl"]), 10))
        print("-" * 70)
        print(f"  投資資産 : {_yen(p['invested_total'])}")
        print(f"  現金     : {_yen(p['cash'])}")
        print(f"  総資産合計: {_yen(p['grand_total'])}")
        print(f"\n  投信 売却申込中 (settling): {_yen(p['invtrust_sell_pending'])}")
        print("  注: CFD(別ログイン)は未集計。")

    _emit(payload, args.json, render)
    return 0


def cmd_trades(client: PayPayClient, args) -> int:
    recs = client.settlement_records(max_pages=getattr(args, "pages", 2))
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

    p = argparse.ArgumentParser(prog="paypay", description="Read-only PayPay証券 client (Phase 1)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn in (("login", cmd_login), ("logout", cmd_logout),
                     ("balance", cmd_balance), ("portfolio", cmd_portfolio),
                     ("history", cmd_history), ("invtrust", cmd_invtrust),
                     ("total", cmd_total), ("assets", cmd_assets), ("trades", cmd_trades),
                     ("fees", cmd_fees), ("review", cmd_review),
                     ("trades-summary", cmd_trades_summary), ("risk", cmd_risk),
                     ("accounts", cmd_accounts), ("cache-clear", cmd_cache_clear)):
        sp = sub.add_parser(name, parents=[common])
        sp.set_defaults(func=fn)
        if name == "trades":
            sp.add_argument("--pages", type=int, default=2,
                            help="how many pages of ledger history to fetch (20 rows each)")
        if name in ("review", "trades-summary"):
            sp.add_argument("--pages", type=int, default=8,
                            help="how many pages of ledger history to scan (20 rows each)")
        if name == "fees":
            sp.add_argument("--pages", type=int, default=4,
                            help="how many pages of ledger history to scan")
            sp.add_argument("--detail", action="store_true", help="show per-trade FX spread")
            sp.add_argument("--price-spread-pct", type=float, default=0.0,
                            help="add an estimated price-spread cost at this %% of US turnover")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # `accounts` only lists profiles — it needs no credentials / network.
    if args.func is cmd_accounts:
        return cmd_accounts(None, args)
    try:
        settings = Settings.from_env(getattr(args, "account", None))
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
