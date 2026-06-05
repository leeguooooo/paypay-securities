---
name: paypay-securities
description: >-
  CLI & agent skill for a PayPay証券 (PayPay Securities, ペイペイ証券) account:
  check portfolio, holdings, balance, 投資信託, 米国株, 取引履歴 (transaction
  history), fees & FX-spread; generate a 復盘/review with realized & unrealized P&L
  and money-weighted return (XIRR); 持仓结构/exposure (risk), 定投/つみたて recurring-buy
  plans (plans), per-year tax view (tax), account snapshots & diff (snapshot/diff),
  multi-account consolidation (-a all), Chinese output (--lang zh); AND place 米国株
  orders (buy/sell/cancel). Use when the user wants to view, review, analyze, report,
  or trade a PayPay証券 / PayPay investment account. Orders dry-run by default; a live
  order requires an explicit human `--execute` with a typed confirmation + the account
  TRADE_PASSWORD (the agent never auto-submits). Facts only — never investment advice
  or buy/sell judgments.
---

# paypay-securities — read-only PayPay証券 client

A small Python CLI that authenticates to the PayPay証券 web frontend and reads
account data. There is **no official or unofficial API/SDK** for PayPay証券, so
this talks to the same endpoints the website uses.

## Scope

| Phase | Status | What |
|---|---|---|
| 0 — auth | ✅ done | `/login.json` + trusted-device cookie → session token, no SMS |
| 1 — read (証券) | ✅ done | balance, portfolio (per-holding), asset/cash history |
| 1b — read (投信) | ✅ done | mutual-fund valuation/principal/P&L via JSON API; `total` aggregation |
| 2 — export/notify | not built | scheduled pull + push to Lark/WeChat/email |
| 3 — trading (証券 米国株) | ✅ built | buy/sell/cancel via the web order flow — **dry-run by default; live `--execute` is human-only (typed confirm + TRADE_PASSWORD); per-order + daily-count guards** |

**Account is split across systems** (see also the project memory): 証券 (this
site, incl. the cash balance in the transaction ledger), 投信 (`/investment_trust/`
Vue SPA + JSON API, same session), and CFD (`cfd.paypay-sec.co.jp`, separate login
— not integrated). `paypay total`/`assets` cover 証券 + 投信 + cash = the app's
full grand total; only CFD is out of scope.

## Install

```bash
npx skills add leeguooooo/paypay-securities --skill paypay-securities      # project-local
npx skills add leeguooooo/paypay-securities --skill paypay-securities -g   # user-global (~/.claude/skills/)
```
The CLI (the bundled `paypay_sec/` package + `pyproject.toml`) is installed
alongside this SKILL.md. Requires [`uv`](https://docs.astral.sh/uv/) on PATH.

## Setup (credentials)

Put credentials in **`~/.paypay-sec/.env`** (outside the repo / installed skill —
this path is searched first, so the CLI works from any directory). Template in
`.env.example`:
- `PAYPAY_MEMBER_ID`, `PAYPAY_PASSWORD`
- `PAYPAY_COOKIE` — the full Cookie header from a logged-in browser. It **must**
  contain the `..._SMS_AUTH_STRING` trusted-device token, otherwise the server
  demands an SMS code and login fails.

Never commit credentials or pass them on the command line. (`PAYPAY_ENV` overrides
the path; `./.env` / `./spike/.env` are also searched for dev checkouts.)

**Multiple accounts:** each account is a profile. The default account reads
`~/.paypay-sec/.env`; a named account `<name>` reads `~/.paypay-sec/<name>.env`.
Select one with `-a <name>` (or the `PAYPAY_ACCOUNT` env var) — e.g.
`uv run paypay assets -a second`. Each account keeps its OWN session + response
cache (default → `~/.paypay-sec/`, named → `~/.paypay-sec/<name>/`) so they never
collide. `paypay accounts` lists the configured profiles.

## Commands

Run from this skill's directory: `uv run paypay <cmd>` (uv resolves deps from the
bundled `pyproject.toml`; the `paypay` entry point is defined there).

```bash
uv run paypay login                 # force a fresh login (refreshes the cached session)
uv run paypay logout                # clear the cached session
uv run paypay balance               # 証券: 評価額合計 / 投資元本 / 含み損益
uv run paypay portfolio             # 証券 holdings: valuation, shares, cost, P&L, account type
uv run paypay history               # 証券 daily asset/cash time series
uv run paypay invtrust              # 投信 (mutual funds): valuation / principal / P&L / 売却申込中
uv run paypay invtrust-history      # 投信 ledger (MARKET_ID=99): 買付/売却/入金/譲渡益税/送金手数料 + moving-avg realized P&L + 現保有(口座別 NISA/特定)
uv run paypay total                 # aggregate 証券 + 投信 invested assets (excludes cash)
uv run paypay assets                # one-shot consolidated holdings + cash + grand total (parallel)
uv run paypay trades [--pages N]    # 証券 transaction ledger (買付/売却/入金/手数料) + running cash balance
uv run paypay fees [--detail]       # cost analysis: explicit fees + measured FX spread (+ optional price spread)
uv run paypay review                # 复盘: 2 blocks — 持仓盈亏(評価損益=App頭条) + 账户盈亏(通算=総資産−純入金); + realized/costs
uv run paypay trades-summary        # per-brand buy/sell/net-invested/net-shares/realized P&L
uv run paypay risk [--accounts]     # 持仓结构 / exposure: weights, concentration, FX/category split (+口座区分 with --accounts; FACTS ONLY)
uv run paypay plans                 # 定投/つみたて: active recurring-buy funds + monthly run-rate (scans FULL history by default; --fast = quick)
uv run paypay tax                   # per-year tax view: 売却 / 譲渡益税 / 分配金 (FULL history by default; --fast = quick; 年間取引報告書 参考, FACTS ONLY)
uv run paypay snapshot save         # save a dated account snapshot (the CLI's own time series)
uv run paypay snapshot list         # list saved snapshots
uv run paypay diff [--days N]       # diff a live read vs the latest (or ~N-days-old) snapshot
uv run paypay calendar              # build 每日涨跌日历 HTML from snapshots (--json = raw schema; --out PATH; -a all = household, the default)
uv run paypay doctor [--online]     # diagnose setup/login readiness (offline by default; --online probes login + 証券/投信 to confirm the session is live)
uv run paypay accounts              # list configured account profiles
uv run paypay cache-clear           # clear the local response cache
```

**`risk` is facts-only.** It reports portfolio *structure* — total, cash %, largest
position %, top-1/3/5 concentration, 種類/口座 splits, and **two distinct currency/region
measures**: 米国 *計価*(証券, USD-quoted) vs 米国株 *底層暴露* (US-underlying, which also
counts JPY-priced S&P500/NASDAQ etc. 投信). It gives **no risk verdict and no buy/sell
advice**, same boundary as every other command.

**`snapshot` / `diff`** give the account its own long-term series:
`snapshot save` writes `<state_dir>/snapshots/<ts>.json` (assets, cash, holdings,
realized, deposits); `diff` compares a live read against the latest snapshot (or
`--days N` ago) so you get "this week's change" — asset/holdings/deposit/realized deltas.
Automate it: `bin/snapshot-cron.sh install` schedules a daily (07:30 local, trading
days) read-only snapshot of **every** profile + a calendar rebuild + a Monday
`diff --days 7`, logged to `~/.paypay-sec/snapshot-cron.log` (`uninstall` / `status` too).

**`calendar`** reads only the saved snapshots (no creds/network) and builds a
single self-contained, offline HTML — the 每日涨跌日历: month + year (GitHub-style
heatmap) views, 红涨/绿跌, per-account toggle + 合计. Each day's value is the
snapshot-to-snapshot change in 総資産 **minus that day's net cash flow** (so deposits
aren't counted as profit; full-quality, includes 投信). Needs ≥2 snapshots on
different days per account. Default / `-a all` = household (all profiles); `-a <name>`
= one. Labels come from `PAYPAY_LABEL` in each `.env` (else the profile name). The
template (`paypay_sec/calendar_template.html`) is data-driven: the cron regex-swaps
the JSON between `/* DATA_START */ … /* DATA_END */`. `--json` emits the raw schema.

Any command takes `-a <name>` to target a non-default account, and
`--format table|lark|json`. **`--format lark`** emits Feishu/Lark-friendly
bullets (bold numbers, `+¥`/`-¥`, no wide tables) — use it for `review` /
`trades-summary` / `risk` / `diff` when posting to Lark.

**`--lang ja|zh`** localizes the table/lark labels (`zh` = 中文: 総資産→总资产,
評価損益→持仓盈亏, 実現→已实现, …); JSON output keys stay English. **`--all`** pages
the full ledger history (until `NEXT_FLG=false`) for complete realized P&L instead of
the default page cap. Reports also stamp **查询时间 (as_of)** + per-source freshness
(live/cache/stale) and loudly flag any feed that failed — never silently ¥0.

**JSON is versioned:** every `--json` dict payload carries `schema_version` (currently
`"1.0"`) so cron jobs / dashboards / the daily snapshot can parse it stably.

**`-a all`** consolidates across every configured account profile (`total` / `assets` /
`risk` / `plans` / `tax`): combined household view (same fund summed) + per-account
totals — e.g. `risk -a all` shows whole-household concentration & US-underlying exposure.
**`review`** also
reports a **money-weighted return (XIRR, 資金加重収益率)** — the proper annualized
performance when 定投/deposits are ongoing (use `--all` for the full deposit history).

**Scope of the read commands: data display only.** The commands above just show
your account's data (or factual calculations on it — totals, realized P&L by
average-cost basis, cost aggregation). They give NO judgments, NO risk/position
rules, NO buy/sell advice. Order placement is a separate, explicit flow ↓ — and
it too gives no advice: it places exactly the order *you* specify, nothing more.

## Ordering (下单 — 米国株 buy/sell/cancel)

**Trading is OFF by default.** `buy`/`sell`/`orders`/`cancel` refuse to run unless
the human sets **`PAYPAY_TRADING_ENABLED=1`** (in `~/.paypay-sec/.env` or the shell).
A read-only / 复盘 invocation can't place an order even by accident — this capability
gate sits on top of the dry-run + confirm + TRADE_PASSWORD wall below.

PayPay証券 has no order API; this drives the same un-pinned web order endpoints
the site uses (`/trade/brand/ajax_*_popup` → `ajax_*_complete`). **The agent never
auto-submits a live order.** Every order is a dry-run (見積/preview only) unless a
human adds `--execute`, and `--execute` then requires (1) typing an exact
confirmation phrase and (2) entering the account **TRADE_PASSWORD** (取引パスワード)
at an interactive `getpass` prompt. The agent does not know, store, or handle that
password — only the live confirm/preview ever runs unattended.

```bash
# DRY-RUN (default) — runs the real 見積/confirm, prints the quote, places NOTHING
uv run paypay buy  TSLA --amount 10000        # 金額指定: ¥10,000 of TSLA, at the quote
uv run paypay buy  TSLA --qty 1               # 株数指定: 1 share
uv run paypay sell TSLA --qty 0.5             # sell 0.5 share
uv run paypay buy  QQQ  --amount 50000 --account-type 3   # into 成長投資枠 NISA

# LIVE — human only. Prompts for the confirmation phrase, THEN the TRADE_PASSWORD.
uv run paypay buy  TSLA --amount 10000 --execute

uv run paypay orders                          # list 未約定/予約注文 (pending orders)
uv run paypay cancel <ORDER_ID>               # cancel a pending order
```

- `--amount <yen>` (金額指定) **or** `--qty <shares>` (株数指定) — exactly one.
- `--limit <price>` is **optional** — PayPay 米国株 fill at the prevailing quote;
  give `--limit` only for a 指値, or `--market-order` to force 成行.
- `--account-type` — `2`=特定 (taxable/cash, default) · `3`=成長投資枠NISA · `4`=つみたて.
- **Guards** (in `guards.py` / `TradeConfig`): `allow_markets` (米国株 only for now),
  `max_order_jpy` per order, and a daily order-count cap. A blocked order prints
  `⛔ order blocked by guards: …` and never reaches the network confirm.
- Every order attempt is appended to an **audit log** (`audit.py`) with a daily count.
- Funding model: the account is funded by cash balance (you top it up); orders are
  placed against that cash. Bank-card buying is mobile-app/passkey-only and out of scope.

**Run from anywhere (no `cd`):** symlink the bundled launcher onto your PATH —
`ln -s <skill-dir>/bin/paypay ~/.local/bin/paypay` (skill-dir varies by runner:
`~/.claude/skills`, `~/.hermes/skills`, `~/.agents/skills`, …) — then
`paypay review --format lark` works in any directory (the launcher resolves the
symlink + skill dir and runs `uv run` there).

**`total` / `assets` scope:** 証券 (株+ETF) + 投信 holdings + the account cash
balance, giving the full grand total that matches the app's 保有資産 figure. The
cash comes from the transaction ledger's running `CASH_BALANCE`
(`/trade/history/ajax_settlement.json`), NOT from PayPayマネー — the cash lives on
the securities site after all. Only CFD (`cfd.paypay-sec.co.jp`, separate login)
is excluded.

**Response cache (anti-throttle):** every GET/POST response is cached to
`~/.paypay-sec/cache/` with a TTL (default 120s, env `PAYPAY_CACHE_TTL`). Repeated
or overlapping commands (and the `total`/`assets`/`trades` trio, which share the
settlement ledger) reuse cached data instead of re-hitting the API — important
because the `ajax_settlement` endpoint throttles (returns empty) when hammered.
`--no-cache` forces fresh fetches; `paypay cache-clear` empties it.

**Session reuse:** after the first login the session cookies + token are cached
to `~/.paypay-sec/session.json` (mode 0600) and reused, so `/login.json` is hit
only on a cold start or when the session has expired (a fetch bouncing to
`/login/` triggers exactly one automatic re-login). This keeps load off the
login endpoint instead of authenticating on every command.

Flags (place AFTER the subcommand, e.g. `uv run paypay portfolio -m usa -a second --json`):
- `-m, --market <usa|japan>` — market segment. Aliases: `jp`→`japan`, `us`→`usa`,
  `米国株`, `日本株`. Default `usa`.
- `-a, --account <name>` — account profile (default reads `~/.paypay-sec/.env`).
- `--json` — machine-readable output.
- `--no-cache` — bypass the response cache.

## Architecture (for maintenance)

- `config.py` — credential loading from `.env`/env (never logged).
- `client.py` — `PayPayClient`: `login()` + `get_page()`; market-slug aliases;
  the web order methods `order_confirm()` / `order_submit()` / `open_orders()` /
  `order_cancel()` (FuelPHP CSRF from the `fuel_csrf_token` cookie; ticker→BRAND_ID
  resolution; single-use ORDER_CONFIRM_NO tokens).
- `orders.py` — order model (`OrderRequest`) + the build→guard→confirm→[confirm
  phrase]→submit pipeline (`dry_run()` / `place()`); injected confirm/submit
  callables so the live submit is never reached except via the human path.
- `guards.py` — `TradeConfig` + `check_guards()`: allow-listed market, per-order
  yen cap, daily order-count cap (pre- and post-confirm checks).
- `audit.py` — append-only order log + daily order count.
- `parsers.py` — **all site-specific selectors live here.** Data sources:
  - account header → `div.mypage_assets_data / .mypage_invest / .mypage_gain`
  - holdings → `table.d_table` on `/trade/portfolio/brands/<market>`
  - history → `var ticks/cashData/acuisitionData` JS arrays (values in 万円)
  - 投信 valuation → `parse_invtrust()` over the `POST /v2/invest/brand/pc_invest_top`
    JSON (NOT HTML — the 投信 page is a Vue SPA; the JSON API needs the FormData
    fields `APP_VERSION/UUID/DEVICE_TOKEN/OS/APP_ID`, empty body hangs the server)
  - 投信 ledger → `parse_invtrust_transactions()` over
    `GET /v0/history/settlements.json?MARKET_ID=99&PAGE_NUM=<n>&OS=pc` — a SEPARATE
    market view from the 証券 ajax ledger (the SPA's "取引の履歴" tab). Holds 投信
    買付/売却/入金/譲渡益税(type 8)/送金手数料(type 54), with 口座区分 in
    `ACCOUNT_TYPE` (1 一般 / 2 特定 / 3 NISA成長 / 4 NISAつみたて). PAGE_NUM here is a
    RECORD OFFSET (n..n+19), same gotcha as the 証券 ajax — step by 20 + de-dup by
    SEQ_NO (paging by 1 without de-dup inflates totals ~10×). `MINI_CLIENT_SEQ_NO`
    is optional. The 入金/送金手数料 rows are account-wide (identical to the 証券
    ledger); only 買付/売却/譲渡益税 are 投信-specific. `review` folds its realized
    P&L + 譲渡益税 + 送金手数料 into the full-account 総収益 so the 証券-only residual
    no longer hides 投信 trading. The endpoint throttles (empty) when hammered.
  - open orders → `parse_open_orders()` over `table.d_table` on `/trade/preorder/`
- `market.py` — real USD/JPY mid (ECB via frankfurter.dev), cached forever on disk.
- `costs.py` — `paypay fees` logic: explicit fees from the ledger + FX spread
  measured as applied 為替レート vs market mid. PayPay never itemizes its spread
  (ledger and 取引報告書 PDFs both show 手数料=¥0; cost is baked into 約定価格 +
  為替レート), so the FX-spread number is reconstructed, and the price spread
  (~0.5%/0.7%) is only an optional `--price-spread-pct` estimate.
- `report.py` — `review` / `trades-summary` / `invtrust-history` aggregation:
  deposits, per-brand buy/sell/net, realized P&L via **moving-average cost**
  (移動平均法 — what JP 特定口座 uses; correct when buys & sells interleave, unlike
  a whole-window average). `BrandFlow.events` holds the chronological trade stream;
  callers feed it oldest-first (`reversed(ledger)`). A `reconciles` flag goes False
  when a sell exceeds the basis on record (window too short → realized under-est.).
  Pure facts — no rules/judgments.
- `cli.py` — argparse subcommands + rendering (CJK-width-aware tables; table/lark/json).
  The order subcommands wire the pipeline, prompt for the confirmation phrase +
  TRADE_PASSWORD (`getpass`, never stored), and emit the dry-run/submitted/blocked
  result. `_persist_cash()` caches 現金 to `~/.paypay-sec/[acct/]last_cash.json` and
  falls back to it (marked `⚠stale`) when the settlement ledger throttles to empty,
  so `assets`/`total`/`review` never silently report cash as ¥0.

Dev-only, NOT shipped: repo-root `tests/` (real-fixture tests; gitignored as it
holds account HTML/JSON). Run: `uv run --project skills/paypay-sec python tests/test_parsers.py`.
**Committable** synthetic regression tests for the 投信 ledger + moving-average:
`skills/paypay-securities/selftest.py` —
`uv run --project skills/paypay-securities python skills/paypay-securities/selftest.py`.

**Fragility:** the 証券 pages are server-side-rendered HTML, so a frontend
redesign breaks parsing — fix only `parsers.py` (all selectors live there).

## Safety

The read commands are read-only. The order commands place trades, behind a
human-in-the-loop wall:
- **Trading is disabled by default.** `buy`/`sell`/`orders`/`cancel` refuse to run
  unless `PAYPAY_TRADING_ENABLED=1` is set — a read-only/复盘 session has no order
  capability at all.
- **Dry-run is the default.** Even when enabled, a bare `buy`/`sell` only runs the
  見積/preview; it places nothing. A live order requires an explicit `--execute`.
- **`--execute` is human-only.** It demands a typed confirmation phrase *and* the
  account TRADE_PASSWORD at an interactive prompt. The agent never enters that
  password and is not built to run a live submit unattended.
- **Guards + audit.** Per-order yen cap, allow-listed market, daily order-count
  cap; every attempt is logged. Confirm tokens (ORDER_CONFIRM_NO) are single-use
  and writes are never auto-retried.
- **No advice.** The skill places exactly the order you specify and never offers
  buy/sell/position judgments.

Automated access to a brokerage may conflict with PayPay証券's terms of service —
use on your own account at your own risk. The order confirm/submit response field
names + the open-orders column mapping are finalized against a live capture when
the US market is open (closed → buyable=0 → confirm returns STATUS=false), so run
a dry-run `buy` at market open to validate before the first live `--execute`.
