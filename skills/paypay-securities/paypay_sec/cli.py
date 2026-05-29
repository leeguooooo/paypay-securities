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
import sys
import unicodedata

import requests

from .client import LoginError, PayPayClient
from .config import Settings
from . import parsers, costs, market


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
    common.add_argument("-m", "--market", default="usa",
                        help="market: usa | japan (aliases: jp, us, 米国株, 日本株). default usa")
    common.add_argument("--no-cache", action="store_true",
                        help="bypass the local response cache (always hit the API)")

    p = argparse.ArgumentParser(prog="paypay", description="Read-only PayPay証券 client (Phase 1)")
    sub = p.add_subparsers(dest="command", required=True)
    for name, fn in (("login", cmd_login), ("logout", cmd_logout),
                     ("balance", cmd_balance), ("portfolio", cmd_portfolio),
                     ("history", cmd_history), ("invtrust", cmd_invtrust),
                     ("total", cmd_total), ("assets", cmd_assets), ("trades", cmd_trades),
                     ("fees", cmd_fees), ("cache-clear", cmd_cache_clear)):
        sp = sub.add_parser(name, parents=[common])
        sp.set_defaults(func=fn)
        if name == "trades":
            sp.add_argument("--pages", type=int, default=2,
                            help="how many pages of ledger history to fetch (20 rows each)")
        if name == "fees":
            sp.add_argument("--pages", type=int, default=4,
                            help="how many pages of ledger history to scan")
            sp.add_argument("--detail", action="store_true", help="show per-trade FX spread")
            sp.add_argument("--price-spread-pct", type=float, default=0.0,
                            help="add an estimated price-spread cost at this %% of US turnover")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        client = PayPayClient(Settings.from_env(),
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
