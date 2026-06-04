from _runner import run
from paypay_sec import i18n


def test_zh_translates_headline_terms():
    s = "総資産 ¥100  純入金 ¥50  評価損益 +¥5  実現損益合計 +¥9  取引コスト ¥1"
    z = i18n.zh(s)
    for term in ("总资产", "累计净入金", "持仓盈亏", "已实现盈亏合计", "交易成本"):
        assert term in z, (term, z)
    # numbers are untouched
    assert "¥100" in z and "+¥5" in z


def test_longest_match_wins():
    # 実現損益 must become 已实现盈亏 (not 已实现+損益), and 評価損益率 keeps its 率
    assert i18n.zh("実現損益") == "已实现盈亏"
    assert i18n.zh("評価損益率") == "持仓盈亏率"
    assert i18n.zh("評価損益") == "持仓盈亏"


def test_localize_ja_is_noop():
    s = "総資産 ¥1"
    assert i18n.localize(s, "ja") == s
    assert i18n.localize(s, "zh") == "总资产 ¥1"


def test_no_named_residuals():
    # the specific JA the user flagged must be gone after zh()
    samples = [
        "投資 ¥500,000  評価損益(=App頭条)",
        "実現益から 譲渡益税 ¥1,102・送金手数料 ¥440・為替等を差引いた後の値",
        "P&L +¥5  buyable cash  settling",
        "測定コスト 合計  現金側手数料/税  推定為替コスト",
    ]
    for s in samples:
        z = i18n.zh(s)
        for bad in ("投資", "App頭条", "から", "P&L", "buyable", "settling",
                    "測定", "現金側", "コスト"):
            assert bad not in z, (bad, z)


def test_empty_and_unknown_safe():
    assert i18n.zh("") == ""
    assert i18n.zh("hello world 123") == "hello world 123"


if __name__ == "__main__":
    raise SystemExit(run(globals()))
