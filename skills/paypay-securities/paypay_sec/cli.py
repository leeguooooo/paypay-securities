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


def _now_jst_iso() -> str:
    """ISO-8601 with the +09:00 offset (the calendar's generated_at format)."""
    return datetime.now(timezone.utc).astimezone(_JST).strftime("%Y-%m-%dT%H:%M:%S+09:00")


# --all pages until NEXT_FLG=false; this is the upper bound that keeps a stuck
# feed from looping forever (settlement_records already stops at NEXT_FLG).
_ALL_PAGES_CAP = 500


def _pages(args, default: int) -> int:
    """Resolve the ledger page count: a big cap when --all, else --pages/default."""
    if getattr(args, "fetch_all", False):
        return _ALL_PAGES_CAP
    return getattr(args, "pages", default)


def _measured_cost(explicit_fees, fx_cost, inv_tax, inv_transfer) -> dict:
    """Measured trading cost — see costs.measured_cost (shared with `fees`)."""
    return costs.measured_cost(explicit_fees, fx_cost, inv_tax, inv_transfer)


def _hist_pages(args) -> int:
    """Page count for the history-analysis commands (plans/tax): FULL history by
    default — they have no 'incomplete' warning, so a short fetch would silently
    under-report (e.g. ¥0 月定投 / ¥0 税). `--fast` caps it for a quick look.
    The ledger stops at NEXT_FLG anyway, so 'full' is cheap for a small account."""
    return 8 if getattr(args, "fast", False) else _ALL_PAGES_CAP


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


def _pick_cash(stored, sec_recs, inv_recs):
    """Pure core of _resolve_cash → (cash, fresh, to_store_or_None).

    The 証券 + 投信 ledgers share ONE cash pool and each only stamps CASH_BALANCE on
    its own rows, so cash = the newest row across both (never a sum). It counts as
    live only when BOTH ledgers returned rows — with one throttled to empty, the
    other's newest row may predate a movement the missing one holds. `stored` is
    the last_cash.json dict ({"cash", "key": [date, seq]}); whichever of stored vs
    fetched carries the newer (BASE_D, SEQ_NO) key wins, so a cached/lagging read
    can never overwrite a newer known balance."""
    live = bool(sec_recs) and bool(inv_recs)
    row = parsers.latest_cash_row(sec_recs or [], inv_recs or [])
    scash = skey = None
    if isinstance(stored, dict) and stored.get("cash") is not None:
        scash = stored["cash"]
        k = stored.get("key")
        skey = (str(k[0]), int(k[1])) if isinstance(k, list) and len(k) == 2 else None
    if row is not None:
        key, cash = row
        rec = {"cash": cash, "key": [key[0], key[1]]}
        if skey is not None:
            if key >= skey:
                return cash, live, (rec if key > skey else None)
            return scash, False, None          # disk knows a newer balance
        if live or scash is None:              # legacy keyless file: trust a live read
            return cash, live, rec
    if scash is not None:
        return scash, False, None
    return None, False, None


def _resolve_cash(client: PayPayClient, sec_recs, inv_recs):
    """現金 from the shared-pool ledgers with a persistent fallback for when a
    ledger throttles to empty. Returns (yen, fresh) — fresh=False means the number
    is not a complete live read (stale disk value or one ledger missing); None =
    nothing on record yet, so callers must NOT silently treat it as ¥0."""
    path = client.session_file.parent / "last_cash.json"
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        stored = None
    cash, fresh, to_store = _pick_cash(stored, sec_recs, inv_recs)
    if to_store is not None:
        try:
            snapshots.atomic_write_text(path, json.dumps(to_store))
        except OSError:
            pass
    return cash, fresh


def _cash_source(cash, fresh) -> str:
    return "live" if fresh else ("stale" if cash is not None else "failed")


def _holding_source(fetched, rows) -> str:
    """ok / partial (a row whose valuation didn't parse) / failed (no fetch)."""
    if fetched is None:
        return "failed"
    return "partial" if any(v is None for v in rows) else "ok"


_SOURCE_RANK = {"failed": 3, "partial": 2, "stale": 1, "missing": 1}


def _merge_sources(dicts) -> dict:
    """Per-key worst status across accounts: failed > partial > stale > ok/live."""
    out: dict = {}
    for d in dicts:
        for k, v in (d or {}).items():
            if k not in out or _SOURCE_RANK.get(v, 0) > _SOURCE_RANK.get(out[k], 0):
                out[k] = v
    return out


def _expected_phrase(req) -> str:
    return f"{req.side.value.upper()} {req.symbol} {req.amount_jpy}"


def _order_terms(req, preview=None) -> str:
    """One honest line describing what the web flow actually sends."""
    pv = preview or {}
    if pv.get("preorder"):
        when = (f"executes at the next session's quote ({pv['execute_date']})"
                if pv.get("execute_date") else "executes at the next session's quote")
        fill = f"— 予約注文 (成行予約; market closed now): {when}, no limit price"
    else:
        fill = "— executed at the current quote (成行相当/按现价; no limit price)"
    return (f"{req.side.value.upper()} {req.symbol}  金額指定 ¥{req.amount_jpy:,}  "
            f"口座: {_orders.account_type_name(req.account_type)}  {fill}")


def make_confirmer(reader=input):
    """Return confirmer(req, preview) -> bool. Requires the user to type the exact
    '<SIDE> <SYMBOL> <amount>' phrase — defends against a reflexive Enter."""
    def confirmer(req, preview) -> bool:
        want = _expected_phrase(req)
        total = (preview or {}).get("total_jpy")
        total_frag = f"\n  server amount: ¥{total:,}" if total is not None else ""
        print(f"\n⚠ LIVE ORDER — {_order_terms(req, preview)}{total_frag}")
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


def cmd_passkey_setup(_unused, args) -> int:
    """One-time: extract the PayPay passkey from Bitwarden (forked `rbw fido2 get`)
    and cache it in the macOS Keychain so headless login can sign locally. The
    private key is parsed in-process and never printed."""
    from . import passkey_login as pk
    account = getattr(args, "account", None) or config.DEFAULT_ACCOUNT
    try:
        info = pk.setup(account, rp_id=args.rp_id, rbw_bin=args.rbw_bin)
    except pk.PasskeyLoginError as e:
        print(f"error: passkey setup failed — {e}", file=sys.stderr)
        print("       (unlock the vault first: `bitwarden-use unlock`, and make sure "
              "bitwarden-use with `fido2` support is on PATH or pass --rbw-bin)", file=sys.stderr)
        return 1
    _emit(info, args.json, lambda o: print(
        f"✅ passkey cached in Keychain for account '{account}'\n"
        f"   credentialId: {o['credential_id']}\n   rpId: {o['rp_id']}\n"
        f"   (private key stored, not displayed) — `paypay login` is now headless"))
    return 0


def cmd_fees(client: PayPayClient, args) -> int:
    txns = parsers.parse_transactions(client.settlement_records(max_pages=_pages(args, 3)))
    result = costs.compute_costs(txns, _fx_series_for(txns))
    # same measured-cost breakdown as `review` (costs.measured_cost): the 投信
    # 譲渡益税 / 送金手数料 are a breakdown of the cash-ledger fees, not extra cost.
    try:
        inv = report.aggregate_invtrust(parsers.parse_invtrust_transactions(
            client.invtrust_settlement_records(max_pages=max(_pages(args, 3), 30))))
        inv_tax, inv_transfer = inv["capital_gains_tax"], inv["transfer_fees"]
    except Exception:  # noqa: BLE001 — breakdown is optional; total doesn't need it
        inv_tax = inv_transfer = None
    mc = costs.measured_cost(result["explicit_fees"], result["fx_spread_cost"], inv_tax, inv_transfer)
    result = {**result, "measured_total": mc["total_cost"],
              "inv_capital_gains_tax": inv_tax, "inv_transfer_fees": inv_transfer,
              "securities_fee_residual": mc["securities_fee_residual"] if inv_tax is not None else None,
              "cost_reconciles": mc["cost_reconciles"] if inv_tax is not None else None}

    pps_pct = getattr(args, "price_spread_pct", 0.0) or 0.0
    pps = None
    if pps_pct and result["usd_notional"] and result["fx_rows"]:
        avg_mid = sum(r["market_mid"] for r in result["fx_rows"]) / len(result["fx_rows"])
        pps = costs.price_spread_estimate(result["usd_notional"], avg_mid, pps_pct)
    payload = {**result, "price_spread_pct": pps_pct, "price_spread_estimate": pps}

    def render(p):
        print("PayPay証券 — cost analysis (read-only)\n")
        print(f"  手数料/税 (explicit)    : {_yen(p['explicit_fees'])}")
        if p["inv_capital_gains_tax"] is not None:
            cmark = "" if p["cost_reconciles"] else "  ⚠現金ledger不足(--allで再取得)"
            print(f"     (うち 投信譲渡益税 {_yen(p['inv_capital_gains_tax'])} / 送金手数料 "
                  f"{_yen(p['inv_transfer_fees'])} / 証券手数料 {_yen(p['securities_fee_residual'])}){cmark}")
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


def _fetch_account(client: PayPayClient, ledger_pages: int = 1, inv_ledger_pages: int = 1) -> dict:
    """THE concurrent per-account fetch behind review/snapshot/diff/assets/risk:
    証券 holdings (usa + japan) + 投信 holdings + both settlement ledgers (which
    also yield the shared-pool cash). Degrades per source — a failed feed is
    marked 'failed' (a row with an unparsed valuation → 'partial'), never a crash."""
    client.ensure_session()
    tasks = {
        "ledger": lambda: client.settlement_records(max_pages=ledger_pages),
        "invledger": lambda: client.invtrust_settlement_records(max_pages=inv_ledger_pages),
        "sec_usa": lambda: parsers.parse_holdings(client.brands_html("usa")),
        "sec_japan": lambda: parsers.parse_holdings(client.brands_html("japan")),
        "inv": lambda: parsers.parse_invtrust(client.invtrust_top()),
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

    inv = out.get("inv")
    names = out.get("names") or {}
    rows = []
    if inv:
        for h in inv.holdings:
            rows.append({"category": "投信", "name": names.get(str(h["brand_id"])) or f"#{h['brand_id']}",
                         "valuation": h["valuation"], "unrealized_pl": h["unrealized_pl"],
                         "account_types": []})   # 投信 口座別 needs the ledger (see _invtrust_lots)
    sec = [h for mkt in ("sec_usa", "sec_japan") for h in (out.get(mkt) or []) if not h.is_cash]
    for h in sec:
        rows.append({"category": "証券", "name": h.name,
                     "valuation": h.valuation, "unrealized_pl": h.unrealized_pl,
                     "account_types": list(h.account_types or [])})
    ledger, invledger = out.get("ledger") or [], out.get("invledger") or []
    cash, cash_fresh = _resolve_cash(client, ledger, invledger)
    sec_src = _holding_source(out.get("sec_usa"), [h.valuation for h in sec])
    if sec_src == "ok" and out.get("sec_japan") is None:
        sec_src = "partial"                    # 米国株 read, 日本株 page didn't
    sources = {
        "securities": sec_src,
        "invtrust": _holding_source(inv, [h["valuation"] for h in (inv.holdings if inv else [])]),
        "ledger": "ok" if ledger else "failed",
        "invledger": "ok" if invledger else "failed",
        "cash": _cash_source(cash, cash_fresh),
    }
    return {"rows": rows, "cash": cash, "cash_fresh": cash_fresh,
            "sell_pending": inv.sell_order_pending if inv else None,
            "ledger": ledger, "invledger": invledger, "sources": sources}


_PAGE_SIZE = 20   # settlement feeds return 20 rows per PAGE_NUM step


def _gather(client: PayPayClient, pages: int = 8) -> dict:
    """Fetch everything a review/snapshot needs (see _fetch_account)."""
    # 投信 ledger is a separate, churnier feed (≈14 pages); fetch generously —
    # it stops early on NEXT_FLG, so the cap is just an upper bound.
    inv_pages = max(pages, 30)
    a = _fetch_account(client, pages, inv_pages)
    ledger, invledger = a["ledger"], a["invledger"]
    # 証券 first (stable order for equal valuations, as before)
    holdings = [{"name": r["name"], "category": r["category"], "valuation": r["valuation"] or 0,
                 "unrealized_pl": r["unrealized_pl"]}
                for r in sorted(a["rows"], key=lambda r: r["category"] != "証券")]
    cash = a["cash"]
    total = sum(h["valuation"] for h in holdings) + (cash or 0)
    # both ledgers returned AND neither hit its page cap → cumulative figures
    # (deposits / realized) cover the whole account history
    ledger_complete = (bool(ledger) and bool(invledger)
                       and len(ledger) < pages * _PAGE_SIZE
                       and len(invledger) < inv_pages * _PAGE_SIZE)
    return {"txns": parsers.parse_transactions(ledger),
            "inv_txns": parsers.parse_invtrust_transactions(invledger),
            "holdings": holdings, "cash": cash or 0, "cash_fresh": a["cash_fresh"],
            "total": total, "sources": a["sources"], "ledger_complete": ledger_complete}


def _fx_series_for(txns) -> dict:
    tdates = [t["date"] for t in txns if t["type"] in costs.TRADE_TYPES and t["date"]]
    return market.usdjpy_series(min(tdates), max(tdates)) if tdates else {}


def cmd_review(client: PayPayClient, args) -> int:
    g = _gather(client, pages=_pages(args, 8))
    agg = report.aggregate_trades(g["txns"])
    inv = report.aggregate_invtrust(g.get("inv_txns") or [])
    fx_series = _fx_series_for(g["txns"])
    g["sources"]["fx"] = "ok" if fx_series else "missing"
    fx_cost = costs.compute_costs(g["txns"], fx_series)["fx_spread_cost"]
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
            print("       ※年率換算。様本期間が短い間は変動が大きく出ます(長期収益力ではない)")

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
                 + ("" if p.get("fetched_all") else " (※--allで正確)")
                 + " ※年率換算·様本期短だと変動大"] if p.get("xirr") is not None else []),
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
        print("\nuse:  paypay <command> -a <name>   (or set PAYPAY_ACCOUNT)   "
              "—  -a all consolidates (total/plans/tax)")

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
    """Compute the account's headline numbers for a snapshot (review-level).

    Always scans the FULL ledger history: net_deposit / realized_total are
    cumulative, and a capped window lets old 入金 fall out between snapshots,
    which the calendar would then book as profit/loss. `ledger_full` records
    whether that full scan actually completed (calendar trusts only full↔full)."""
    g = _gather(client, pages=_ALL_PAGES_CAP)
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
        "ledger_full": bool(g.get("ledger_complete")),
    }


def cmd_snapshot(client: PayPayClient, args) -> int:
    """Save / list account snapshots — the CLI's own long-term time series."""
    account = getattr(args, "account", None)
    if getattr(args, "snap_cmd", "save") == "list":
        rows = [{k: s.get(k) for k in ("ts", "as_of", "grand_total", "cash", "realized_total")}
                for s in snapshots.load_all(account)]
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


def _calendar_payload(per_account: dict) -> dict:
    """Assemble the daily P&L calendar schema from {label: [snapshots]}.
    Pure: snapshot lists in → calendar JSON out (see report.daily_pnl_series)."""
    accounts = list(per_account.keys())
    series = {lab: report.daily_pnl_series(snaps) for lab, snaps in per_account.items()}
    all_dates = sorted({d for s in series.values() for d in s})
    days = []
    for d in all_dates:
        accts = {lab: series[lab][d] for lab in accounts if d in series[lab]}
        if accts:
            days.append({"date": d, "accounts": accts})
    return {"schema_version": SCHEMA_VERSION, "currency": "JPY",
            "generated_at": _now_jst_iso(), "accounts": accounts, "days": days}


def cmd_calendar(client: PayPayClient, args) -> int:
    """Build the 每日涨跌日历 (P&L calendar): per-day mark-to-market gain/loss from
    the saved snapshot series, emitted as the viewer's data schema, and (by default)
    injected into the bundled HTML template → a standalone offline file.

    Accounts: default / -a all → every configured profile (household calendar);
    -a <name> → just that one. Labels come from each profile's PAYPAY_LABEL."""
    from pathlib import Path
    from .client import state_dir as _state_dir

    acct_arg = getattr(args, "account", None)
    profiles = ([acct_arg] if acct_arg and acct_arg != "all"
                else (config.list_accounts() or [config.DEFAULT_ACCOUNT]))
    per: dict[str, list] = {}
    for prof in profiles:
        label = config.account_label(prof)
        if label in per:                       # de-collide duplicate labels
            label = f"{label}({prof})"
        per[label] = snapshots.load_all(prof)

    payload = _calendar_payload(per)

    if getattr(args, "json", False):
        print(_json_dump(payload))
        return 0

    tpl = (Path(__file__).resolve().parent / "calendar_template.html").read_text(encoding="utf-8")
    dumped = json.dumps(payload, ensure_ascii=False, indent=2)
    html, n = re.subn(r"/\* DATA_START \*/.*?/\* DATA_END \*/",
                      lambda m: "/* DATA_START */ " + dumped + " /* DATA_END */",
                      tpl, count=1, flags=re.S)
    if n != 1:
        print("calendar template is missing the DATA_START/DATA_END markers", file=sys.stderr)
        return 1

    out = getattr(args, "out", None)
    out_path = Path(out).expanduser() if out else (_state_dir(None) / "pnl-calendar.html")
    snapshots.atomic_write_text(out_path, html)

    n_days = len(payload["days"])
    print(f"P&L calendar written: {out_path}")
    print(f"  accounts: {', '.join(payload['accounts']) or '(none)'}  ·  {n_days} 天有日度盈亏")
    if n_days == 0:
        print("  ⚠ 还没有日度盈亏 —— 每个账户需要 ≥2 个不同日期的快照。")
        print("    跑 `paypay snapshot save`(或装 bin/snapshot-cron.sh 每日自动存)。")
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
    txns = parsers.parse_invtrust_transactions(client.invtrust_settlement_records(max_pages=_hist_pages(args)))
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
            if pl["months"]:
                body = (f"月額(推) {_yen(pl['monthly_estimate'])} "
                        f"(推算依据: 累計 {_yen(pl['total_invested'])} / {pl['months']}ヶ月)")
            else:
                body = "月額(推) 未推算(尚無買付記録)" + ("" if pl["active"] else " · 設定なし/停止?")
            L.append(f"  - {tag} {pl['name']}: {body}")
        if p.get("accounts"):
            L.append("- 口座別: " + " / ".join(f"{a}:{_yen(v)}/月" for a, v in p["accounts"].items()))
        L.append(f"\n> {p.get('note','')}")
        print("\n".join(L))
        return
    print(f"{head}\n")
    print(f"  月定投 合計(推計): {_yen(p['monthly_total_estimate'])}   (年化 {_yen(p['annualized_total_estimate'])})")
    if p.get("accounts"):
        print("  口座別/月: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
    print("\n  " + _lj("銘柄", 32) + _lj("定投", 6) + _rj("月額(推)", 12)
          + _rj("年化", 12) + _rj("月数", 6) + _rj("累計投入", 12))
    for pl in p["plans"]:
        if pl["months"]:
            me, an, mo, ti = (_yen(pl["monthly_estimate"]), _yen(pl["annualized"]),
                              str(pl["months"]), _yen(pl["total_invested"]))
        else:
            me, an, mo, ti = "未推算", "—", "0", "—"
        print("  " + _lj(pl["name"] or "", 32) + _lj("📅" if pl["active"] else "—", 6)
              + _rj(me, 12) + _rj(an, 12) + _rj(mo, 6) + _rj(ti, 12))
    print("\n  「未推算」= まだ買付記録がなく月額を推算できない(active だが初回約定待ち)")
    print(f"  注: {p.get('note','')}")


def cmd_tax(client: PayPayClient, args) -> int:
    """Per calendar-year tax view: 売却 proceeds, 譲渡益税 withheld, 分配金 — the
    figures the 年間取引報告書 shows. FACTS only (NISA is tax-free; 特定 is withheld)."""
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_tax, _tax_payload, merge=_merge_tax)
    payload = {"as_of": _now_jst_str(), **_tax_payload(client, args),
               "note": "参考値のみ・非正式。確定申告は PayPay 証券交付の年間取引報告書で確認。NISA非課税/特定源泉徴収。"}
    _emit_fmt(payload, _fmt(args), lambda p: _render_tax(p, "table"),
              lambda p: _render_tax(p, "lark"))
    return 0


def _tax_payload(client: PayPayClient, args) -> dict:
    sec = parsers.parse_transactions(client.settlement_records(max_pages=_hist_pages(args)))
    inv = parsers.parse_invtrust_transactions(client.invtrust_settlement_records(max_pages=_hist_pages(args)))
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
    """Yield (account_name, client) for every configured profile (for -a all).
    Each account is loaded STRICTLY from its own .env (from_account_file) so creds
    can't cross-contaminate via os.environ within this single process."""
    for a in config.list_accounts():
        try:
            yield a, PayPayClient(Settings.from_account_file(a),
                                  cache_ttl=0 if getattr(args, "no_cache", False) else None)
        except Exception:  # noqa: BLE001 — skip a profile that won't load
            yield a, None


def _all_accounts(args, render, payload_fn, merge) -> int:
    """Run payload_fn per account, merge, and emit. Shared by -a all commands."""
    per = {}
    for name, client in _account_clients(args):
        if client is None:
            per[name] = {"_error": "ProfileLoadError"}
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
                                                 "monthly_estimate": None, "annualized": None,
                                                 "months": 0, "total_invested": 0, "last_date": None})
            cur["active"] = cur["active"] or r["active"]
            if r.get("months"):  # only funds with real buys contribute a run-rate
                cur["monthly_estimate"] = (cur["monthly_estimate"] or 0) + (r.get("monthly_estimate") or 0)
                cur["annualized"] = (cur["annualized"] or 0) + (r.get("annualized") or 0)
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
            "note": "全口座合算・参考値のみ・非正式。確定申告は PayPay 証券交付の年間取引報告書で確認。"}


def _total_payload(client: PayPayClient, args) -> dict:
    sec_by_market, errors = {}, []
    for mkt in ("usa", "japan"):
        try:
            s = parsers.parse_summary(client.portfolio_html(mkt))
            sec_by_market[mkt] = s.total_valuation   # None = page read, value didn't parse
            if s.total_valuation is None:
                errors.append(f"{mkt}: valuation unparsed")
        except (requests.HTTPError, requests.RequestException) as e:
            sec_by_market[mkt] = None
            errors.append(f"{mkt}: {type(e).__name__}")
    sec_total = sum(v for v in sec_by_market.values() if v)
    try:
        inv = parsers.parse_invtrust(client.invtrust_top())
    except Exception as e:  # noqa: BLE001 — degrade like the other sources
        inv = None
        errors.append(f"invtrust: {type(e).__name__}")
    inv_val = inv.valuation if inv else None
    invested = sec_total + (inv_val or 0)
    # shared cash pool: newest balance across 証券 + 投信 ledgers (page-1 of each is
    # enough — both are newest-first) so an 投信 buy doesn't leave 証券 cash stale.
    ledgers = []
    for fetch in (client.settlement_records, client.invtrust_settlement_records):
        try:
            ledgers.append(fetch(max_pages=1))
        except Exception:  # noqa: BLE001
            ledgers.append([])
    cash, cash_fresh = _resolve_cash(client, *ledgers)
    if not cash_fresh:
        errors.append("cash: throttled→stale")
    fetched = [v for v in sec_by_market.values() if v is not None]
    return {
        "securities_by_market": sec_by_market, "securities_total": sec_total,
        "invtrust_valuation": inv_val, "invested_total": invested,
        "cash": cash, "cash_fresh": cash_fresh, "grand_total": invested + (cash or 0),
        "invtrust_sell_pending": inv.sell_order_pending if inv else None, "errors": errors,
        "sources": {
            "securities": ("failed" if not fetched
                           else "ok" if len(fetched) == len(sec_by_market) else "partial"),
            "invtrust": "ok" if inv_val is not None else "failed",
            "cash": _cash_source(cash, cash_fresh)},
        "note": ("grand_total = 証券 + 投信 holdings + cash (newest balance across the "
                 "証券+投信 shared pool), matching the app's 保有資産 total. CFD "
                 "(separate login) is not included."),
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


def _acct_sources(per: dict) -> list:
    """Each account's sources for _merge_sources; an account that errored out
    entirely counts as one failed source (so the merge never looks complete)."""
    return [{f"account:{a}": "failed"} if p.get("_error") else (p.get("sources") or {})
            for a, p in per.items()]


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
        errors += [f"{acct}: {e}" for e in p.get("errors") or []]
    out.update({"accounts": accounts, "errors": errors,
                "cash_fresh": all(p.get("cash_fresh", False) for p in per.values()),
                "sources": _merge_sources(_acct_sources(per)),
                "note": "全口座合算。CFD は別ログイン未集計。"})
    return out


def cmd_total(client: PayPayClient, args) -> int:
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_total, _total_payload, merge=_merge_total)
    payload = {"as_of": _now_jst_str(), **_total_payload(client, args)}
    _emit_fmt(payload, _fmt(args), lambda p: _render_total(p, "table"),
              lambda p: _render_total(p, "lark"))
    return 0


def _consolidated_holdings(client: PayPayClient):
    """証券 + 投信 holdings + shared-pool cash (page 1 of each ledger), via
    _fetch_account. Returns (rows, cash, cash_fresh, invtrust_sell_pending, sources)."""
    a = _fetch_account(client, 1, 1)
    sources = {k: a["sources"][k] for k in ("securities", "invtrust", "cash")}
    return a["rows"], a["cash"], a["cash_fresh"], a["sell_pending"], sources


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


def _account_raw(client: PayPayClient, args) -> dict:
    """Per-account materials for assets/risk (also the -a all merge input)."""
    rows, cash, cash_fresh, sell_pending, sources = _consolidated_holdings(client)
    raw = {"rows": rows, "cash": cash, "cash_fresh": cash_fresh,
           "sell_pending": sell_pending, "sources": sources}
    if getattr(args, "accounts", False):
        try:
            lots, _, _ = _invtrust_lots(client, _pages(args, 30))
        except Exception:  # noqa: BLE001
            lots = None
        raw["lots"] = lots if lots is not None else []
    return raw



def _merge_holdings(per: dict) -> dict:
    """Combine per-account raws (same fund summed) for -a all. Sources merge per
    key by worst status; cash_fresh only if every account's cash was live."""
    combined, lots, accts = {}, [], {}
    cash = sell = 0
    want_accounts = False
    for acct, raw in per.items():
        if raw.get("_error"):
            continue
        cash += raw.get("cash") or 0
        sell += raw.get("sell_pending") or 0
        if "lots" in raw:
            want_accounts = True
            lots += raw.get("lots") or []
        acct_total = raw.get("cash") or 0
        for r in raw.get("rows", []):
            acct_total += r.get("valuation") or 0
            key = (r["name"], r["category"])
            cur = combined.setdefault(key, {"name": r["name"], "category": r["category"],
                                            "valuation": 0, "unrealized_pl": 0, "account_types": []})
            cur["valuation"] += r.get("valuation") or 0
            cur["unrealized_pl"] = (cur["unrealized_pl"] or 0) + (r.get("unrealized_pl") or 0)
            cur["account_types"] = sorted(set(cur["account_types"]) | set(r.get("account_types") or []))
        accts[acct] = acct_total
    return {"rows": list(combined.values()), "cash": cash, "sell_pending": sell or None,
            "cash_fresh": all(r.get("cash_fresh", False) for r in per.values()),
            "sources": _merge_sources(_acct_sources(per)),
            "lots": lots, "want_accounts": want_accounts, "accounts": accts}


def _build_risk(rows, cash, sell_pending, sources, lots, want_accounts) -> dict:
    payload = _risk_payload(rows, cash, sell_pending, sources)
    if want_accounts:
        payload["by_account_pct"] = _account_split(rows, lots or [], cash, payload["grand_total"])
    return payload


def _merge_risk(per: dict) -> dict:
    """Combine holdings across accounts (same fund summed) → household concentration."""
    m = _merge_holdings(per)
    p = _build_risk(m["rows"], m["cash"], m["sell_pending"], m["sources"], m["lots"], m["want_accounts"])
    p["accounts"] = m["accounts"]
    p["note"] = "全口座合算。" + p["note"]
    return p


def _render_risk(p: dict, fmt: str) -> None:
    lg = p["largest_position"]
    allp = "(全口座)" if p.get("accounts") else ""
    if fmt == "lark":
        L = [f"**PayPay証券 持仓结构{allp} (事実のみ)**", "",
             f"- 総資産 **{_yen(p['grand_total'])}** (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])} = {p['cash_pct']}%)",
             f"- 最大单一持仓: **{lg['name'] if lg else '—'} {lg['weight_pct'] if lg else 0}%**",
             f"- 集中度: top1 {p['top1_pct']}% / top3 {p['top3_pct']}% / top5 {p['top5_pct']}%",
             f"- 米国 計価(証券): {p['usd_asset_pct']}% / 米国株 底層暴露: **{p['us_underlying_pct']}%** (S&P500等の投信も実質米株)",
             f"- 种类构成: " + " / ".join(f"{k} {v}%" for k, v in p["by_kind_pct"].items()),
             f"- 账户构成: " + " / ".join(f"{k} {v}%" for k, v in p["by_category_pct"].items())]
        if p.get("by_account_pct"):
            L.append("- 口座区分: " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        if p.get("accounts"):
            L.append("- 口座別総額: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
        L += ["- 持仓ウェイト:"]
        for pos in p["positions"]:
            L.append(f"  - {pos['name']} ({pos['kind']}): {_yen(pos['valuation'])} / {pos['weight_pct']}%")
        if p["cash"]:
            L.append(f"  - 現金: {_yen(p['cash'])} / {p['cash_pct']}%")
        _freshness_block(p, L)
        L.append(f"\n> {p['note']}")
        print("\n".join(L))
        return
    print(f"PayPay証券 — 持仓结构 / exposure{allp} (事実のみ)\n")
    print(f"  総資産 {_yen(p['grand_total'])}  (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])} = {p['cash_pct']}%)")
    print(f"  最大单一持仓 : {lg['name'] if lg else '—'} {lg['weight_pct'] if lg else 0}%")
    print(f"  集中度       : top1 {p['top1_pct']}% / top3 {p['top3_pct']}% / top5 {p['top5_pct']}%")
    print(f"  米国 計価(証券) : {p['usd_asset_pct']}%   (USD建で値付け)")
    print(f"  米国株 底層暴露 : {p['us_underlying_pct']}%   (S&P500等の投信も含む実質米株)")
    print(f"  种类构成     : " + " / ".join(f"{k} {v}%" for k, v in p["by_kind_pct"].items()))
    print(f"  账户构成     : " + " / ".join(f"{k} {v}%" for k, v in p["by_category_pct"].items()))
    if p.get("by_account_pct"):
        print(f"  口座区分     : " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
    if p.get("accounts"):
        print(f"  口座別総額   : " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
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


def cmd_risk(client: PayPayClient, args) -> int:
    """Structural exposure of the account — FACTS ONLY (weights, concentration,
    category/currency split). No risk verdicts, no buy/sell advice."""
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_risk, _account_raw, merge=_merge_risk)
    raw = _account_raw(client, args)
    payload = {"as_of": _now_jst_str(),
               **_build_risk(raw["rows"], raw["cash"], raw["sell_pending"], raw["sources"],
                             raw.get("lots"), getattr(args, "accounts", False))}
    _emit_fmt(payload, _fmt(args), lambda p: _render_risk(p, "table"),
              lambda p: _render_risk(p, "lark"))
    return 0


def _build_assets(rows, cash, cash_fresh, sell_pending, sources, lots, want_accounts, accounts=None) -> dict:
    invested = sum(r.get("valuation") or 0 for r in rows)
    p = {"holdings": rows, "invested_total": invested, "cash": cash, "cash_fresh": cash_fresh,
         "grand_total": invested + (cash or 0), "invtrust_sell_pending": sell_pending,
         "sources": sources,
         "note": "grand_total = invested holdings + cash (newest balance across the "
                 "証券+投信 shared pool), matching the app's 保有資産 total. CFD "
                 "(separate login) is not included."}
    if want_accounts:
        p["by_account_pct"] = _account_split(rows, lots or [], cash, p["grand_total"])
    if accounts is not None:
        p["accounts"] = accounts
        p["note"] = "全口座合算。" + p["note"]
    return p


def _merge_assets(per: dict) -> dict:
    m = _merge_holdings(per)
    return _build_assets(m["rows"], m["cash"], m["cash_fresh"], m["sell_pending"],
                         m["sources"], m["lots"], m["want_accounts"], accounts=m["accounts"])


def _render_assets(p: dict, fmt: str) -> None:
    # Weights are over the GRAND TOTAL (incl. cash) to match the app's 資産割合 donut.
    denom = p["grand_total"] or 0
    allp = "(全口座)" if p.get("accounts") else ""
    if fmt == "lark":
        L = [f"**PayPay証券 资产总览{allp}**", "",
             f"- 総資産: **{_yen(p['grand_total'])}** (投資 {_yen(p['invested_total'])} + 現金 {_yen(p['cash'])})"]
        for r in sorted(p["holdings"], key=lambda x: -(x["valuation"] or 0)):
            wt = f"{100 * (r['valuation'] or 0) / denom:.1f}%" if denom else "—"
            L.append(f"  - [{r['category']}] {r['name']}: {_yen(r['valuation'])} ({wt}) {_signed_yen(r['unrealized_pl'])}")
        if p["cash"]:
            cw = f"{100 * p['cash'] / denom:.1f}%" if denom else "—"
            L.append(f"  - [現金] buyable: {_yen(p['cash'])} ({cw})")
        if p.get("by_account_pct"):
            L.append("- 口座区分: " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
        if p.get("accounts"):
            L.append("- 口座別総額: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
        L.append(f"- 投信 売却申込中: {_yen(p['invtrust_sell_pending'])}")
        _freshness_block(p, L)
        L.append("\n> CFD(別ログイン)は未集計")
        print("\n".join(L))
        return
    print(f"PayPay証券 — consolidated assets{allp} (read-only)\n")
    print(_lj("CATEGORY", 9) + _lj("NAME", 34) + _rj("VALUATION", 12) + _rj("WEIGHT", 8) + _rj("P&L", 10))
    print("-" * 73)
    for r in sorted(p["holdings"], key=lambda x: -(x["valuation"] or 0)):
        wt = f"{100 * (r['valuation'] or 0) / denom:.1f}%" if denom else "—"
        print(_lj(r["category"], 9) + _lj(r["name"] or "", 34)
              + _rj(_yen(r["valuation"]), 12) + _rj(wt, 8) + _rj(_yen(r["unrealized_pl"]), 10))
    if p["cash"]:
        cw = f"{100 * p['cash'] / denom:.1f}%" if denom else "—"
        print(_lj("現金", 9) + _lj("(buyable cash)", 34) + _rj(_yen(p["cash"]), 12) + _rj(cw, 8) + _rj("—", 10))
    print("-" * 73)
    cstale = "" if p.get("cash_fresh", True) else "  ⚠stale(取得失敗)"
    print(f"  投資資産 : {_yen(p['invested_total'])}")
    print(f"  現金     : {_yen(p['cash'])}{cstale}")
    print(f"  総資産合計: {_yen(p['grand_total'])}")
    if p.get("by_account_pct"):
        print("  口座区分 : " + " / ".join(f"{k} {v}%" for k, v in p["by_account_pct"].items()))
    if p.get("accounts"):
        print("  口座別総額: " + " / ".join(f"{a} {_yen(v)}" for a, v in p["accounts"].items()))
    print(f"\n  投信 売却申込中 (settling): {_yen(p['invtrust_sell_pending'])}")
    print("  注: CFD(別ログイン)は未集計。")
    fl = []
    _freshness_block(p, fl)
    if fl:
        print()
        for line in fl:
            print(line)


def cmd_assets(client: PayPayClient, args) -> int:
    """Consolidated view: 証券 + 投信 holdings + securities cash. `-a all` merges
    every account (same fund summed) into a household view + per-account totals."""
    if getattr(args, "account", None) == "all":
        return _all_accounts(args, _render_assets, _account_raw, merge=_merge_assets)
    raw = _account_raw(client, args)
    payload = {"as_of": _now_jst_str(),
               **_build_assets(raw["rows"], raw["cash"], raw["cash_fresh"], raw["sell_pending"],
                               raw["sources"], raw.get("lots"), getattr(args, "accounts", False))}
    _emit_fmt(payload, _fmt(args), lambda p: _render_assets(p, "table"),
              lambda p: _render_assets(p, "lark"))
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
        # moving-average cost of the 口 still held — buy−sell (net_invested) is wrong
        # after a partial sell, as it leaves the sold lots' gain/loss in the "cost"
        cost = b["remaining_cost"]
        holdings.append({"brand": b["brand"], "acct": b["acct"], "net_shares": b["net_shares"],
                         "cost": cost, "valuation": val, "unrealized_pl": val - cost})
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


def _stdin_is_tty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _refuse_non_tty() -> bool:
    """--execute is for a human at a keyboard only. True (and an error printed) if
    stdin is not a terminal — an agent / pipe / cron can never place an order."""
    if _stdin_is_tty():
        return False
    print("error: --execute requires an interactive terminal (stdin is not a TTY). "
          "Live orders are human-only; run it yourself in a terminal.", file=sys.stderr)
    return True


def _portfolio_total_jpy(client):
    """保有資産 total (holdings valuation + cash) the same way `assets` computes it;
    None if any source failed — the max_pct guard then fails closed."""
    try:
        rows, cash, _fresh, _sell, sources = _consolidated_holdings(client)
    except Exception:  # noqa: BLE001 — unknown total -> guard blocks
        return None
    if cash is None or any(s == "failed" for s in (sources or {}).values()):
        return None
    return sum(r.get("valuation") or 0 for r in rows) + cash


def _guard_ctx(client, args) -> "_guards.GuardContext":
    acct = getattr(args, "account", None)
    return _guards.GuardContext(
        now=datetime.now(timezone.utc),
        today_order_count=_audit.count_today(account=acct, kind=_audit.CAP_KINDS),
        portfolio_total_jpy=_portfolio_total_jpy(client),
    )


def _order_common(client, args, side: str) -> int:
    acct = getattr(args, "account", None)
    execute = getattr(args, "execute", False)
    try:
        req = _orders.build(market=args.market, symbol=args.symbol, side=side,
                            amount_jpy=args.amount, account=acct,
                            account_type=getattr(args, "account_type", 2))
    except _orders.OrderError as e:
        print(f"error: {e}", file=sys.stderr); return 2
    if execute and _refuse_non_tty():
        return 2
    cfg = _guards.load_trade_config(acct)
    ctx = _guard_ctx(client, args)
    confirm = lambda r: client.order_confirm(r)
    base = {"symbol": req.symbol, "side": side, "amount_jpy": req.amount_jpy,
            "amount_type": "金額指定", "account_type": req.account_type}

    def audit(event: dict) -> None:
        ok = _audit.record({**base, **event}, account=acct)
        if not ok and event.get("kind") in _audit.CAP_KINDS:
            print("\n!!! WARNING: the order audit log could NOT be written — this live "
                  "order is NOT counted toward daily_order_cap. Check `paypay orders` "
                  "and fix ~/.paypay-sec permissions before placing more. !!!",
                  file=sys.stderr)

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
        if execute:
            res = _orders.place(req, cfg, ctx, confirm=confirm,
                                submit=lambda tok, r: client.order_submit(tok, r, trade_password=_trade_pw()),
                                confirmer=make_confirmer())
        else:
            res = _orders.dry_run(req, cfg, ctx, confirm=confirm)
    except NotImplementedError as e:
        print(f"[pending Phase 0] guards OK, but {e}", file=sys.stderr)
        audit({"kind": "dry_run", "status": "confirm_pending_phase0"})
        return 3
    except (RuntimeError, requests.RequestException) as e:
        # confirm rejected (e.g. market closed / over buyable), session, or submit rejected
        print(f"error: {e}", file=sys.stderr)
        audit({"kind": "error", "error": str(e)[:200]})
        return 1

    if res.outcome_unknown:
        kind = "submit_unknown"
    elif res.submitted:
        kind = "submit"
    elif res.aborted_reason:
        kind = "aborted"
    else:
        kind = "dry_run"
    audit({"kind": kind, "violations": res.violations, "order_id": res.order_id,
           "preorder": bool((res.preview or {}).get("preorder")), "reason": res.aborted_reason})

    def render(d):
        # d is res.__dict__ (a plain dict — _emit also json-serializes it)
        if d.get("violations"):
            print("⛔ order blocked by guards:")
            for v in d["violations"]:
                print(f"   - {v}")
            return
        if d.get("outcome_unknown"):
            print(f"❓ outcome UNKNOWN — {_order_terms(req, d.get('preview'))}")
            print(f"   {d.get('aborted_reason')}")
            print("   The order may or may not have been placed. Check `paypay orders` "
                  "before retrying.")
            return
        if d.get("aborted_reason"):
            print(f"ABORTED: {d['aborted_reason']}  (nothing was sent)")
            return
        pv = d.get("preview") or {}
        print(f"{'✅ SUBMITTED' if d.get('submitted') else '🔎 DRY-RUN (not sent)'}  {_order_terms(req, pv)}")
        if pv.get("total_jpy") is not None:
            print(f"   server amount: ¥{pv['total_jpy']:,}   quote: {pv.get('est_price')}   fee: ¥{pv.get('fee_jpy', 0):,}")
        if d.get("submitted"):
            if d.get("order_id"):
                print(f"   order no.  : {d['order_id']}")
            else:
                print("   submitted; order number not returned — verify with `paypay orders`")
        elif not execute:
            print("   (re-run with --execute in a terminal to place; you will be asked to type a confirmation)")

    _emit(res.__dict__, getattr(args, "json", False), render)
    if res.outcome_unknown or res.aborted_reason or res.violations:
        return 1
    return 0


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
    if _refuse_non_tty():
        return 2
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
    from . import __version__
    p.add_argument("--version", action="version", version=f"paypay {__version__}")
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
            sp.add_argument("--fast", action="store_true",
                            help="quick look (default scans the FULL ledger history for accuracy)")
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

    pks = sub.add_parser("passkey-setup", parents=[common]); pks.set_defaults(func=cmd_passkey_setup)
    pks.add_argument("--rp-id", default="paypay-sec.co.jp",
                     help="passkey rpId / entry selector in Bitwarden (default paypay-sec.co.jp)")
    pks.add_argument("--rbw-bin", default="bitwarden-use",
                     help="path to the bitwarden-use binary with `fido2` support (default: bitwarden-use on PATH)")

    snap = sub.add_parser("snapshot", parents=[common]); snap.set_defaults(func=cmd_snapshot)
    snap.add_argument("snap_cmd", nargs="?", choices=("save", "list"), default="save",
                      help="save (default) a snapshot of the account, or list saved snapshots")
    snap.add_argument("--pages", type=int, default=8,
                      help="ignored — snapshots always scan the full ledger (cumulative deposits)")

    dff = sub.add_parser("diff", parents=[common]); dff.set_defaults(func=cmd_diff)
    dff.add_argument("--days", type=int, default=None,
                     help="compare a live read against the snapshot ~N days old (default: the latest snapshot)")
    dff.add_argument("--since", default=None,
                     help="baseline selector; 'last' = latest snapshot (the default)")
    dff.add_argument("--pages", type=int, default=8,
                     help="ignored — the live read scans the full ledger, like snapshot save")

    cal = sub.add_parser("calendar", parents=[common]); cal.set_defaults(func=cmd_calendar)
    cal.add_argument("--out", default=None,
                     help="output HTML path (default: <state_dir>/pnl-calendar.html). Ignored with --json")

    buy = sub.add_parser("buy", parents=[common]); buy.set_defaults(func=cmd_buy)
    sell = sub.add_parser("sell", parents=[common]); sell.set_defaults(func=cmd_sell)
    for sp_ in (buy, sell):
        sp_.add_argument("symbol", help="ticker, e.g. TSLA")
        sp_.add_argument("--amount", type=int, required=True,
                         help="order amount in JPY (金額指定) — fills at the current quote (成行相当)")
        sp_.add_argument("--account-type", dest="account_type", type=int, choices=(2, 3, 4), default=2,
                         help="brokerage account: 2=特定(cash, default) | 3=成長投資枠NISA | 4=つみたて")
        sp_.add_argument("--execute", action="store_true", help="actually place the order (default is dry-run; human-only, needs a TTY)")

    ords = sub.add_parser("orders", parents=[common]); ords.set_defaults(func=cmd_orders)
    canc = sub.add_parser("cancel", parents=[common]); canc.set_defaults(func=cmd_cancel)
    canc.add_argument("order_id", help="order id from `paypay orders`")
    canc.add_argument("--execute", action="store_true", help="actually cancel (default is dry-run)")

    upd = sub.add_parser("self-update", aliases=["upgrade"],
                         help="update this skill + CLI from GitHub (no login, no account data)")
    upd.set_defaults(func=cmd_self_update)
    upd.add_argument("--check", action="store_true",
                     help="only report installed vs target version; change nothing")
    upd.add_argument("--ref", default="main",
                     help="branch, tag or full commit sha to install (default: main)")
    upd.add_argument("--yes", action="store_true",
                     help="skip the confirmation prompt (required without a TTY)")
    upd.add_argument("--force", action="store_true",
                     help="update even if the installed folder has local edits (keeps a backup)")
    upd.add_argument("--json", action="store_true", help="machine-readable result")

    return p


def cmd_self_update(_client, args) -> int:
    from .selfupdate import cmd_self_update as _run
    return _run(args)


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
    # `self-update` only talks to GitHub + uv — never loads credentials or logs in.
    if args.func is cmd_self_update:
        return cmd_self_update(None, args)
    # `accounts` / `doctor` only inspect local config — no credentials / network.
    if args.func is cmd_accounts:
        return cmd_accounts(None, args)
    if args.func is cmd_doctor:
        return cmd_doctor(None, args)
    # `passkey-setup` only extracts from Bitwarden (via rbw) into the Keychain — no
    # PayPay login/network of its own.
    if args.func is cmd_passkey_setup:
        return cmd_passkey_setup(None, args)
    # `calendar` only reads saved snapshots from disk (no creds/network); it handles
    # account selection (incl. -a all = household) itself.
    if args.func is cmd_calendar:
        return cmd_calendar(None, args)
    # -a all: consolidate across every configured profile (handled inside the cmd).
    if getattr(args, "account", None) == "all":
        if args.func not in (cmd_total, cmd_assets, cmd_risk, cmd_plans, cmd_tax):
            print("error: -a all is supported only for total / assets / risk / plans / tax",
                  file=sys.stderr)
            return 2
        return args.func(None, args)
    try:
        acct = getattr(args, "account", None)
        # A named account (-a <name>) is loaded from ITS file so a shell-exported or
        # previously-loaded default PAYPAY_* can't bleed in; the default account keeps
        # the os.environ path (dev-friendly: PAYPAY_ENV / ./.env overrides).
        settings = (Settings.from_account_file(acct)
                    if acct and acct != config.DEFAULT_ACCOUNT
                    else Settings.from_env(acct))
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
