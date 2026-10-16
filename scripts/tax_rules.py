"""所得税の税率表など（令和8年分）。税額の計算はここを1か所の正とする。

build_books.py（転記シートの申告書・税金の計算）は関数をそのまま使い、
build_dashboard.py（税額の試算）は INCOME_TAX_TABLE などをページに埋め込んでJSで同じ計算をする。
税制が変わったらここを直し、CLAUDE.md の「税制パラメータ」表も同時に直す。

値は令和8年分のもの。使う前に、その年分の国税庁の資料で確かめること。
    所得税の税率（速算表）: https://www.nta.go.jp/taxes/shiraberu/taxanswer/shotoku/2260.htm
"""

# 所得税の速算表: (課税される所得金額の上限, 税率%, 控除額)。上限 None は最後の段
INCOME_TAX_TABLE = [
    (1_950_000, 5, 0),
    (3_300_000, 10, 97_500),
    (6_950_000, 20, 427_500),
    (9_000_000, 23, 636_000),
    (18_000_000, 33, 1_536_000),
    (40_000_000, 40, 2_796_000),
    (None, 45, 4_796_000),
]
# 復興特別所得税（基準所得税額 × 2.1%）。令和9年分から防衛特別1.0%＋復興1.1%になるが合計は同じ
RECONSTRUCTION_PERMILLE = 21
# 寄附金控除: (寄附金 − 2,000円)。寄附金は総所得金額等の40%が上限
DONATION_FLOOR = 2_000
DONATION_CAP_PERCENT = 40

# ---- 事業者ごとの前提（自分の状況に合わせて直す）
# 消費税は簡易課税だけに対応している。みなし仕入率（%）は事業区分で決まる
# （第1種90・第2種80・第3種70・第4種60・第5種50・第6種40）。本則課税・免税事業者には未対応
DEEMED_PURCHASE_RATE = 50


def taxable_income(income, deductions):
    """課税される所得金額（千円未満切捨）。"""
    return max(0, (income - deductions) // 1000 * 1000)


def income_tax_bracket(taxable):
    """(税率%, 控除額)。"""
    return next((rate, d) for limit, rate, d in INCOME_TAX_TABLE if limit is None or taxable <= limit)


def income_tax(taxable):
    """課税される所得金額に対する税額（速算表）。"""
    rate, d = income_tax_bracket(taxable)
    return taxable * rate // 100 - d


def reconstruction_tax(base):
    """復興特別所得税額（1円未満切捨）。"""
    return base * RECONSTRUCTION_PERMILLE // 1000


def donation_deduction(donations, total_income):
    """寄附金控除額。"""
    return max(0, min(donations, total_income * DONATION_CAP_PERCENT // 100) - DONATION_FLOOR)
