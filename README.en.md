# paypay-securities

[日本語](README.md) | **English**

A read-only command-line client for a **PayPay証券 (PayPay Securities)** account —
balance, holdings, mutual funds, transaction history, and a measured cost
analysis (including the FX spread PayPay never itemizes). Distributed as an
[agent skill](https://skills.sh) and usable as a plain CLI.

> Read-only: it never places or cancels orders. Use on your own account at your
> own risk — automated access may conflict with PayPay証券's terms of service.

## Install

```bash
npx skills add leeguooooo/paypay-securities --skill paypay-securities       # project-local
npx skills add leeguooooo/paypay-securities --skill paypay-securities -g     # user-global (~/.claude/skills/)
```

This installs the `paypay-securities` skill (SKILL.md + the bundled Python CLI) into your
agent's skills directory. Requires [`uv`](https://docs.astral.sh/uv/).

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
uv run paypay assets     # consolidated holdings + cash + grand total
uv run paypay trades     # transaction ledger
uv run paypay fees       # cost analysis (explicit fees + measured FX spread)
```

Full command reference and architecture notes: [`skills/paypay-securities/SKILL.md`](skills/paypay-securities/SKILL.md).

## Repository layout

```
skills/paypay-securities/     the shippable skill (SKILL.md + paypay_sec/ CLI + pyproject.toml)
tests/                        dev tests (fixtures hold real account data → gitignored)
```
