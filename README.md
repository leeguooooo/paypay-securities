# paypay-securities

**日本語** | [English](README.en.md)

**PayPay証券(ペイペイ証券 / PayPay Securities)** 口座の残高・保有銘柄・投資信託・
米国株・取引履歴を参照し、手数料や為替スプレッドのコストを実測、**復盘(レビュー)**まで
出力する**読み取り専用**のコマンドラインツールです。NISA や日々の投資振り返りに。
[エージェントスキル](https://skills.sh)として配布され、通常の CLI としても使えます。

> 読み取り専用です。注文の発注・取消は一切行いません。ご自身の口座で自己責任の
> もとご利用ください。自動アクセスはPayPay証券の利用規約に抵触する可能性があります。

## インストール

```bash
npx skills add leeguooooo/paypay-securities --skill paypay-securities       # プロジェクトに追加
npx skills add leeguooooo/paypay-securities --skill paypay-securities -g     # ユーザー全体 (~/.claude/skills/)
```

`paypay-securities` スキル(SKILL.md + 同梱の Python CLI)がエージェントのスキル
ディレクトリに導入されます。[`uv`](https://docs.astral.sh/uv/) が必要です。

## 設定

認証情報を `~/.paypay-sec/.env` に置きます
([`skills/paypay-securities/.env.example`](skills/paypay-securities/.env.example) 参照):

```
PAYPAY_MEMBER_ID=Pxxxxxxxxx
PAYPAY_PASSWORD=...
PAYPAY_COOKIE=...   # ログイン済みブラウザの Cookie ヘッダ全体(..._SMS_AUTH_STRING デバイストークンを含むこと)
```

**複数アカウント:** 既定アカウントは `~/.paypay-sec/.env`、別名アカウント `<name>` は
`~/.paypay-sec/<name>.env` を読みます。`-a <name>`(または `PAYPAY_ACCOUNT`)で切り替え:
`uv run paypay assets -a second`。アカウントごとにセッション・キャッシュは独立。
`uv run paypay accounts` で一覧表示。

## 使い方

```bash
cd skills/paypay-securities
uv run paypay assets               # 保有資産の総覧(保有銘柄 + 現金 + 総資産合計)
uv run paypay review               # 復盘: 資産・実現/未実現損益・入金・コスト・保有
uv run paypay review --format lark # Feishu/Lark 向けの箇条書き出力
uv run paypay trades-summary       # 銘柄別の買い/売り/純投入/実現損益
uv run paypay fees                 # コスト分析(明示手数料 + 実測の為替スプレッド)
```

どこからでも実行(`cd` 不要):ランチャーを PATH に通す —
`ln -s "$HOME/.claude/skills/paypay-securities/bin/paypay" ~/.local/bin/paypay`
→ `paypay review --format lark`。

コマンド一覧と設計メモ:
[`skills/paypay-securities/SKILL.md`](skills/paypay-securities/SKILL.md)。

## リポジトリ構成

```
skills/paypay-securities/     配布するスキル(SKILL.md + paypay_sec/ CLI + pyproject.toml)
tests/                        開発用テスト(フィクスチャに実口座データを含むため gitignore)
```
