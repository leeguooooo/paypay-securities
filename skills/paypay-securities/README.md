# paypay-securities

A small Python CLI + agent skill for a **PayPay証券 (PayPay Securities / ペイペイ証券)**
account. It authenticates to the same endpoints the website uses (there is no
official PayPay証券 API) and reads your account: portfolio, holdings, balance,
投資信託, 米国株, 取引履歴, fees & FX-spread, and a 復盘/review with realized &
unrealized P&L. It can also **place 米国株 orders** behind a strict human-in-the-loop
wall (see [Safety](#safety)).

> Agent-skill docs live in [`SKILL.md`](SKILL.md); this README is the human quick-start.

## Install

```bash
npx skills add leeguooooo/paypay-securities --skill paypay-securities      # project-local
npx skills add leeguooooo/paypay-securities --skill paypay-securities -g   # user-global
```

Requires [`uv`](https://docs.astral.sh/uv/) on your PATH. The CLI (the bundled
`paypay_sec/` package + `pyproject.toml`) installs alongside `SKILL.md`.

**Run from anywhere** — symlink the launcher onto your PATH. The skill dir depends on
your runner (`~/.claude/skills`, `~/.hermes/skills`, `~/.agents/skills`, …), so adjust
the path to where it's installed:

```bash
ln -s ~/.claude/skills/paypay-securities/bin/paypay ~/.local/bin/paypay   # ← adjust to your install
paypay review --format lark --lang zh
```

(The launcher runs `uv run --project <skill>` for you and silences uv's
`VIRTUAL_ENV` warning.)

## Configure credentials

Copy `.env.example` to `~/.paypay-sec/.env` and fill it in (this path is searched
first, so the CLI works from any directory). **Never commit it.**

| Key | What |
|---|---|
| `PAYPAY_MEMBER_ID`, `PAYPAY_PASSWORD` | login credentials |
| `PAYPAY_COOKIE` | the full `Cookie:` header from a logged-in browser — **must** contain the `..._SMS_AUTH_STRING` trusted-device token, or the server demands an SMS code |

Run `paypay doctor` to check your setup (it tells you if the cookie is missing the
trusted-device token, whether a session is cached, etc.) — no network needed.

**Multiple accounts:** each profile is `~/.paypay-sec/<name>.env`; select with
`-a <name>` (the default account uses `~/.paypay-sec/.env`). `paypay accounts` lists them.

## Common commands

```bash
paypay doctor [--online]           # diagnose setup / login readiness (--online probes the live session)
paypay assets [--accounts]         # 証券 + 投信 holdings + cash + grand total (+口座区分 with --accounts)
paypay review                      # 復盘: 持仓盈亏 / 累計実現 / 整体盈亏 + costs
paypay risk [--accounts]           # 持仓结构: weights, concentration, FX/category/口座 split (facts only)
paypay plans                       # 定投/つみたて: active recurring-buy funds + monthly run-rate (scans full history; --fast = quick)
paypay tax                         # per-year tax view: 売却 / 譲渡益税 / 分配金 (full history by default; --fast = quick)
paypay total -a all                # consolidate every account profile (also: assets / risk / plans / tax -a all)
paypay risk -a all                 # whole-household concentration & US-underlying exposure (both accounts)
paypay total                       # aggregate invested assets
paypay trades [--pages N | --all]  # transaction ledger + running cash
paypay invtrust-history            # 投信 ledger + moving-average realized P&L
paypay fees [--detail]             # explicit fees + measured FX spread
paypay trades-summary              # per-brand buy/sell/net/realized P&L
paypay snapshot save               # save a dated account snapshot (your own time series)
paypay snapshot list               # list saved snapshots
paypay diff [--days 7]             # diff a live read vs the latest (or N-days-old) snapshot
paypay calendar                    # build the 每日涨跌日历 HTML from saved snapshots (--json for raw data, --out PATH)
bin/snapshot-cron.sh install       # schedule a daily read-only snapshot + calendar rebuild + Monday weekly diff (macOS launchd)
```

### 每日涨跌日历 (P&L calendar)

`paypay calendar` turns your saved snapshots into a self-contained, offline HTML
calendar of **daily mark-to-market P&L** — month and year views, 红涨/绿跌 heatmap,
per-account toggle (`leo` / `lisa` / 合计). Each day's number is the change in total
assets **minus that day's cash flow**, so deposits never read as profit; it includes
投信 (full-quality), unlike the US-stock-only live series.

```bash
paypay calendar                    # household calendar → <state_dir>/pnl-calendar.html
paypay calendar -a second          # just one account
paypay calendar --json             # the raw schema (for piping / dashboards)
paypay calendar --out ~/pnl.html   # choose the output path
```

Needs **≥2 snapshots on different days** per account to show any P&L (the daily cron
accumulates them). Account labels come from `PAYPAY_LABEL` in each profile's `.env`
(falls back to the profile name). The viewer is data-driven — swap the JSON between
the `/* DATA_START */ … /* DATA_END */` markers and the calendar re-renders.

### Useful flags (place after the subcommand)

| Flag | Effect |
|---|---|
| `--format table\|lark\|json` | output format. `lark` = Feishu/Lark bullets. |
| `--lang ja\|zh` | label language for table/lark. `zh` = 中文 (JSON keys stay English). |
| `--all` | fetch the **full** ledger history (page until `NEXT_FLG=false`) for complete realized P&L. |
| `-a <name>` | target a non-default account. |
| `-m usa\|japan` | market segment (aliases `jp`/`us`/`米国株`/`日本株`). |
| `--no-cache` | bypass the local response cache. |

Reports stamp a **查询时间 (as_of)** and per-source **freshness** (live/cache/stale);
a feed that drops out is flagged loudly, never silently treated as ¥0. Every `--json`
payload carries a `schema_version` so cron jobs / dashboards can parse it stably.

## Ordering (米国株 buy / sell / cancel)

**Trading is OFF by default** — `buy`/`sell`/`orders`/`cancel` refuse to run unless you
set `PAYPAY_TRADING_ENABLED=1` (in `~/.paypay-sec/.env` or the shell). Even then orders
are **dry-run by default**; a live order needs an explicit human `--execute`, which then
prompts for a typed confirmation **and** the account TRADE_PASSWORD. See [Safety](#safety).

```bash
export PAYPAY_TRADING_ENABLED=1              # enable the trading commands first
paypay buy TSLA --amount 10000               # DRY-RUN (preview only)
paypay buy TSLA --amount 10000 --execute     # LIVE — human only; prompts for TRADE_PASSWORD
paypay orders                                # list pending orders
paypay cancel <ORDER_ID> --execute           # cancel (human only)
```

## Safety

- **Read commands are read-only.** They show your data + factual calculations
  (totals, realized P&L by moving-average cost). **No buy/sell advice, no risk
  verdicts, no judgments** — `paypay risk` reports *structure* (weights,
  concentration), not opinions.
- **Order placement is human-gated:** disabled unless `PAYPAY_TRADING_ENABLED=1`;
  dry-run by default; `--execute` is the only path to a live order and requires a
  typed confirmation **and** the TRADE_PASSWORD at an interactive prompt. The agent
  never enters that password and is not built to submit a live order unattended.
  Per-order yen caps, an allow-listed market, and a daily order-count cap are
  enforced (`guards.py`); every attempt is logged (`audit.py`); confirm tokens are
  single-use and writes never auto-retry.

Automated access to a brokerage may conflict with PayPay証券's terms of service —
use on your own account at your own risk.

## FAQ

- **Login asks for an SMS code.** Your `PAYPAY_COOKIE` is missing the
  `..._SMS_AUTH_STRING` trusted-device token. Re-copy the full cookie from a
  logged-in browser. `paypay doctor` checks this.
- **Numbers look stale / cash shows as ¥0.** The settlement-ledger endpoint
  throttles when hammered; the CLI caches the last good cash value and marks it
  `⚠stale`. Re-run later or use `--no-cache`. The freshness line shows which
  source was live vs cache.
- **Realized P&L looks too small.** The default page cap may miss early buys.
  Re-run with `--all` to page the full history (a `⚠ 取得原価が不足` hint appears
  when a sell has no matching buy basis in the fetched window).
- **CFD isn't included.** `cfd.paypay-sec.co.jp` is a separate login and is out of
  scope. `total`/`assets` cover 証券 + 投信 + cash (the app's 保有資産 total).

## Development

```bash
uv run --project skills/paypay-securities python skills/paypay-securities/tests/test_orders.py
uv run --project skills/paypay-securities python skills/paypay-securities/selftest.py
```

All site-specific HTML/JSON selectors live in `parsers.py`; a frontend redesign
only touches that module. Real-fixture tests live in the gitignored repo-root
`tests/` (they hold account PII); the committable `tests/` here use synthetic data.
