---
name: paypay-securities
description: >-
  Read-only access to a PayPay証券 (PayPay Securities, formerly One Tap BUY)
  account via its web frontend. Use when the user wants to check their PayPay
  証券 portfolio, holdings, balance / valuation, unrealized P&L, or asset
  history from the command line. Logs in through /login.json with a trusted-
  device cookie (no SMS) and parses the server-rendered pages. Phase 1 is
  READ-ONLY — it never places or cancels orders.
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
| 3 — trading | not built | buy/sell/cancel — **high risk; requires dry-run + confirmation + limit guards** |

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
uv run paypay total                 # aggregate 証券 + 投信 invested assets (excludes cash)
uv run paypay assets                # one-shot consolidated holdings + cash + grand total (parallel)
uv run paypay trades [--pages N]    # transaction ledger (買付/売却/入金/手数料) + running cash balance
uv run paypay fees [--detail]       # cost analysis: explicit fees + measured FX spread (+ optional price spread)
uv run paypay cache-clear           # clear the local response cache
```

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

Flags (place AFTER the subcommand, e.g. `uv run paypay portfolio -m usa --json`):
- `-m, --market <usa|japan>` — market segment. Aliases: `jp`→`japan`, `us`→`usa`,
  `米国株`, `日本株`. Default `usa`.
- `--json` — machine-readable output.

## Architecture (for maintenance)

- `config.py` — credential loading from `.env`/env (never logged).
- `client.py` — `PayPayClient`: `login()` + `get_page()`; market-slug aliases.
- `parsers.py` — **all site-specific selectors live here.** Data sources:
  - account header → `div.mypage_assets_data / .mypage_invest / .mypage_gain`
  - holdings → `table.d_table` on `/trade/portfolio/brands/<market>`
  - history → `var ticks/cashData/acuisitionData` JS arrays (values in 万円)
  - 投信 → `parse_invtrust()` over the `POST /v2/invest/brand/pc_invest_top` JSON
    (NOT HTML — the 投信 page is a Vue SPA; the JSON API needs the FormData fields
    `APP_VERSION/UUID/DEVICE_TOKEN/OS/APP_ID`, empty body hangs the server)
- `market.py` — real USD/JPY mid (ECB via frankfurter.dev), cached forever on disk.
- `costs.py` — `paypay fees` logic: explicit fees from the ledger + FX spread
  measured as applied 為替レート vs market mid. PayPay never itemizes its spread
  (ledger and 取引報告書 PDFs both show 手数料=¥0; cost is baked into 約定価格 +
  為替レート), so the FX-spread number is reconstructed, and the price spread
  (~0.5%/0.7%) is only an optional `--price-spread-pct` estimate.
- `cli.py` — argparse subcommands + rendering (CJK-width-aware table helpers).

Dev-only (kept at repo root, NOT shipped with the skill): `tests/` (the fixtures
hold real account HTML/JSON, so `tests/fixtures/` is gitignored).
Run: `uv run --project skills/paypay-sec python tests/test_parsers.py`.

**Fragility:** the 証券 pages are server-side-rendered HTML, so a frontend
redesign breaks parsing — fix only `parsers.py` (all selectors live there).

## Safety

Read-only. No write/order endpoints are implemented. Automated access to a
brokerage may conflict with PayPay証券's terms of service — use on your own
account at your own risk.
