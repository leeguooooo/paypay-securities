# paypay-securities

[日本語](README.md) | **English**

A command-line client for a **PayPay証券 (PayPay Securities, ペイペイ証券)**
account — balance, holdings, mutual funds (投資信託), US stocks (米国株), transaction
history, a measured cost analysis (incl. the FX spread PayPay never itemizes), and
a portfolio **review** with realized/unrealized P&L. Great for NISA and routine
investment review. Data display only — no advice. Distributed as an
[agent skill](https://skills.sh) and usable as a plain CLI.

> Read commands are read-only. US-stock buy/sell/cancel is supported but OFF by
> default (`PAYPAY_TRADING_ENABLED=1`), dry-run by default, and a live order needs a
> human at a terminal (TTY) running `--execute` + a typed confirmation + the trade
> password. Orders are yen-amount only, filled at the quote (off-hours: a 予約注文 for the next session). Use on your own
> account at your own risk — automated access may conflict with PayPay証券's terms of service.

## Install

```bash
npx skills add leeguooooo/paypay-securities --skill paypay-securities       # project-local
npx skills add leeguooooo/paypay-securities --skill paypay-securities -g     # user-global (~/.claude/skills/)
```

This installs the `paypay-securities` skill (SKILL.md + the bundled Python CLI) into your
agent's skills directory. Requires [`uv`](https://docs.astral.sh/uv/).

## Update

```bash
paypay self-update --check --json   # installed vs latest (main); changes nothing
paypay self-update --yes            # update in place (prompts instead of --yes on a TTY)
paypay self-update --ref <tag|sha> --yes   # pin a version
```

The skill and the CLI ship as one folder, so they update together. `self-update`
only talks to GitHub (git fetch) and `uv`; it never logs in, never reads credentials,
never runs trading commands, and never touches `~/.paypay-sec` (credentials, sessions,
cache, snapshots). It only updates a global `npx skills add -g` install recorded in
`~/.agents/.skill-lock.json`; for git checkouts or other installs it prints the manual
command (`npx skills update paypay-securities -g -y`). The copied folder is checked
against the target git tree hash, `uv sync` + offline `--help` run afterwards, and the
previous folder is restored if that fails. Local edits to the skill folder block the
update unless you pass `--force` (a backup is kept); a `.env` inside the folder and
files you added are carried over.

## Configure

Put credentials in `~/.paypay-sec/.env` (see
[`skills/paypay-securities/.env.example`](skills/paypay-securities/.env.example)):

```
PAYPAY_MEMBER_ID=Pxxxxxxxxx
PAYPAY_PASSWORD=...
PAYPAY_COOKIE=...   # full Cookie header incl. the ..._SMS_AUTH_STRING device token
```

**Multiple accounts:** the default account reads `~/.paypay-sec/.env`; a named
account `<name>` reads `~/.paypay-sec/<name>.env`. Switch with `-a <name>` (or
`PAYPAY_ACCOUNT`): `uv run paypay assets -a second`. Each account keeps its own
session + cache. `uv run paypay accounts` lists them.

## Use

```bash
cd skills/paypay-securities
uv run paypay assets               # consolidated holdings + cash + grand total
uv run paypay review               # review: assets, realized/unrealized P&L, deposits, costs, holdings
uv run paypay review --format lark # Feishu/Lark-friendly bullets
uv run paypay trades-summary       # per-brand buy/sell/net-invested/realized P&L
uv run paypay fees                 # cost analysis (explicit fees + measured FX spread)
```

Run from anywhere (no `cd`): put the launcher on PATH —
`ln -s "$HOME/.claude/skills/paypay-securities/bin/paypay" ~/.local/bin/paypay`
→ `paypay review --format lark`.

Full command reference and architecture notes: [`skills/paypay-securities/SKILL.md`](skills/paypay-securities/SKILL.md).

## Repository layout

```
skills/paypay-securities/     the shippable skill (SKILL.md + paypay_sec/ CLI + pyproject.toml)
tests/                        dev tests (fixtures hold real account data → gitignored)
```
