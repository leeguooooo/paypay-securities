"""Output localization for human-facing renders (table / lark).

The CLI's render strings are written in Japanese (matching the PayPay証券 app's
own labels). `--lang zh` runs the rendered text through a Japanese→Chinese term
map so the user doesn't have to translate every report by hand.

This is a *term* substitution, not a full translator: it swaps the financial
vocabulary the user cares about (総資産→总资产, 評価損益→持仓盈亏, …). JSON output
is never translated — machine-readable keys stay stable.

Keys are applied longest-first so a compound term (実現損益) is replaced before
its substring (実現). All keys/values are plain str; the pass is idempotent
enough for one render.
"""
from __future__ import annotations

# Japanese term  →  Chinese term. Order does not matter here — _apply() sorts by
# key length descending so the longest match wins.
_ZH_TERMS: dict[str, str] = {
    # headline P&L / asset vocabulary
    "評価損益率": "持仓盈亏率",
    "評価損益": "持仓盈亏",
    "実現損益合計": "已实现盈亏合计",
    "実現損益": "已实现盈亏",
    "未実現": "未实现",
    "実現益": "已实现收益",
    "実現": "已实现",
    "総資産合計": "总资产合计",
    "総資産": "总资产",
    "純入金": "累计净入金",
    "投資資産": "投资资产",
    "投資元本": "投资本金",
    "投資": "投资",
    "App頭条": "App头条",
    "頭条": "头条",
    "P&L": "损益",
    "から": "从",
    "buyable": "可买",
    "評価額合計": "市值合计",
    "評価額": "市值",
    "保有資産": "持有资产",
    "保有": "持仓",
    "含み損益": "浮动盈亏",
    "取得原価": "取得成本",
    "取得単価不足": "取得单价不足",
    # cash / flows
    "現金残高": "现金余额",
    "現金": "现金",
    "買付可能金額": "可买入金额",
    "買付可能": "可买入",
    "買付": "买入",
    "売却申込中": "卖出申请中",
    "売却口数": "卖出口数",
    "売却": "卖出",
    "売って確定": "卖出确定",
    "入金": "入金",
    "出金": "出金",
    "送金手数料": "汇款手续费",
    "手数料": "手续费",
    "税引前": "税前",
    "譲渡益税": "资本利得税",
    # fx / cost
    "為替スプレッド": "汇率点差",
    "為替レート": "汇率",
    "為替": "汇率",
    "測定コスト": "测算成本",
    "測定": "测算",
    "取引コスト": "交易成本",
    "コスト": "成本",
    "現金側": "现金侧",
    "推定": "估算",
    "約定価格": "成交价格",
    "に内包": "内含",
    "内包": "内含",
    "取引履歴": "交易历史",
    "取引集計": "交易汇总",
    "取引明細": "交易明细",
    "取引": "交易",
    "証券手数料": "证券手续费",
    # instruments / accounts
    "銘柄": "标的",
    "純投入": "净投入",
    "口座区分": "账户类别",
    "口座別": "按账户",
    "口座": "账户",
    "口数": "口数",
    "米国株式": "美国股票",
    "米国株": "美股",
    "米国": "美国",
    "個股": "个股",
    "種類": "种类",
    "底層暴露": "底层暴露",
    "底層": "底层",
    "計価": "计价",
    "実質": "实质",
    "値付け": "定价",
    "日本株": "日股",
    "証券": "证券",
    "投信": "基金(投信)",
    "基金": "基金",
    # full disclaimer notes (whole-sentence — precede component terms)
    "参考値のみ・非正式。確定申告は PayPay 証券交付の年間取引報告書で確認。NISA非課税/特定源泉徴収。":
        "仅参考值·非正式。报税以 PayPay 证券交付的年度交易报告书为准。NISA 免税/特定源泉扣缴。",
    "全口座合算・参考値のみ・非正式。確定申告は PayPay 証券交付の年間取引報告書で確認。":
        "全账户合算·仅参考值·非正式。报税以 PayPay 证券交付的年度交易报告书为准。",
    "「未推算」= まだ買付記録がなく月額を推算できない(active だが初回約定待ち)":
        "「未推算」= 尚无买入记录,无法推算月额(active,等待首次约定)",
    "月額(推) 未推算(尚無買付記録)": "月额(推) 未推算(尚无买入记录)",
    "推算依据: 累計": "推算依据: 累计",
    "年率換算。様本期間が短い間は変動が大きく出ます(長期収益力ではない)":
        "年率换算。样本期间短时波动会被放大(并非长期收益能力)",
    "年率換算·様本期短だと変動大": "年率换算·样本期短则波动大",
    "設定なし/停止?": "未设定/暂停?",
    "未推算": "未推算",
    "ヶ月": "个月",
    "様本": "样本",
    "換算": "换算",
    "変動": "变动",
    "買付記録": "买入记录",
    "約定": "约定",
    "記録": "记录",
    "設定金額・頻度は web API 非公開。実際の積立額は invtrust-history で確認。":
        "设定金额·频率未在 web API 公开。实际定投额见 invtrust-history。",
    "買付可能現金": "可买入现金",
    "定投額は実際のNISAつみたて買付からの推計(設定値はAPI非公開)。事実のみ、助言ではありません。":
        "定投额=按实际 NISAつみたて 买入推算(配置值 API 未公开)。仅事实,非建议。",
    "全口座合算。定投額は実際のNISAつみたて買付からの推計。事実のみ。":
        "全账户合算。定投额=按实际 NISAつみたて 买入推算。仅事实。",
    "事実のみ。年間取引報告書の参考値。NISA口座は非課税、特定口座は源泉徴収。":
        "仅事实。年度交易报告书参考值。NISA 账户免税,特定账户源泉扣缴。",
    "全口座合算。年間取引報告書の参考値。事実のみ。":
        "全账户合算。年度交易报告书参考值。仅事实。",
    "全口座合算。CFD は別ログイン未集計。": "全账户合算。CFD 独立登录未计入。",
    "定投額": "定投额",
    "年間取引報告書": "年度交易报告书",
    "参考値": "参考值",
    "非課税": "免税",
    "源泉徴収": "源泉扣缴",
    # 定投 / つみたて + analysis labels
    "資金加重収益率": "资金加权收益率",
    "資金加重": "资金加权",
    "税務サマリー": "税务摘要",
    "税務": "税务",
    "サマリー": "摘要",
    "証券売却": "证券卖出",
    "投信売却": "基金卖出",
    "分配金": "分红",
    "全口座": "全账户",
    "口座別": "按账户",
    "累計投入": "累计投入",
    "月定投": "月定投",
    "計画": "计划",
    "推計": "推算",
    "年率": "年率",
    "月額": "月额",
    "月数": "月数",
    "設定なし": "未设定",
    "停止": "暂停",
    "設定中の銘柄": "设定中的标的",
    "設定金額・頻度は web API 非公開": "设定金额·频率未在 web API 公开",
    "実際の積立額は": "实际定投额见",
    "買付行で確認できます": "买入行",
    "設定金額": "设定金额",
    "頻度": "频率",
    "非公開": "未公开",
    "積立額": "定投额",
    "現保有": "当前持仓",
    "今の保有": "当前持仓",
    "別ログイン": "独立登录",
    # report scaffolding / notes
    "復盘": "复盘",
    "累計実現": "累计已实现",
    "整体盈亏": "整体盈亏",
    "持仓盈亏": "持仓盈亏",
    "通算": "合计通算",
    "最終": "最终",
    "未集計": "未计入",
    "取得失敗": "获取失败",
    "移動平均推計": "移动平均估算",
    "移動平均": "移动平均",
    "過小評価": "可能低估",
    # disclaimer notes (whole-sentence — must precede their component terms)
    "事実データのみ。投資助言・推奨ではありません。": "仅事实数据,非投资建议/推荐。",
    "構造・ウェイトの事実のみ。リスク評価・推奨・売買助言ではありません。":
        "仅结构与权重的事实。非风险评估/推荐/买卖建议。",
    "事実の差分のみ。投資助言ではありません。": "仅事实差异。非投资建议。",
    "事実データのみ": "仅事实数据",
    "投資助言・推奨ではありません": "非投资建议/推荐",
    "リスク評価": "风险评估",
    "売買助言": "买卖建议",
    "投資助言": "投资建议",
    "助言": "建议",
    "推奨": "推荐",
    "ウェイト": "权重",
    "構造": "结构",
    "差分": "差异",
    "事実のみ": "仅事实",
    "事実": "事实",
    "settling": "结算中",
    "うち": "其中",
    "合計": "合计",
    "一部市場の取得に失敗": "部分市场获取失败",
    "集計から除外": "已从合计剔除",
    "実現益から": "从已实现收益中扣除",
    "を差引いた後の値": "后的数值",
    "を差引いた後": "后",
    "App「実現損益合計」が正(米株特定はFX差)": "以App「已实现盈亏合计」为准(美股特定口座为FX差异)",
    "App「実現損益合計」が正": "以App「已实现盈亏合计」为准",
    "が正": "为准",
    "米株": "美股",
    "履歴": "历史",
    "この間": "期间",
    "増分": "增量",
    # CFD note (topic particle は + のため don't survive term-swap; map the whole clauses)
    "は別ログインのため未集計": "为独立登录,未计入",
    "(別ログイン)は未集計": "(独立登录)未计入",
    # risk underlying-exposure parentheticals (の / も particles)
    "S&P500等の投信も含む実質米株": "含S&P500等投信的实质美国股票",
    "S&P500等の投信も実質米株": "S&P500等投信也按美国股票底层计",
    "実質米株": "实质美国股票",
    "本日": "今日",
    "株": "股",
    "件": "条",
}

# Static labels used by report scaffolding that aren't worth a term entry but
# read better fully translated. (applied as whole-token replaces too)
_ZH_PHRASES: dict[str, str] = {
    "データ鮮度": "数据新鲜度",
    "查询时间": "查询时间",
}


def zh(text: str) -> str:
    """Translate a rendered (table/lark) string's JA financial terms to ZH."""
    return _apply(text, {**_ZH_TERMS, **_ZH_PHRASES})


def _apply(text: str, table: dict[str, str]) -> str:
    if not text:
        return text
    for ja in sorted(table, key=len, reverse=True):
        if ja in text:
            text = text.replace(ja, table[ja])
    return text


def localize(text: str, lang: str) -> str:
    """Entry point: translate `text` for `lang` ('zh') or return it unchanged."""
    return zh(text) if lang == "zh" else text
