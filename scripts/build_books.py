#!/usr/bin/env python3
"""帳簿（仕訳帳・総勘定元帳）と青色申告決算書の数字を生成する。

使い方:
    python3 scripts/build_books.py 2026        # 売上を計上済みの最終月までを期間とする
    python3 scripts/build_books.py 2026/12     # 期間の最終月を指定する

読み込み元（ここでは一切書き換えない）:
    YYYY/MM/勘定科目/YYYYMMDD_取引先_金額.拡張子   経費の証憑。1ファイル＝1仕訳
    YYYY/売上高_YYYY.csv                         月度・取引先別の売上（税込）。前年の12月度は期首の売掛金の消し込みに使う
    YYYY/売上/取引先/                            請求書（仕訳の証憑欄に使う）
    YYYY/売上先一覧_YYYY.csv                     決算書3面の売上先の明細
    YYYY/定期経費_YYYY.csv                       領収書が届かない定期の経費（口座から引き落とされる掛金など。任意）
                                                 列: 月,金額,借方,貸方,摘要（例: 4月,10000,保険料,事業主借,掛金）
    YYYY/寄付金/寄付金_YYYY.csv                  寄附金控除の明細（転記シート）
    YYYY/確定申告サマリー_YYYY.csv               売上高・地代家賃・水道光熱費・固定資産・経費（照合用）・
                                                 社会保険料控除・小規模企業共済等掛金控除・医療費控除・
                                                 専従者給与・扶養控除・基礎控除・青色申告特別控除・予定納税
    YYYY/帳簿/期首残高_YYYY.csv                  前年の貸借対照表の期末の資産・負債（手で作る）
    YYYY/帳簿/手入力仕訳_YYYY.csv                上のどれにもない取引（任意）
                                                 列: 日付,借方,貸方,金額,摘要,証憑
    YYYY/帳簿/入金日_YYYY.csv                    通帳で確かめた売上の入金日（任意）
                                                 列: 月度,取引先,入金日（例: 2026/02,取引先名,2026/03/31）

出力:
    YYYY/帳簿/仕訳帳_YYYY.csv
    YYYY/帳簿/総勘定元帳_YYYY.csv
    YYYY/帳簿/決算書_YYYY.csv     青色申告決算書（一般用）の欄に合わせた損益計算書・貸借対照表
    YYYY/帳簿/転記シート_YYYY.csv 作成コーナーに入力する数字（決算書2〜4面・所得税・消費税）を
                                  書類と欄ごとに並べたもの。
                                  所得税は税金の計算（申告納税額・第3期分）まで。税率表は scripts/tax_rules.py
    帳簿.html                     全年度の決算書・仕訳帳・総勘定元帳をブラウザで見る版
                                  （証憑へのリンクつき・入金状況と年次推移つき・外部に公開しない）

過年度（申告済み）は、申告書の控えから転記した YYYY/帳簿/決算書_YYYY.csv だけを置く。
出典が「申告済み」で始まる年は生成し直さない（帳簿.html の表示にだけ使う）。

仕訳の前提:
    - 経費は個人のお金で払った扱い（貸方 事業主借）。証憑の金額は税込（税込経理）
    - 売上は月度の末日に 売掛金/売上高、入金日に 事業主貸/売掛金。
      入金先の口座は個人の口座として扱う（期首残高の普通預金は期首に事業主貸へ振り替える）。
      入金日は 入金日_YYYY.csv にあればその日、なければ支払期限（月度の翌月末）で仮置きする
    - 水道光熱費・地代家賃は総額で計上し、期末に家事分を事業主貸へ振り替える（按分率はサマリー）
    - 消費税は簡易課税だけに対応（みなし仕入率は scripts/tax_rules.py の DEEMED_PURCHASE_RATE）
    - 減価償却費はサマリーの年額を期末に計上する（直接法）

出力はすべてメモリ上で作って検算し、問題がなければ最後にまとめて書き出す。
値が1つでも読めない・計算が合わないときは、何も書かずに中止する。
"""

import calendar
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True  # scripts/__pycache__ を作らない
sys.path.insert(0, str(Path(__file__).resolve().parent))
# 証憑ファイル名の解釈は台帳と共通にする（二重管理しない）
from rebuild_ledger import AMOUNT_RE, CURRENCIES, EXCLUDE_MARKERS, IMAGE_EXT  # noqa: E402
# 税率表・税額の計算はダッシュボードと共通にする
from tax_rules import (DEEMED_PURCHASE_RATE, donation_deduction, income_tax,  # noqa: E402
                       income_tax_bracket, reconstruction_tax, taxable_income)

ROOT = Path(__file__).resolve().parent.parent

OWNER_PAID = "事業主借"        # 個人のお金で払った経費の貸方
OWNER_DRAW = "事業主貸"        # 家事分の振替先
# 売上の入金先。入金先の口座を個人の口座として扱うなら事業主貸。
# 事業専用の口座で受けるなら "普通預金" にし、その口座のすべての出入りを仕訳にして通帳と照合する
DEPOSIT_ACCOUNT = OWNER_DRAW
# 個人の口座として扱う預金。期首残高にあれば期首に事業主貸へ振り替え、期中の仕訳には使わせない
PRIVATE_BANK = "普通預金" if DEPOSIT_ACCOUNT == OWNER_DRAW else None
DEPRECIATION_ACCOUNT = "工具器具備品"  # 償却資産は1件だけに対応（サマリーの【固定資産・減価償却】）

# 期末に家事分を振り替える科目 → 按分率を読むサマリーのセクション
PRORATED = {"水道光熱費": "水道光熱費", "地代家賃": "地代家賃"}

# 青色申告決算書（一般用）1面の経費欄。ここにない科目は ㉕〜㉚ の空欄に書く
FORM_EXPENSES = [
    ("⑧", "租税公課"), ("⑨", "荷造運賃"), ("⑩", "水道光熱費"), ("⑪", "旅費交通費"),
    ("⑫", "通信費"), ("⑬", "広告宣伝費"), ("⑭", "接待交際費"), ("⑮", "損害保険料"),
    ("⑯", "修繕費"), ("⑰", "消耗品費"), ("⑱", "減価償却費"), ("⑲", "福利厚生費"),
    ("⑳", "給料賃金"), ("㉑", "外注工賃"), ("㉒", "利子割引料"), ("㉓", "地代家賃"),
    ("㉔", "貸倒金"),
]
FREE_SLOTS = ["㉕", "㉖", "㉗", "㉘", "㉙", "㉚"]
# 空欄に書く順。前年の決算書と同じ欄になるように並べる
FREE_ORDER = ["会議費", "新聞図書費", "支払手数料", "保険料", "諸会費"]

# 4面 貸借対照表の科目順
BS_ASSETS = ["現金", "当座預金", "定期預金", "その他の預金", "普通預金", "受取手形", "売掛金",
             "有価証券", "棚卸資産", "前払金", "貸付金", "建物", "建物附属設備", "機械装置",
             "車両運搬具", "工具器具備品", "土地"]
BS_LIABILITIES = ["支払手形", "買掛金", "借入金", "未払金", "前受金", "預り金", "貸倒引当金"]
REVENUES = ["売上高", "雑収入"]

GENERATED = "scripts/build_books.py（帳簿から生成）"  # 決算書CSVの出典欄
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺"

# 証憑フォルダに置いてよい経費科目（ほかのフォルダがあれば中止）
EXPENSE_FOLDERS = ({name for _, name in FORM_EXPENSES} | set(FREE_ORDER) | {"雑費"})
# CSV・サマリーから計上する科目。証憑フォルダにあると二重計上になるので中止する
# （定期経費_YYYY.csv の借方の科目もここに加わる）
SCHEDULED = {"地代家賃", "減価償却費"}

# 同じ日付の仕訳を並べる順
ORDER = {"期首": 0, "売上": 1, "入金": 2, "経費": 3, "定期": 4, "手入力": 5, "決算整理": 9}
TENTATIVE = "（支払期限で仮置き）"  # 入金日を通帳で確かめていない入金の摘要の末尾


def die(msg):
    sys.exit(f"中止: {msg}\n（帳簿は書き出していません）")


def rel(p: Path) -> str:
    return str(p.relative_to(ROOT))


# これから書き出すCSV（パス → 行）。書き出す前の検算・転記シート・HTMLはここから読む
PENDING = {}


def exists(path: Path):
    return path in PENDING or path.exists()


def read_rows(path: Path):
    if path in PENDING:
        return PENDING[path]
    if not path.exists():
        die(f"{rel(path)} が見つからない")
    with path.open(encoding="utf-8") as fh:
        return list(csv.reader(fh))


def stage(path: Path, rows):
    """書き出すCSVを PENDING に置く。読み直したときと同じく、セルは文字列にしておく。"""
    PENDING[path] = [["" if c is None else str(c) for c in r] for r in rows]


def write_csv(path: Path, rows):
    with path.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)


def need_int(s, where):
    v = to_int(s)
    if v is None:
        die(f"{where} の金額が読めない: {s!r}")
    return v


def to_int(s):
    s = (s or "").replace(",", "").replace("¥", "").replace("%", "").strip()
    return int(s) if re.fullmatch(r"-?\d+", s) else None


def month_end(y, m):
    return date(y, m, calendar.monthrange(y, m)[1])


def next_month_end(y, m):
    return month_end(y + 1, 1) if m == 12 else month_end(y, m + 1)


def round_half_up(n, pct):
    """n × pct% を1円未満四捨五入する（サマリーの按分と同じ丸め）。"""
    return (n * pct + 50) // 100


# ---------------------------------------------------------------- サマリー
class Summary:
    def __init__(self, year):
        self.path = ROOT / f"{year}/確定申告サマリー_{year}.csv"
        self.rows = read_rows(self.path)

    def section(self, name):
        out, inside = [], False
        for r in self.rows:
            if r and r[0].startswith("【"):
                inside = r[0].startswith(f"【{name}")
                continue
            if inside and r and not r[0].startswith("※"):
                out.append(r)
        if not out:
            die(f"{rel(self.path)} に『【{name}】』のセクションが見つからない")
        return out

    def value(self, section, label, text=False):
        for r in self.section(section):
            if len(r) >= 2 and (r[0] == label or r[0].startswith(label + "（")):
                v = r[1].strip() if text else to_int(r[1])
                if v not in (None, ""):
                    return v
        die(f"{rel(self.path)} の【{section}】に『{label}』の値が見つからない")


# ---------------------------------------------------------------- 仕訳を集める
def entry(d, dr, cr, amount, memo, evidence, kind):
    if not isinstance(amount, int) or amount <= 0:
        die(f"金額が正の整数でない仕訳: {d} {dr}/{cr} {amount} {memo}")
    return {"date": d, "dr": dr, "cr": cr, "amount": amount, "memo": memo,
            "evidence": evidence, "kind": kind}


def collect_expenses(year, end_month, scheduled):
    """経費の証憑を1ファイル1仕訳にする。読めないファイルがあれば中止。"""
    entries, unparsable, excluded, outside = [], [], 0, 0
    for m in range(1, 13):
        month_dir = ROOT / f"{year}/{m:02d}"
        if not month_dir.is_dir():
            continue
        for cat_dir in sorted(p for p in month_dir.iterdir()
                              if p.is_dir() and not p.name.startswith(".")):
            if cat_dir.name in scheduled:
                die(f"{rel(cat_dir)}: {cat_dir.name}はCSV・サマリーから計上するので、"
                    "証憑フォルダに置くと二重計上になる")
            if cat_dir.name not in EXPENSE_FOLDERS:
                die(f"{rel(cat_dir)}: 経費の科目として知らないフォルダ名"
                    "（EXPENSE_FOLDERS に足すか、置き場所を直す）")
            for f in sorted(cat_dir.iterdir()):
                if f.name.startswith("."):
                    continue
                if f.suffix not in IMAGE_EXT:
                    unparsable.append(f)
                    continue
                if m > end_month:
                    outside += 1
                    continue
                if any(mk in f.stem for mk in EXCLUDE_MARKERS):
                    excluded += 1
                    continue
                head = re.match(r"(\d{4})(\d{2})(\d{2})_", f.stem)
                amt = AMOUNT_RE.search(f.stem)
                if amt and amt.group(2).upper() in CURRENCIES:
                    amt = None  # 外貨のまま（円に換算していない）
                try:
                    d = date(*map(int, head.groups())) if head else None
                except ValueError:
                    d = None
                if not d or not amt or d.year != year:
                    unparsable.append(f)
                    continue
                vendor = f.stem[9:amt.start()]
                entries.append(entry(d, cat_dir.name, OWNER_PAID, int(amt.group(1)),
                                     vendor, rel(f), "経費"))
    if unparsable:
        die("ファイル名から日付・金額を読めない、または対応していない拡張子の証憑がある"
            "（jpg/jpeg/png/pdf のみ）:\n  "
            + "\n  ".join(rel(f) for f in unparsable))
    return entries, excluded, outside


def find_invoice(year, client, m, amount, used):
    """月度 m・金額が一致する請求書を探す。請求日は月度の20日〜翌月末。"""
    lo, hi = date(year, m, 20), next_month_end(year, m)
    folder = client.replace(" ", "")
    for y in {lo.year, hi.year}:
        d = ROOT / f"{y}/売上/{folder}"
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            mm = re.match(r"(\d{4})(\d{2})(\d{2})_[^_]+_(\d+)$", f.stem)
            if not mm or f in used or int(mm.group(4)) != amount:
                continue
            if lo <= date(*map(int, mm.groups()[:3])) <= hi:
                used.add(f)
                return rel(f)
    return ""


def load_receipt_dates(year):
    """通帳で確かめた売上の入金日（任意）。キーは (月度 'YYYY/MM', 取引先)。"""
    path = ROOT / f"{year}/帳簿/入金日_{year}.csv"
    dates = {}
    if not path.exists():
        return dates, path
    for r in read_rows(path)[1:]:
        if not r or r[0].startswith("※"):
            continue
        mm = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", r[2].strip()) if len(r) >= 3 else None
        key = (r[0].strip(), r[1].strip())
        if not mm or not re.fullmatch(r"\d{4}/\d\d", key[0]) or key in dates:
            die(f"{rel(path)} の行が読めないか、同じ月度・取引先が2回ある: {','.join(r)}")
        dates[key] = date(*map(int, mm.groups()))
    return dates, path


def receipt_entry(receipts, month_key, client, earliest, due, period_end, v, memo, evidence):
    """売掛金の入金。入金日_YYYY.csv の日付があればその日、なければ支払期限で仮置きする。
    入金日が期間の末日より後なら、まだ入金していない（売掛金のまま）ので仕訳を作らない。"""
    paid = receipts.get((month_key, client))
    if paid is not None and paid < earliest:
        die(f"{client} {month_key}月度の入金日 {paid:%Y/%m/%d} が {earliest:%Y/%m/%d} より前"
            "（売上の計上前の入金＝前受金や、前年中の入金は未対応）")
    d = paid or due
    if d > period_end:
        return []
    return [entry(d, DEPOSIT_ACCOUNT, "売掛金", v, f"{memo} 入金" + ("" if paid else TENTATIVE),
                  evidence, "入金")]


def collect_sales(year, end_month, period_end, warnings, receipts=None, known=None):
    """売上と入金の仕訳。known には入金日の照合用に (月度, 取引先) を全月分入れる。"""
    receipts = receipts or {}
    known = set() if known is None else known
    path = ROOT / f"{year}/売上高_{year}.csv"
    rows = read_rows(path)
    clients = rows[0][1:-1]
    entries, used, posted = [], set(), []
    for r in rows[1:]:
        mm = re.match(rf"{year}/(\d\d)$", r[0]) if r else None
        if not mm:
            continue
        m = int(mm.group(1))
        vals = [to_int(x) for x in r[1:1 + len(clients)]]
        if None in vals or to_int(r[-1]) != sum(vals):
            die(f"{rel(path)} の {r[0]} の金額が読めないか、合計が合わない")
        known.update((r[0], c) for c, v in zip(clients, vals) if v)
        if m > end_month:
            continue
        for client, v in zip(clients, vals):
            if not v:
                continue
            posted.append(m)
            inv = find_invoice(year, client, m, v, used)
            if not inv:
                warnings.append(f"請求書が見つからない: {client} {m}月度 {v:,}円")
            sold = month_end(year, m)
            entries.append(entry(sold, "売掛金", "売上高", v,
                                 f"{client} {m}月度", inv or rel(path), "売上"))
            entries += receipt_entry(receipts, r[0], client, sold, next_month_end(year, m),
                                     period_end, v, f"{client} {m}月度", inv or rel(path))
    return entries, max(posted, default=0)


def collect_opening_receivables(year, opening_assets, period_end, receipts, known):
    """期首の売掛金（前年12月度の売上）を、入金日（なければ支払期限の1月末）に消し込む。"""
    amount = opening_assets.get("売掛金", 0)
    if not amount:
        return []
    path = ROOT / f"{year - 1}/売上高_{year - 1}.csv"
    rows = read_rows(path)
    clients = rows[0][1:-1]
    key = f"{year - 1}/12"
    dec = next((r for r in rows[1:] if r and r[0] == key), None)
    vals = [to_int(x) for x in dec[1:1 + len(clients)]] if dec else [None]
    if None in vals or sum(vals) != amount:
        die(f"期首の売掛金 {amount:,} が {rel(path)} の12月度の売上と合わない")
    known.update((key, c) for c, v in zip(clients, vals) if v)
    out = []
    for c, v in zip(clients, vals):
        if v:
            out += receipt_entry(receipts, key, c, date(year, 1, 1), month_end(year, 1),
                                 period_end, v, f"{c} {year - 1}年12月度", rel(path))
    return out


def collect_opening_transfers(year, opening_assets):
    """個人の口座として扱う預金の期首残高を、期首に事業主貸へ振り替える。"""
    v = opening_assets.get(PRIVATE_BANK, 0) if PRIVATE_BANK else 0
    if v < 0:
        die(f"期首残高の{PRIVATE_BANK}が負の値 {v:,}")
    if not v:
        return []
    return [entry(date(year, 1, 1), OWNER_DRAW, PRIVATE_BANK, v,
                  f"{PRIVATE_BANK}を個人の口座として扱うため、期首残高を事業主貸へ振替",
                  rel(ROOT / f"{year}/帳簿/期首残高_{year}.csv"), "期首")]


def recurring_path(year):
    return ROOT / f"{year}/定期経費_{year}.csv"


def load_recurring(year):
    """領収書が届かない定期の経費（任意）。行は (月, 金額, 借方, 貸方, 摘要)。ファイルがなければ空。"""
    path = recurring_path(year)
    if not path.exists():
        return []
    rows = []
    for r in read_rows(path)[1:]:
        if not r or not r[0].strip() or r[0].startswith("※"):
            continue
        mm = re.fullmatch(r"(\d{1,2})月", r[0].strip())
        v = to_int(r[1]) if len(r) > 1 else None
        if (not mm or not 1 <= int(mm.group(1)) <= 12 or v is None or v < 0
                or len(r) < 5 or not r[2].strip() or not r[3].strip()):
            die(f"{rel(path)} の行が読めない: {','.join(r)}")
        if r[2].strip() in SCHEDULED or r[2].strip() in PRORATED:
            die(f"{rel(path)}: 借方『{r[2].strip()}』は確定申告サマリーから計上する科目なので、定期経費に書かない")
        rows.append((int(mm.group(1)), v, r[2].strip(), r[3].strip(), r[4].strip()))
    return rows


def collect_recurring(year, end_month, recurring):
    """定期経費を月末に計上する。金額0円の行は仕訳を作らない。"""
    return [entry(month_end(year, m), dr, cr, v, f"{memo} {m}月分", rel(recurring_path(year)), "定期")
            for m, v, dr, cr, memo in recurring if m <= end_month and v]


def collect_rent(year, end_month, summary):
    gross = summary.value("地代家賃", "家賃月額（総額）")
    if not gross:
        return []
    payee = summary.value("地代家賃", "支払先", text=True)
    return [entry(month_end(year, m), "地代家賃", OWNER_PAID, gross,
                  f"{payee} {m}月分家賃", rel(summary.path), "定期")
            for m in range(1, end_month + 1)]


def collect_manual(year, period_end):
    path = ROOT / f"{year}/帳簿/手入力仕訳_{year}.csv"
    if not path.exists():
        return [], 0
    entries, outside = [], 0
    for r in read_rows(path)[1:]:
        if not r or r[0].startswith("※"):
            continue
        mm = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", r[0].strip())
        if not mm or len(r) < 5 or to_int(r[3]) is None:
            die(f"{rel(path)} の行が読めない: {','.join(r)}")
        d = date(*map(int, mm.groups()))
        if d > period_end:
            outside += 1
            continue
        entries.append(entry(d, r[1].strip(), r[2].strip(), to_int(r[3]), r[4].strip(),
                             r[5].strip() if len(r) > 5 else rel(path), "手入力"))
    return entries, outside


def closing_entries(entries, period_end, summary, opening):
    """期末の決算整理: 家事分の振替と減価償却。"""
    out = []
    for account, section in PRORATED.items():
        gross = sum(e["amount"] for e in entries if e["dr"] == account)
        pct = summary.value(section, "事業按分率")
        private = gross - round_half_up(gross, pct)
        if private:
            out.append(entry(period_end, OWNER_DRAW, account, private,
                             f"家事按分 {account} 総額{gross:,}円の{100 - pct}%",
                             rel(summary.path), "決算整理"))
    year = period_end.year
    dep = summary.value("固定資産・減価償却", f"{year}年減価償却費")
    asset = summary.value("固定資産・減価償却", "資産名", text=True)
    if dep > opening.get(DEPRECIATION_ACCOUNT, 0):
        die(f"減価償却費 {dep:,} が {DEPRECIATION_ACCOUNT} の期首残高を超えている")
    if dep:
        out.append(entry(period_end, "減価償却費", DEPRECIATION_ACCOUNT, dep,
                         f"{asset} 定額法 年額", rel(summary.path), "決算整理"))
    return out


def load_opening(year):
    path = ROOT / f"{year}/帳簿/期首残高_{year}.csv"
    assets, liabilities = {}, {}
    for r in read_rows(path)[1:]:
        if not r or r[0].startswith("※"):
            continue
        v = to_int(r[2]) if len(r) > 2 else None
        if v is None or r[0] not in ("資産", "負債") or not r[1]:
            die(f"{rel(path)} の行が読めない: {','.join(r)}")
        name = r[1]
        if name in (OWNER_DRAW, OWNER_PAID, "元入金"):
            die(f"{rel(path)}: {name}は書かない。事業主貸・事業主借は期首に元入金へ振り替わり、"
                "元入金は 資産−負債 で自動計算する")
        expected = "資産" if r[0] == "資産" else "負債・資本"
        if kind_of(name) != expected or name in assets or name in liabilities:
            die(f"{rel(path)}: 『{name}』は{r[0]}の科目ではないか、2回書かれている")
        (assets if r[0] == "資産" else liabilities)[name] = v
    return assets, liabilities


# ---------------------------------------------------------------- 集計
def kind_of(account):
    """科目の区分。資産・負債・収益の一覧にない科目は費用とみなす。"""
    if account in BS_ASSETS or account == OWNER_DRAW:
        return "資産"
    if account in BS_LIABILITIES or account in (OWNER_PAID, "元入金"):
        return "負債・資本"
    if account in REVENUES:
        return "収益"
    return "費用"


def classify(account, expense_accounts):
    kind = kind_of(account)
    if kind == "費用" and account not in expense_accounts:
        die(f"勘定科目『{account}』の区分が分からない（BS_ASSETS 等に追加する）")
    return kind


def debit_normal(kind):
    return kind in ("資産", "費用")


def assign_pl_lines(expense_totals):
    """経費科目を決算書1面の欄に割り当てる。"""
    fixed = {name for _, name in FORM_EXPENSES}
    extra = [a for a in expense_totals if a not in fixed and a != "雑費"]
    extra.sort(key=lambda a: (FREE_ORDER.index(a) if a in FREE_ORDER else 99, a))
    if len(extra) > len(FREE_SLOTS):
        die(f"決算書の空欄（㉕〜㉚）が足りない: {extra}")
    lines = [(no, name, expense_totals.get(name, 0)) for no, name in FORM_EXPENSES]
    lines += [(FREE_SLOTS[i], name, expense_totals[name]) for i, name in enumerate(extra)]
    lines.append(("㉛", "雑費", expense_totals.get("雑費", 0)))
    return lines


def main():
    if len(sys.argv) != 2 or not re.fullmatch(r"\d{4}(/\d{1,2})?", sys.argv[1].strip("/")):
        sys.exit(__doc__)
    parts = sys.argv[1].strip("/").split("/")
    year = int(parts[0])
    kessan = ROOT / f"{year}/帳簿/決算書_{year}.csv"
    if kessan.exists() and kessan_source(kessan).startswith("申告済み"):
        die(f"{rel(kessan)} は申告済みの決算書から転記したもの。生成し直さない")
    summary = Summary(year)
    warnings = []

    # 期間の最終月: 指定がなければ売上を計上済みの最終月（サマリーと同じく売上と期間を揃える）
    _, last_sales = collect_sales(year, 12, date(year, 12, 31), [])
    end_month = int(parts[1]) if len(parts) == 2 else last_sales
    if not 1 <= end_month <= 12:
        die("期間の最終月が決まらない（売上が1件もない）")
    period_end = month_end(year, end_month)
    if period_end > date.today():
        die(f"期間の末日 {period_end:%Y/%m/%d} がまだ来ていない。"
            "家賃など未発生の取引を帳簿に書かないため中止する")
    if summary.value("専従者給与・扶養控除", "専従者給与"):
        die("専従者給与が0円でない。このスクリプトは専従者給与（決算書㊳）に対応していない")

    opening_assets, opening_liab = load_opening(year)
    receipts, receipts_path = load_receipt_dates(year)
    known = set()
    sales, _ = collect_sales(year, end_month, period_end, warnings, receipts, known)
    sales += collect_opening_receivables(year, opening_assets, period_end, receipts, known)
    unknown = sorted(set(receipts) - known)
    if unknown:
        die(f"{rel(receipts_path)} に売上のない月度・取引先がある: {unknown[0][0]} {unknown[0][1]}")
    recurring = load_recurring(year)
    expenses, excluded, outside = collect_expenses(
        year, end_month, SCHEDULED | {dr for _, _, dr, _, _ in recurring})
    manual, manual_outside = collect_manual(year, period_end)
    entries = (collect_opening_transfers(year, opening_assets) + sales + expenses
               + collect_recurring(year, end_month, recurring)
               + collect_rent(year, end_month, summary) + manual)
    entries += closing_entries(entries, period_end, summary, opening_assets)
    stray = [e for e in entries if PRIVATE_BANK in (e["dr"], e["cr"]) and e["kind"] != "期首"]
    if stray:
        die(f"{PRIVATE_BANK}は個人の口座として扱っている（DEPOSIT_ACCOUNT）。"
            f"{stray[0]['evidence']} の『{stray[0]['memo']}』の{PRIVATE_BANK}を、"
            f"個人のお金で払ったなら{OWNER_PAID}、個人の口座に入ったなら{OWNER_DRAW}に直す")
    entries.sort(key=lambda e: (e["date"], ORDER[e["kind"]], e["evidence"], e["memo"]))
    for i, e in enumerate(entries, 1):
        e["no"] = i

    # --- 勘定科目ごとに集計 ---
    expense_accounts = ({e["dr"] for e in expenses} | {name for _, name in FORM_EXPENSES}
                        | set(FREE_ORDER) | {"雑費"})
    dr_sum, cr_sum = defaultdict(int), defaultdict(int)
    for e in entries:
        dr_sum[e["dr"]] += e["amount"]
        cr_sum[e["cr"]] += e["amount"]
    capital = sum(opening_assets.values()) - sum(opening_liab.values())
    opening = {**opening_assets, **opening_liab, "元入金": capital}
    accounts = set(dr_sum) | set(cr_sum) | set(opening)
    kinds = {a: classify(a, expense_accounts) for a in accounts}
    for a in opening_assets:
        if kinds[a] != "資産":
            die(f"期首残高の『{a}』は資産の科目ではない")

    def balance(a):
        sign = 1 if debit_normal(kinds[a]) else -1
        return opening.get(a, 0) + sign * (dr_sum[a] - cr_sum[a])

    revenue = sum(balance(a) for a in accounts if kinds[a] == "収益")
    sales_total = balance("売上高") if "売上高" in accounts else 0  # 雑収入を除いた課税売上
    expense_totals = {a: balance(a) for a in accounts if kinds[a] == "費用" and balance(a)}
    income = revenue - sum(expense_totals.values())
    assets_end = sum(balance(a) for a in accounts if kinds[a] == "資産")
    liab_end = sum(balance(a) for a in accounts if kinds[a] == "負債・資本")
    if sum(dr_sum.values()) != sum(cr_sum.values()) or assets_end != liab_end + income:
        die(f"貸借が一致しない（資産 {assets_end:,} / 負債・資本＋所得 {liab_end + income:,}）")

    pl_lines = assign_pl_lines(expense_totals)
    expense_total = sum(v for _, _, v in pl_lines)
    blue = min(summary.value("青色申告特別控除", "青色申告特別控除"), max(income, 0))
    order = [a for a in (BS_ASSETS + [OWNER_DRAW] + BS_LIABILITIES + [OWNER_PAID, "元入金"]
                         + REVENUES + [name for _, name, _ in pl_lines])
             if a in accounts]
    if set(order) != accounts:
        die(f"総勘定元帳に並べられない科目がある: {sorted(accounts - set(order))}")

    # --- サマリーとの照合（サマリーの値が None ならサマリーに行がない） ---
    expense_rows = {re.sub(r"（.*）$", "", r[0]): to_int(r[1])
                    for r in summary.section("経費") if len(r) >= 2 and to_int(r[1]) is not None}
    checks = [("売上高", revenue, summary.value("売上高", "年間売上高"))]
    checks += [(a, expense_totals.get(a, 0), v) for a, v in expense_rows.items() if a != "経費合計"]
    checks += [(a, expense_totals[a], None) for a in sorted(set(expense_totals) - set(expense_rows))]
    checks.append(("経費合計", expense_total, expense_rows.get("経費合計")))
    diffs = [(a, b, s) for a, b, s in checks if b != s]

    notes = [f"同一取引の別証憑として除外: {excluded}件"] if excluded else []
    if outside:
        notes.append(f"期間外（{end_month + 1}月以降）の証憑: {outside}件（未計上）")
    if manual_outside:
        notes.append(f"期間外の手入力仕訳: {manual_outside}件（未計上）")
    notes.append(f"売上の入金先は {DEPOSIT_ACCOUNT}"
                 + ("（入金先の口座は個人の口座として扱う）" if PRIVATE_BANK else ""))
    tentative = sum(1 for e in entries if e["kind"] == "入金" and e["memo"].endswith(TENTATIVE))
    if tentative:
        notes.append(f"入金日を支払期限で仮置きしている入金: {tentative}件"
                     f"（通帳の入金日を {rel(receipts_path)} に書くと直る）")
    notes += warnings

    # --- 出力をメモリ上で作る（書き出しは最後） ---
    out_dir = ROOT / f"{year}/帳簿"
    period = f"{year}/01/01〜{period_end:%Y/%m/%d}" + ("" if end_month == 12 else "（期中・暫定）")
    paths = {name: out_dir / f"{name}_{year}.csv" for name in ("仕訳帳", "総勘定元帳", "決算書", "転記シート")}

    total = sum(e["amount"] for e in entries)
    stage(paths["仕訳帳"], [["番号", "日付", "借方科目", "借方金額", "貸方科目", "貸方金額", "摘要", "証憑"]]
          + [[e["no"], f"{e['date']:%Y/%m/%d}", e["dr"], e["amount"], e["cr"], e["amount"],
              e["memo"], e["evidence"]] for e in entries]
          + [["合計", "", "", total, "", total, f"期間 {period}", ""]])

    ledger = [["勘定科目", "日付", "番号", "相手科目", "摘要", "借方", "貸方", "残高"]]
    for a in order:
        sign = 1 if debit_normal(kinds[a]) else -1
        run = opening.get(a, 0)
        if a in opening:
            ledger.append([a, f"{year}/01/01", "", "", "前期繰越", "", "", run])
        for e in entries:
            if a not in (e["dr"], e["cr"]):
                continue
            is_dr = e["dr"] == a
            run += sign * (e["amount"] if is_dr else -e["amount"])
            ledger.append([a, f"{e['date']:%Y/%m/%d}", e["no"], e["cr"] if is_dr else e["dr"],
                           e["memo"], e["amount"] if is_dr else "", "" if is_dr else e["amount"], run])
        ledger.append([a, "", "", "", "合計", dr_sum[a], cr_sum[a], run])
    stage(paths["総勘定元帳"], ledger)

    pl = ([("①", "売上（収入）金額", revenue), ("⑦", "差引金額", revenue)] + pl_lines
          + [("㉜", "経費 計", expense_total), ("㉝", "差引金額", revenue - expense_total),
             ("㊸", "青色申告特別控除前の所得金額", income), ("㊹", "青色申告特別控除額", blue),
             ("㊺", "所得金額", income - blue)])
    kessan_rows = [[f"{year}年分 青色申告決算書（一般用）の数字", "", "", ""],
                   [f"期間 {period}", "", "", f"出典 {GENERATED}"], [],
                   ["【損益計算書（1面）】", "", "", ""], ["欄", "科目", "金額", ""]]
    kessan_rows += [[no, name, v, ""] for no, name, v in pl]
    kessan_rows += [[], ["【貸借対照表（4面）】", "", "", ""], ["区分", "科目", "期首", "期末"]]
    kessan_rows += [["資産", a, opening.get(a, "") if a != OWNER_DRAW else "", balance(a)]
                    for a in order if kinds[a] == "資産"]
    kessan_rows.append(["資産", "合計", sum(opening_assets.values()), assets_end])
    kessan_rows += [["負債・資本", a, opening.get(a, 0) if a != OWNER_PAID else "", balance(a)]
                    for a in order if kinds[a] == "負債・資本"]
    kessan_rows += [["負債・資本", "青色申告特別控除前の所得金額", "", income],
                    ["負債・資本", "合計", sum(opening_assets.values()), liab_end + income],
                    [], ["【サマリーとの照合】", "", "", ""], ["項目", "帳簿", "サマリー", ""]]
    kessan_rows += [[a, b, "" if s is None else s, ""] for a, b, s in checks]
    kessan_rows += [[], ["【注記】", "", "", ""]] + [[f"※ {n}", "", "", ""] for n in notes]
    stage(paths["決算書"], kessan_rows)

    sheet_rows, prev_tax = filing_sheet_rows(year, end_month, read_kessan(paths["決算書"]),
                                             summary, sales_total)
    stage(paths["転記シート"], [SHEET_HEADER] + sheet_rows)
    html_path, html_text, years = build_html()

    # --- ここまで中止せずに来たら、まとめて書き出す ---
    for path in paths.values():
        write_csv(path, PENDING[path])
    html_path.write_text(html_text, encoding="utf-8")

    # --- 報告 ---
    print(f"期間 {period}")
    print(f"仕訳 {len(entries)}件 → {rel(out_dir)}/仕訳帳_{year}.csv・総勘定元帳_{year}.csv・決算書_{year}.csv")
    print(f"  転記シート: {rel(paths['転記シート'])}（作成コーナーに入力する数字）")
    print(f"  見やすい版: {rel(html_path)}（{len(years)}年分・ブラウザで開く）")
    if prev_tax:
        print(f"  消費税の計算式の確認: {year - 1}年分を同じ式で出すと "
              f"{prev_tax['national'] + prev_tax['local']:,}（国税 {prev_tax['national']:,}）")
    print(f"  売上 {revenue:,} / 経費 {expense_total:,} / 青色申告特別控除前の所得 {income:,}")
    print(f"  資産合計（期末） {assets_end:,}（期首 {sum(opening_assets.values()):,}・元入金 {capital:,}）")
    print("\nサマリーとの照合: " + ("すべて一致" if not diffs else f"{len(diffs)}件の不一致"))
    for a, b, s in diffs:
        print(f"  {a}: 帳簿 {b:,} / サマリー {'行がない' if s is None else f'{s:,}'}")
    for n in notes:
        print(f"※ {n}")


# ---------------------------------------------------------------- 作成コーナーへの転記シート
SHEET_HEADER = ["書類", "区分", "欄", "項目", "金額", "内容", "出典", "状態"]
INTERIM_LIMIT = 480000  # 前年の消費税額（国税）がこれを超えると中間申告が要る（年1回）


def floor_to(n, unit):
    return n // unit * unit


def consumption_tax(sales_incl_tax):
    """簡易課税の消費税額（みなし仕入率は DEEMED_PURCHASE_RATE）。売上は全額10%課税の税込額。"""
    base = floor_to(sales_incl_tax * 100 // 110, 1000)   # 課税標準額（千円未満切捨）
    tax = base * 78 // 1000                              # 消費税額（国税7.8%）
    deduction = tax * DEEMED_PURCHASE_RATE // 100        # 控除対象仕入税額
    national = floor_to(tax - deduction, 100)            # 差引税額（百円未満切捨）
    local = floor_to(national * 22 // 78, 100)           # 地方消費税（譲渡割額）
    return {"base": base, "tax": tax, "deduction": deduction,
            "national": national, "local": local}


def taxable_sales(year):
    """その年の課税売上（税込）。帳簿があれば売上高の勘定だけ（雑収入は除く）、
    なければ決算書の①（過年度の①は売上だけで、雑収入を含まない）。なければ None。"""
    journal = ROOT / f"{year}/帳簿/仕訳帳_{year}.csv"
    if exists(journal):
        rows = [r for r in read_rows(journal)[1:] if r and r[0] != "合計"]
        return (sum(int(r[5]) for r in rows if r[4] == "売上高")
                - sum(int(r[3]) for r in rows if r[2] == "売上高"))
    path = ROOT / f"{year}/帳簿/決算書_{year}.csv"
    if not exists(path):
        return None
    return next((v for n, _, v in read_kessan(path)["pl"] if n == "①"), None)


def filing_sheet_rows(year, end_month, kessan, summary, sales_total):
    """決算書・所得税・消費税・添付書類の数字を、書類と欄ごとに並べる（作成コーナーへの転記用）。"""
    interim = end_month != 12
    tentative = f"暫定（1-{end_month}月）" if interim else ""
    pl = {no: v for no, _, v in kessan["pl"]}
    kpath = rel(ROOT / f"{year}/帳簿/決算書_{year}.csv")
    spath = rel(summary.path)
    rows = []

    def add(doc, part, no, item, amount=None, text="", src="", state=None):
        rows.append([doc, part, no, item, "" if amount is None else amount, text, src,
                     tentative if state is None else state])

    # --- 青色申告決算書 ---
    doc = "青色申告決算書（一般用）"
    for no, name, v in kessan["pl"]:
        if v or no in ("①", "⑦", "㉜", "㉝", "㊸", "㊹", "㊺"):
            add(doc, "1面 損益計算書", no, name, v, src=kpath)

    sales_path = ROOT / f"{year}/売上高_{year}.csv"
    monthly = {}
    for r in read_rows(sales_path)[1:]:
        mm = re.match(rf"{year}/(\d\d)$", r[0]) if r else None
        if mm:
            monthly[int(mm.group(1))] = to_int(r[-1]) or 0
    for m in range(1, 13):
        add(doc, "2面 月別売上（収入）金額", f"{m}月", "売上（収入）金額",
            monthly.get(m, 0) if m <= end_month else 0, src=rel(sales_path),
            state=None if m <= end_month else "未計上")
    other_income = pl["①"] - sales_total
    if other_income:
        add(doc, "2面 月別売上（収入）金額", "", "雑収入", other_income, "売上以外の収入（帳簿の雑収入）", kpath)
    add(doc, "2面 月別売上（収入）金額", "計", "売上（収入）金額", pl["①"], src=kpath)

    add(doc, "2面 専従者給与の内訳", "", "専従者給与", None, "なし", spath, "")

    gross = summary.value("地代家賃", "家賃月額（総額）")
    rent = next((v for _, name, v in kessan["pl"] if name == "地代家賃"), 0)
    part = "2面 地代家賃の内訳"
    add(doc, part, "", "支払先の住所・氏名", None,
        f"{summary.value('地代家賃', '支払先住所', text=True)} "
        f"{summary.value('地代家賃', '支払先', text=True)}", spath, "")
    add(doc, part, "", "賃借物件", None, summary.value("地代家賃", "賃借物件の種別", text=True), spath, "")
    add(doc, part, "", "本年中の賃借料・権利金等（賃）", gross * end_month,
        f"月{gross:,}円 × {end_month}か月", spath)
    add(doc, part, "", "左の賃借料のうち必要経費算入額", rent,
        f"事業按分 {summary.value('地代家賃', '事業按分率')}%", kpath)

    part = "2面 青色申告特別控除額の計算"
    add(doc, part, "⑦", "青色申告特別控除前の所得金額", pl["㊸"], src=kpath)
    add(doc, part, "⑨", "青色申告特別控除額", pl["㊹"],
        "上限65万円（複式簿記で期限内に申告し、e-Tax か優良な電子帳簿の要件を満たす場合）", kpath)

    part = "3面 売上（収入）金額の明細"
    clients_path = ROOT / f"{year}/売上先一覧_{year}.csv"
    listed = 0
    for r in read_rows(clients_path)[1:]:
        if r and r[0] not in ("合計", "") and not r[0].startswith("※"):
            v = need_int(r[3] if len(r) > 3 else "", f"{rel(clients_path)} の {r[0]}")
            listed += v
            add(doc, part, "", r[0], v, f"{r[1]}／登録番号（法人番号）{r[2]}", rel(clients_path),
                None if v and listed <= sales_total else "要確認")
    if listed != sales_total:
        add(doc, part, "", "売上先の合計と帳簿の売上高が合わない", sales_total - listed,
            f"売上先一覧 {listed:,} / 帳簿の売上高 {sales_total:,}。売上先一覧を更新する", rel(clients_path), "要確認")
    if other_income:
        add(doc, part, "", "上記以外の売上先の計（雑収入を含む）", other_income, src=kpath)
    add(doc, part, "計", "売上（収入）金額", pl["①"], src=kpath)

    part = "3面 減価償却費の計算"
    sec = "固定資産・減価償却"
    dep = summary.value(sec, f"{year}年減価償却費")
    asset_row = next((r for r in kessan["bs"] if r[1] == DEPRECIATION_ACCOUNT), None)
    opening_book, closing = (asset_row[2], asset_row[3]) if asset_row else (None, None)
    add(doc, part, "", "減価償却資産の名称等", None, summary.value(sec, "資産名", text=True), spath, "")
    add(doc, part, "", "面積又は数量", 1, src=spath, state="")
    add(doc, part, "", "取得年月", None, summary.value(sec, "取得日", text=True), spath, "")
    add(doc, part, "イ", "取得価額", summary.value(sec, "取得価額"), src=spath, state="")
    add(doc, part, "ロ", "償却の基礎になる金額", summary.value(sec, "取得価額"), "定額法は取得価額", spath, "")
    add(doc, part, "", "償却方法・耐用年数・償却率", None,
        f"{summary.value(sec, '償却方法', text=True)}・{summary.value(sec, '耐用年数', text=True)}", spath, "")
    add(doc, part, "ニ", "本年中の償却期間", None,
        f"{summary.value(sec, '事業供用月数', text=True)}／12", spath, "")
    add(doc, part, "ホ", "本年分の普通償却費", dep, src=spath, state="")
    add(doc, part, "チ", "事業専用割合", None, "100%（事業専用の資産）", "", "")
    add(doc, part, "リ", "本年分の必要経費算入額", dep, src=spath, state="")
    add(doc, part, "ヌ", "未償却残高（期末残高）", closing,
        "" if opening_book is None else f"期首 {opening_book:,} − 本年分 {dep:,}", kpath, "")

    for side, name, opening, close in kessan["bs"]:
        add(doc, f"4面 貸借対照表（{side}）", "", name, close,
            "" if opening is None else f"期首 {opening:,}", kpath)

    # --- 所得税及び復興特別所得税の申告書 ---
    doc = "所得税及び復興特別所得税の申告書"
    prev_sales = taxable_sales(year - 1)
    prev = consumption_tax(prev_sales) if prev_sales is not None else None
    income = pl["㊺"]  # 総所得金額等（事業所得のみ）
    add(doc, "収入金額等", "", "事業 営業等", pl["①"], src=kpath)
    add(doc, "所得金額等", "", "事業 営業等", income, "青色申告特別控除後", kpath)
    add(doc, "所得金額等", "", "合計", income, "事業所得のみ", kpath)
    if prev and prev["national"] + prev["local"] and not any(
            name == "租税公課" and v for _, name, v in kessan["pl"]):
        add(doc, "所得金額等", "", "租税公課（前年分の消費税）", None,
            f"{year - 1}年分の消費税 {prev['national'] + prev['local']:,}（同じ式での試算）が経費に入っていない。"
            f"税込経理では納めた年の租税公課。納付日を確かめて 手入力仕訳_{year}.csv に入れると所得と税額が下がる",
            "", "要確認")

    def itemized(section):
        """サマリーのセクションの内訳（合計以外の行。3列目は内容）を並べて合計を返す。
        内訳の合計が『<セクション名>合計』の行と合わなければ中止する。"""
        part, total_label, items = f"所得から差し引かれる金額（{section}）", f"{section}合計", 0
        for r in summary.section(section):
            if len(r) < 2 or not r[0].strip() or r[0].startswith(total_label):
                continue
            v = need_int(r[1], f"{spath} の【{section}】{r[0]}")
            items += v
            add(doc, part, "", r[0], v, r[2] if len(r) > 2 else "", spath)
        total = summary.value(section, total_label)
        if items != total:
            die(f"{spath} の【{section}】の内訳の合計 {items:,} が{total_label} {total:,} と合わない")
        add(doc, part, "", f"{section} 合計", total, src=spath)
        return total

    shakai = itemized("社会保険料控除")
    shokibo = itemized("小規模企業共済等掛金控除")

    part = "所得から差し引かれる金額（医療費控除）"
    add(doc, part, "", "支払った医療費", summary.value("医療費控除", "医療費合計"),
        "医療費の明細は 医療費_YYYY.csv。年末に医療費通知などで確定", spath)
    add(doc, part, "", "保険金などで補填される金額", 0, src=spath)
    iryo = summary.value("医療費控除", "控除対象額")
    add(doc, part, "", "医療費控除額", iryo, "10万円（総所得金額等が200万円未満ならその5%）を差し引いた後", spath)

    part = "所得から差し引かれる金額（寄附金控除）"
    donations = ROOT / f"{year}/寄付金/寄付金_{year}.csv"
    total = 0
    for r in (read_rows(donations)[1:] if donations.exists() else []):  # 寄付金CSVは任意
        if r and re.fullmatch(r"\d+", r[0]):
            if len(r) < 4:
                die(f"{rel(donations)} の No.{r[0]} の列が足りない（No,寄付日,寄付先,金額,区分,受領証明書）")
            r = r + [""] * (6 - len(r))
            v = need_int(r[3], f"{rel(donations)} の No.{r[0]}")
            total += v
            add(doc, part, "", r[2], v, f"寄附日 {r[1]}／{r[4]}／受領証明書 {r[5]}",
                rel(donations), "")
    add(doc, part, "", "寄附金 合計", total,
        "確定申告をするとワンストップ特例は無効になるので、全件を入力する", rel(donations), "")
    kifu = donation_deduction(total, income)
    add(doc, part, "", "寄附金控除額", kifu, "寄附金 合計 − 2,000円（総所得金額等の40%が上限）",
        "scripts/tax_rules.py")

    part = "所得から差し引かれる金額（その他）"
    fuyo_v = summary.value("専従者給与・扶養控除", "扶養控除（所得税）")
    if fuyo_v:
        add(doc, part, "", "扶養控除", fuyo_v, "", spath, "")
    kiso = summary.value("基礎控除", "基礎控除")
    add(doc, part, "", "基礎控除", kiso, "自動で入る", spath, "")
    add(doc, part, "", "生命保険料控除・地震保険料控除", None,
        "このリポジトリに記録がない。該当があれば証明書から入力する", "", "要確認")

    # 今日締めたらこうなる、という税額（作成コーナーの計算と同じ順）。速算表は scripts/tax_rules.py
    part = "税金の計算"
    rules = "scripts/tax_rules.py"
    deductions = [("社会保険料", shakai), ("小規模企業共済等掛金", shokibo), ("医療費", iryo),
                  ("寄附金", kifu), ("扶養", fuyo_v), ("基礎", kiso)]
    ded_total = sum(v for _, v in deductions)
    add(doc, part, "", "所得から差し引かれる金額の合計", ded_total,
        "＋".join(f"{k} {v:,}" for k, v in deductions if v), spath)
    taxable = taxable_income(income, ded_total)
    add(doc, part, "", "課税される所得金額", taxable,
        f"所得金額 {income:,} − 所得控除 {ded_total:,}、千円未満切捨", kpath)
    rate, minus = income_tax_bracket(taxable)
    base = income_tax(taxable)
    add(doc, part, "", "上の課税される所得金額に対する税額", base,
        f"速算表 {taxable:,} × {rate}% − {minus:,}", rules)
    add(doc, part, "", "差引所得税額（基準所得税額）", base,
        "税額控除（配当控除・住宅借入金等特別控除など）なし", "")
    fukko = reconstruction_tax(base)
    add(doc, part, "", "復興特別所得税額", fukko, "基準所得税額 × 2.1%、1円未満切捨", rules)
    add(doc, part, "", "所得税及び復興特別所得税の額", base + fukko, "基準所得税額＋復興特別所得税額", "")
    add(doc, part, "", "源泉徴収税額", 0,
        "売上は請求額のまま入金されている（源泉徴収された売上があれば支払調書の額を入れる）", "")
    filed = (base + fukko) // 100 * 100
    add(doc, part, "", "申告納税額", filed, "百円未満切捨", "")
    prepaid = summary.value("予定納税", "予定納税額 合計")
    add(doc, part, "", "予定納税額（第1期・第2期）", prepaid, "予定納税額の通知書の合計欄", spath, "")
    add(doc, part, "", "第3期分の税額（納める税金）" if filed >= prepaid else "第3期分の税額（還付される税金）",
        abs(filed - prepaid), "申告納税額 − 予定納税額", "")

    part = "第二表 住民税・事業税に関する事項"
    add(doc, part, "", "事業税", None,
        "個人事業税はこのリポジトリでは計算していない（業種によって課税される。都道府県の案内で確かめる）",
        "", "要確認")

    # --- 消費税及び地方消費税の申告書（簡易課税） ---
    doc = "消費税及び地方消費税の申告書（簡易課税）"
    part = "基本情報"
    add(doc, part, "", "課税期間", None, f"{year}/01/01〜{year}/12/31", "", "")
    base_sales = taxable_sales(year - 2)
    if base_sales is not None:
        add(doc, part, "", f"基準期間（{year - 2}年）の課税売上高", base_sales * 100 // 110,
            f"{year - 2}年分の売上 {base_sales:,}（税込）× 100/110（その年も課税事業者だった前提）",
            rel(ROOT / f"{year - 2}/帳簿/決算書_{year - 2}.csv"), "")
    else:
        add(doc, part, "", f"基準期間（{year - 2}年）の課税売上高", None,
            f"{year - 2}年分の決算書CSVがない", "", "要確認")
    t = consumption_tax(sales_total)
    part = f"税額の計算（簡易課税・みなし仕入率{DEEMED_PURCHASE_RATE}%）"
    add(doc, part, "", "課税売上高（税込）", sales_total,
        "売上はすべて10%課税" + (f"。雑収入 {other_income:,} は不課税として除く" if other_income else ""), kpath)
    add(doc, part, "①", "課税標準額", t["base"], "税込売上 × 100/110、千円未満切捨", kpath)
    add(doc, part, "②", "消費税額", t["tax"], "課税標準額 × 7.8%", kpath)
    add(doc, part, "④", "控除対象仕入税額", t["deduction"], f"消費税額 × {DEEMED_PURCHASE_RATE}%", kpath)
    add(doc, part, "⑨", "差引税額", t["national"], "百円未満切捨", kpath)
    add(doc, part, "⑩", "中間納付税額", 0,
        f"前年分の消費税額（国税）{prev['national']:,} が{INTERIM_LIMIT // 10000}万円以下のため中間申告なし"
        if prev and prev["national"] <= INTERIM_LIMIT else "前年分の税額を確認する", "",
        "" if prev and prev["national"] <= INTERIM_LIMIT else "要確認")
    add(doc, part, "⑪", "納付税額（国税）", t["national"], src=kpath)
    add(doc, part, "", "地方消費税（譲渡割額）", t["local"], "差引税額 × 22/78、百円未満切捨", kpath)
    add(doc, part, "", "納付税額 合計", t["national"] + t["local"], src=kpath)

    return rows, prev


# ---------------------------------------------------------------- 全年度の閲覧用HTML
def kessan_source(path: Path):
    """決算書CSVの出典欄だけを読む（中身の検算はしない）。"""
    rows = read_rows(path)
    return rows[1][3].removeprefix("出典 ") if len(rows) > 1 and len(rows[1]) > 3 else ""


def read_kessan(path: Path):
    """決算書CSVを読む。帳簿から生成した年も、申告済みの決算書から転記した年も同じ形式。"""
    rows = read_rows(path)
    if len(rows) < 2 or not rows[1] or not rows[1][0].startswith("期間 "):
        die(f"{rel(path)} の2行目に『期間』がない")
    sections, cur = defaultdict(list), None
    for r in rows[2:]:
        if not r or not any(c.strip() for c in r):
            continue
        if r[0].startswith("【"):
            cur = r[0].strip("【】").split("（")[0]
        elif cur and r[0] not in ("欄", "区分", "項目"):
            sections[cur].append(r)

    pl = []
    for r in sections["損益計算書"]:
        v = to_int(r[2]) if len(r) > 2 else None
        if r[0] not in CIRCLED or v is None:
            die(f"{rel(path)} の損益計算書の行が読めない: {','.join(r)}")
        pl.append([r[0], r[1], v])
    bs = []
    for r in sections["貸借対照表"]:
        cells = (r + ["", ""])[2:4]
        vals = [to_int(c) for c in cells]
        if r[0] not in ("資産", "負債・資本") or any(c.strip() and v is None for c, v in zip(cells, vals)):
            die(f"{rel(path)} の貸借対照表の行が読めない: {','.join(r)}")
        bs.append([r[0], r[1], *vals])
    source = rows[1][3].removeprefix("出典 ") if len(rows[1]) > 3 else ""
    k = {"year": int(path.parent.parent.name), "period": rows[1][0].removeprefix("期間 "),
         "source": source, "filed": source.startswith("申告済み"), "pl": pl, "bs": bs,
         "checks": [[r[0], to_int(r[1]), to_int(r[2])] for r in sections["サマリーとの照合"]],
         "notes": [r[0].removeprefix("※").strip() for r in sections["注記"]]}
    verify_kessan(k, path)
    return k


def verify_kessan(k, path):
    """欄どうしの計算が合うか確かめる。転記ミスをここで止める。"""
    v = {no: amt for no, _, amt in k["pl"]}
    errs = []
    lines = [amt for no, _, amt in k["pl"] if 8 <= CIRCLED.index(no) + 1 <= 31]
    if lines and sum(lines) != v.get("㉜"):
        errs.append(f"⑧〜㉛の合計 {sum(lines):,} ≠ ㉜ {v.get('㉜')}")
    if {"⑦", "㉜", "㉝"} <= v.keys() and v["⑦"] - v["㉜"] != v["㉝"]:
        errs.append("⑦ − ㉜ ≠ ㉝")
    if {"㉝", "㊸"} <= v.keys() and v["㉝"] + v.get("㊲", 0) - v.get("㊷", 0) != v["㊸"]:
        errs.append("㉝ + ㊲ − ㊷ ≠ ㊸")
    if {"㊸", "㊹", "㊺"} <= v.keys() and v["㊸"] - v["㊹"] != v["㊺"]:
        errs.append("㊸ − ㊹ ≠ ㊺")
    totals = {}
    for side in ("資産", "負債・資本"):
        rows = [r for r in k["bs"] if r[0] == side]
        total = next((r for r in rows if r[1] == "合計"), None)
        if rows and not total:
            errs.append(f"貸借対照表の{side}に合計の行がない")
            continue
        for i, label in ((2, "期首"), (3, "期末")):
            s = sum(r[i] or 0 for r in rows if r[1] != "合計")
            if total and s != (total[i] or 0):
                errs.append(f"貸借対照表 {side}の{label} {s:,} ≠ 合計 {total[i]}")
        if total:
            totals[side] = total
    if len(totals) == 2 and [x or 0 for x in totals["資産"][2:]] != [x or 0 for x in totals["負債・資本"][2:]]:
        errs.append("貸借対照表の資産と負債・資本の合計が違う")
    if errs:
        die(f"{rel(path)} の数字が合わない:\n  " + "\n  ".join(errs))


def read_books(journal: Path, ledger: Path):
    """仕訳帳・総勘定元帳CSVを閲覧用に読む。"""
    entries = [[int(r[0]), r[1], r[2], r[4], int(r[3]), r[6], r[7]]
               for r in read_rows(journal)[1:] if r and r[0] != "合計"]
    accounts = {}
    for r in read_rows(ledger)[1:]:
        a = accounts.setdefault(r[0], {"name": r[0], "kind": kind_of(r[0]), "open": None,
                                       "dr": 0, "cr": 0, "close": 0})
        if r[2] == "" and r[4] == "前期繰越":
            a["open"] = to_int(r[7])
        elif r[2] == "" and r[4] == "合計":
            a.update(dr=to_int(r[5]) or 0, cr=to_int(r[6]) or 0, close=to_int(r[7]) or 0)
    return entries, list(accounts.values())


def receivables(entries, accounts):
    """売上ごとの入金状況（帳簿.html の入金状況タブ）。read_books の仕訳から作る。
    売上『取引先 M月度』と入金『取引先 M月度 入金』（仮置きなら末尾に TENTATIVE）を摘要で突き合わせる。
    行: [月度, 取引先, 金額, 計上日（期首の売掛金は空）, 入金日, 状態, 支払期限, 証憑]"""
    rows, paid = {}, {}
    for no, d, dr, cr, amount, memo, evidence in entries:
        if (dr, cr) == ("売掛金", "売上高"):
            y, m = int(d[:4]), int(d[5:7])
            rows[memo] = [memo, amount, d, "", "未入金", f"{next_month_end(y, m):%Y/%m/%d}", evidence]
        elif cr == "売掛金":
            key = memo.removesuffix(TENTATIVE).removesuffix(" 入金")
            paid[key] = (d, "仮置き" if memo.endswith(TENTATIVE) else "通帳で確認", amount, evidence)
    for key, (d, state, amount, evidence) in paid.items():
        r = rows.setdefault(key, [key, amount, "", "", "", "", evidence])  # 期首の売掛金
        if r[1] != amount:
            die(f"入金状況: {key} の売上 {r[1]:,} と入金 {amount:,} が合わない")
        r[3], r[4] = d, state
    out = []
    for memo, amount, sold, d, state, due, evidence in sorted(rows.values(), key=lambda r: r[2]):
        mm = re.fullmatch(r"(.+) ((?:\d{4}年)?\d{1,2}月度)", memo)
        if not mm:
            die(f"入金状況: 摘要『{memo}』から取引先と月度が読めない")
        out.append([mm.group(2), mm.group(1), amount, sold, d, state, due, evidence])
    open_ar = sum(r[2] for r in out if r[5] == "未入金")
    close_ar = next((a["close"] for a in accounts if a["name"] == "売掛金"), 0)
    if open_ar != close_ar:
        die(f"入金状況: 未入金の合計 {open_ar:,} が売掛金の期末残高 {close_ar:,} と合わない")
    return out


def build_html():
    """全年度の決算書CSV（あれば仕訳帳・総勘定元帳・転記シートも）から 帳簿.html の中身を作る。
    書き出しは呼び出し側が行う。これから書き出すCSV（PENDING）も読む。"""
    found = {}
    for path in sorted(ROOT.glob("[0-9][0-9][0-9][0-9]/帳簿/決算書_*.csv")) + sorted(PENDING):
        if path.name.startswith("決算書_"):
            found[int(path.parent.parent.name)] = ROOT / f"{path.parent.parent.name}/帳簿/{path.name}"
    years = []
    for year, path in sorted(found.items(), reverse=True):
        k = read_kessan(path)
        journal = path.with_name(f"仕訳帳_{year}.csv")
        ledger = path.with_name(f"総勘定元帳_{year}.csv")
        if exists(journal) and exists(ledger):
            k["entries"], k["accounts"] = read_books(journal, ledger)
            k["receivables"] = receivables(k["entries"], k["accounts"])
        sheet = path.with_name(f"転記シート_{year}.csv")
        if exists(sheet):
            k["sheet"] = [r[:4] + [to_int(r[4])] + r[5:] for r in read_rows(sheet)[1:] if r]
        years.append(k)
    payload = json.dumps({"years": years, "owner": [OWNER_DRAW, OWNER_PAID], "privateBank": bool(PRIVATE_BANK)},
                         ensure_ascii=False).replace("</", "<\\/")
    return ROOT / "帳簿.html", HTML.replace("/*__DATA__*/null", payload), years


HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>帳簿と決算書</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Shippori+Mincho:wght@500;700&family=Noto+Sans+JP:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{color-scheme:light dark;
  --paper:#faf8f4; --surface:#ffffff; --sunk:#f2efe8;
  --ink:#1f2733; --ink-2:#4e5a6b; --ink-3:#818d9d;
  --rule:#e0dbd0; --rule-2:#cdc6b8;
  --seal:#b03a30; --moss:#3f6b52; --indigo:#2f4d7a; --ochre:#9a7326;
}
@media (prefers-color-scheme:dark){ :root:not([data-theme="light"]){
  --paper:#15181d; --surface:#1c2028; --sunk:#232832;
  --ink:#e8e4db; --ink-2:#a8b1bf; --ink-3:#76808f;
  --rule:#2e343f; --rule-2:#3c434f;
  --seal:#e07263; --moss:#7bb491; --indigo:#8ab0e0; --ochre:#d1a75a;
}}
:root[data-theme="dark"]{
  --paper:#15181d; --surface:#1c2028; --sunk:#232832;
  --ink:#e8e4db; --ink-2:#a8b1bf; --ink-3:#76808f;
  --rule:#2e343f; --rule-2:#3c434f;
  --seal:#e07263; --moss:#7bb491; --indigo:#8ab0e0; --ochre:#d1a75a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:"Noto Sans JP",system-ui,sans-serif;line-height:1.7;
  font-size:15px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1400px;margin:0 auto;padding-inline:24px;padding-block:0 72px}
@media(max-width:560px){.wrap{padding-inline:16px}}
h1,h2{font-family:"Shippori Mincho",serif;font-weight:700;margin:0;letter-spacing:.02em}
.num{font-family:"IBM Plex Mono",ui-monospace,monospace;
  font-variant-numeric:tabular-nums;letter-spacing:-.01em}
a{color:var(--indigo)}

header{border-bottom:2px solid var(--ink);margin-bottom:16px;
  padding-block:44px 16px;display:flex;flex-wrap:wrap;
  align-items:flex-end;gap:16px;justify-content:space-between}
header h1{font-size:clamp(28px,5.2vw,40px);line-height:1.2}
.eyebrow{font-size:11px;letter-spacing:.22em;color:var(--seal);font-weight:700;margin-bottom:8px}
.stamp{border:1.5px solid var(--seal);color:var(--seal);border-radius:3px;
  padding:5px 11px;font-size:11px;font-weight:700;letter-spacing:.09em;
  white-space:nowrap;line-height:1.5;text-align:center}
.stamp.filed{border-color:var(--moss);color:var(--moss)}

nav.years{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:24px}
nav.years a{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:13.5px;text-decoration:none;
  color:var(--ink-2);border:1px solid var(--rule-2);border-radius:999px;padding:3px 13px;background:var(--surface)}
nav.years a:hover{border-color:var(--ink-3);color:var(--ink)}
nav.years a[aria-current="page"]{background:var(--ink);color:var(--paper);border-color:var(--ink)}

.figs{display:grid;gap:1px;background:var(--rule);
  grid-template-columns:repeat(auto-fit,minmax(180px,1fr));border:1px solid var(--rule)}
.fig{background:var(--surface);padding:16px 17px 14px}
.fig .k{font-size:11px;color:var(--ink-3);letter-spacing:.09em;font-weight:500}
.fig .v{font-size:clamp(20px,3.2vw,26px);font-weight:600;margin-top:5px;line-height:1.15}
.fig .s{font-size:11.5px;color:var(--ink-3);margin-top:4px}
.fig.hl{background:var(--sunk)}
.fig.hl .v{color:var(--moss)}

.check{border:1px solid var(--rule);border-left:3px solid var(--ink-3);background:var(--surface);
  padding:12px 16px;margin:18px 0 28px;font-size:13.5px}
.check.ok{border-left-color:var(--moss)} .check.ng{border-left-color:var(--seal)}
.check.ok b{color:var(--moss)} .check.ng b{color:var(--seal)}
.check .src{color:var(--ink-3);font-size:12.5px}
.check ul{margin:6px 0 0;padding-left:1.2em;color:var(--ink-2)}

nav.tabs{display:flex;gap:2px;border-bottom:1px solid var(--rule-2);margin-bottom:22px;flex-wrap:wrap}
nav.tabs a{font-size:14.5px;text-decoration:none;color:var(--ink-2);
  border-bottom:2.5px solid transparent;padding:8px 16px;margin-bottom:-1px}
nav.tabs a[aria-current="page"]{color:var(--ink);border-bottom-color:var(--seal);font-weight:700}
[hidden]{display:none !important}
h2{font-size:18px;margin-bottom:10px}
.sub{font-size:12px;color:var(--ink-3);margin:-6px 0 10px}
.empty{border:1px dashed var(--rule-2);padding:22px;color:var(--ink-2);font-size:14px;background:var(--surface)}

.grid2{display:grid;gap:28px;grid-template-columns:minmax(0,1fr)}
@media(min-width:1000px){.grid2{grid-template-columns:minmax(0,1fr) minmax(0,1fr)}}

.controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:12px}
.controls input,.controls select{font:inherit;font-size:13.5px;padding:6px 10px;
  border:1px solid var(--rule-2);border-radius:4px;background:var(--surface);color:var(--ink)}
.controls input[type=search]{flex:1 1 220px;min-width:0}
.controls label{font-size:13px;color:var(--ink-2);display:flex;gap:6px;align-items:center}
.controls .count{margin-left:auto;font-size:13px;color:var(--ink-2)}

.scroll{overflow:auto;border:1px solid var(--rule);background:var(--surface)}
.scroll.tall{max-height:72vh}
table{border-collapse:collapse;width:100%;font-size:13.5px}
.wide{min-width:960px}
th,td{padding:7px 12px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--rule)}
th{font-size:11px;letter-spacing:.06em;color:var(--ink-3);font-weight:600;
  background:var(--sunk);position:sticky;top:0;z-index:1}
th.l,td.l{text-align:left}
td.memo{text-align:left;white-space:normal;min-width:220px}
td.mark,td.no{color:var(--ink-3);font-size:12px}
td.zero{color:var(--ink-3)}
tr.sum td{font-weight:700;background:var(--sunk);border-top:1.5px solid var(--rule-2)}
tr.head td{font-weight:700;color:var(--ink-2);background:var(--paper);font-size:12px;letter-spacing:.08em}
tr.click{cursor:pointer}
tr.click:hover td{background:var(--sunk)}
tr.sel td{background:var(--sunk)}
tr.sel td:first-child{box-shadow:inset 3px 0 0 var(--seal)}
a.acct{color:inherit;text-decoration:none;border-bottom:1px dotted var(--ink-3)}
a.acct:hover{color:var(--indigo);border-bottom-color:var(--indigo)}
td.ev a{font-size:12px}

#trend-table{min-width:880px}
#trend-table th a{color:inherit;text-decoration:none}
#trend-table th small{display:block;font-size:10px;letter-spacing:0;font-weight:500}
#trend-table th.cur,#trend-table td.cur{background:color-mix(in srgb,var(--seal) 7%,var(--surface))}
#trend-table th.cur{color:var(--seal)}
#trend-table tr.sum td.cur{background:color-mix(in srgb,var(--seal) 10%,var(--sunk))}
h3{font-size:14px;margin:22px 0 8px;color:var(--ink-2);font-weight:700}
.sheet-doc{margin-bottom:34px}
.sheet-doc table{min-width:780px}
td.copy{cursor:copy}
td.copy:hover{background:var(--sunk)}
td.copied{background:color-mix(in srgb,var(--moss) 20%,var(--surface))}
.badge{font-size:11px;padding:1px 7px;border-radius:3px;border:1px solid currentColor;white-space:nowrap}
.badge.t{color:var(--ochre)} .badge.c{color:var(--seal)} .badge.n{color:var(--ink-3)} .badge.ok{color:var(--moss)}
.ar-sum{display:flex;flex-wrap:wrap;gap:8px 18px;font-size:13.5px;color:var(--ink-2);margin-bottom:12px}
#ar-table{min-width:520px}
#ar-table td{vertical-align:top}
#ar-table td.l{font-weight:500}
.ar-cell .d{font-size:13.5px;margin-right:6px}
.ar-cell .a{display:block;font-size:12px;color:var(--ink-3)}
.ar-cell .s{font-size:12px;color:var(--ink-3)}
.ar-cell .a a{color:inherit}
footer{margin-top:40px;font-size:12px;color:var(--ink-3)}
@media print{
  body{background:#fff;color:#000;font-size:11px}
  nav,.controls,footer,#figs{display:none}
  .scroll,.scroll.tall{max-height:none;overflow:visible;border:none}
  th{position:static}
  .wide,#trend-table,#ar-table,.sheet-doc table{min-width:0}
  header{padding-block:8px 6px}
}
</style>
</head>
<body>
<div class="wrap">
<header>
  <div>
    <div class="eyebrow">青色申告</div>
    <h1 id="title">帳簿と決算書</h1>
  </div>
  <div class="stamp" id="stamp"></div>
</header>
<nav class="years" id="years" aria-label="年分"></nav>

<div class="figs" id="figs"></div>
<div class="check" id="check"></div>

<nav class="tabs" id="tabs">
  <a data-tab="pl">決算書</a>
  <a data-tab="journal">仕訳帳</a>
  <a data-tab="ledger">総勘定元帳</a>
  <a data-tab="receipts">入金状況</a>
  <a data-tab="trend">年次推移</a>
  <a data-tab="sheet">転記シート</a>
</nav>

<section class="panel" id="pl">
  <div class="grid2">
    <div>
      <h2>損益計算書（1面）</h2>
      <p class="sub">欄番号は青色申告決算書（一般用）。科目名を押すと総勘定元帳を開く</p>
      <div class="controls"><label><input type="checkbox" id="zero"> 0円の欄も表示</label></div>
      <div class="scroll"><table><thead><tr><th class="l">欄</th><th class="l">科目</th><th>金額</th></tr></thead>
        <tbody id="pl-body"></tbody></table></div>
    </div>
    <div>
      <h2>貸借対照表（4面）</h2>
      <p class="sub">事業主貸・事業主借は期首欄なし。元入金は期首の資産−負債</p>
      <div id="bs-box"></div>
    </div>
  </div>
</section>

<section class="panel" id="journal" hidden>
  <div id="journal-box">
    <div class="controls">
      <input type="search" id="q" placeholder="摘要・科目・証憑で絞り込み">
      <select id="j-acct"></select>
      <select id="j-month"></select>
      <span class="count num" id="j-count"></span>
    </div>
    <div class="scroll tall"><table class="wide"><thead><tr>
      <th>番号</th><th class="l">日付</th><th class="l">借方</th><th class="l">貸方</th><th>金額</th>
      <th class="l">摘要</th><th class="l">証憑</th></tr></thead><tbody id="j-body"></tbody></table></div>
  </div>
  <div class="empty" id="journal-empty" hidden></div>
</section>

<section class="panel" id="ledger" hidden>
  <div class="grid2" id="ledger-box">
    <div>
      <h2>試算表</h2>
      <p class="sub">行を押すと右に元帳を表示</p>
      <div class="scroll"><table><thead><tr><th class="l">科目</th><th>期首</th><th>借方</th><th>貸方</th><th>残高</th></tr></thead>
        <tbody id="tb-body"></tbody></table></div>
    </div>
    <div>
      <h2 id="l-title"></h2>
      <p class="sub" id="l-sub"></p>
      <div class="scroll tall"><table class="wide"><thead><tr>
        <th class="l">日付</th><th>番号</th><th class="l">相手科目</th><th class="l">摘要</th>
        <th>借方</th><th>貸方</th><th>残高</th><th class="l">証憑</th></tr></thead>
        <tbody id="l-body"></tbody></table></div>
    </div>
  </div>
  <div class="empty" id="ledger-empty" hidden></div>
</section>

<section class="panel" id="receipts" hidden>
  <div id="receipts-box">
    <h2>入金状況（売掛金）</h2>
    <p class="sub" id="ar-note"></p>
    <div class="ar-sum" id="ar-sum"></div>
    <div class="scroll"><table id="ar-table"><thead id="ar-head"></thead><tbody id="ar-body"></tbody></table></div>
  </div>
  <div class="empty" id="receipts-empty" hidden></div>
</section>

<section class="panel" id="trend" hidden>
  <h2>年次推移（損益計算書）</h2>
  <p class="sub">年を押すとその年の決算書を開く。— は0円か内訳なし</p>
  <div class="scroll"><table id="trend-table"><thead id="tr-head"></thead><tbody id="tr-body"></tbody></table></div>
</section>

<section class="panel" id="sheet" hidden>
  <p class="sub">作成コーナーに入力する数字を、書類と欄ごとに並べたもの。金額を押すとコピーできる</p>
  <div id="sheet-box"></div>
  <div class="empty" id="sheet-empty" hidden></div>
</section>

<footer>scripts/build_books.py が生成。手で編集しない（次の生成で消える）。外部に公開しない。</footer>
</div>

<script>
const ALL = /*__DATA__*/null;
const $ = id => document.getElementById(id);
const yen = n => n == null || n === "" ? "" : Number(n).toLocaleString("ja-JP");
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v, cls = "") => `<td class="num ${cls}${v === 0 ? " zero" : ""}">${yen(v)}</td>`;
const debitNormal = a => a.kind === "資産" || a.kind === "費用";
const CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚㉛㉜㉝㉞㉟㊱㊲㊳㊴㊵㊶㊷㊸㊹㊺";
const lineNo = mark => [...CIRCLED].indexOf(mark) + 1;
const SUMS = new Set(["⑦", "㉜", "㉝", "㊸", "㊺"]);
const TABS = ["pl", "journal", "ledger", "receipts", "trend", "sheet"];
const OWNER = new Set(ALL.owner);
const BY_YEAR = Object.fromEntries(ALL.years.map(y => [y.year, y]));
const pick = (y, no) => (y.pl.find(r => r[0] === no) || [])[2] ?? null;
const status = y => y.filed ? "申告済み" : /暫定/.test(y.period) ? "期中・暫定" : "未申告";

let Y = null, ACC = {}, tab = "pl";
const href = (year, t, arg) => `#${year}/${t}` + (arg ? "/" + encodeURIComponent(arg) : "");
const acct = (n, label = n) => ACC[n] ? `<a class="acct" href="${href(Y.year, "ledger", n)}">${esc(label)}</a>` : esc(label);
const ev = p => p ? `<a href="${esc(encodeURI(p))}" target="_blank" rel="noopener">${esc(p.split("/").pop())}</a>` : "";

// ---- 年ごとの表示 ----
function renderYear() {
  ACC = Object.fromEntries((Y.accounts || []).map(a => [a.name, a]));
  $("title").textContent = `${Y.year}年分 帳簿と決算書`;
  $("stamp").className = "stamp" + (Y.filed ? " filed" : "");
  $("stamp").innerHTML = status(Y) + "<br>" + esc(Y.period.replace(/（.*）/, ""));
  const prev = BY_YEAR[Y.year - 1];
  const fig = (k, no, cls = "") => {
    const v = pick(Y, no), p = prev && pick(prev, no);
    return `<div class="fig ${cls}"><div class="k">${k}</div><div class="v num">${v == null ? "—" : yen(v)}</div>` +
      `<div class="s">${p == null ? "&nbsp;" : `前年 <span class="num">${yen(p)}</span>`}</div></div>`;
  };
  $("figs").innerHTML = fig("売上（収入）金額", "①") + fig("経費", "㉜") +
    fig("青色申告特別控除前の所得", "㊸") + fig("所得金額（特別控除後）", "㊺", "hl");

  const diffs = Y.checks.filter(([, b, s]) => b !== s);
  $("check").className = "check" + (Y.checks.length ? diffs.length ? " ng" : " ok" : "");
  let h = `<div class="src">出典: ${esc(Y.source)}</div>`;
  if (Y.checks.length) h += diffs.length
    ? `<b>確定申告サマリーと ${diffs.length}件 不一致</b><ul>${diffs.map(([a, b, s]) =>
        `<li>${esc(a)}: 帳簿 ${yen(b)} / サマリー ${s == null ? "行がない" : yen(s)}</li>`).join("")}</ul>`
    : `<b>確定申告サマリーと一致</b>（売上・経費${Y.checks.length - 2}科目・経費合計）`;
  if (Y.notes.length) h += `<ul>${Y.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`;
  $("check").innerHTML = h;

  renderPL();
  renderBS();
  const has = !!Y.entries;
  for (const id of ["journal", "ledger"]) {
    $(id + "-box").hidden = !has;
    $(id + "-empty").hidden = has;
    $(id + "-empty").textContent = `${Y.year}年分の仕訳帳・総勘定元帳はこのリポジトリにありません（${Y.source}）。` +
      "決算書の数字だけを載せています。";
  }
  $("receipts-box").hidden = !has;
  $("receipts-empty").hidden = has;
  $("receipts-empty").textContent = `${Y.year}年分の入金状況はありません。入金状況は帳簿から生成した年だけに作ります。`;
  $("sheet-box").hidden = !Y.sheet;
  $("sheet-empty").hidden = !!Y.sheet;
  $("sheet-empty").textContent = `${Y.year}年分の転記シートはありません。転記シートは帳簿から生成した年だけに作ります。`;
  if (has) {
    $("j-acct").innerHTML = `<option value="">すべての科目</option>` +
      Y.accounts.map(a => `<option>${esc(a.name)}</option>`).join("");
    $("j-month").innerHTML = `<option value="">全期間</option>` +
      [...new Set(Y.entries.map(e => e[1].slice(5, 7)))].map(m => `<option value="${m}">${Number(m)}月</option>`).join("");
    $("q").value = "";
  }
}

function renderPL() {
  const all = $("zero").checked;
  $("pl-body").innerHTML = Y.pl.filter(([no, , v]) => all || v !== 0 || SUMS.has(no) || no === "①")
    .map(([no, name, v]) => `<tr class="${SUMS.has(no) ? "sum" : ""}"><td class="mark">${no}</td>` +
      `<td class="l">${acct(name === "売上（収入）金額" ? "売上高" : name, name)}</td>${num(v)}</tr>`).join("");
}
$("zero").addEventListener("change", renderPL);

function renderBS() {
  if (!Y.bs.length) {
    $("bs-box").innerHTML = `<div class="empty">この年の貸借対照表はリポジトリにありません。</div>`;
    return;
  }
  const row = r => r[1] === "合計"
    ? `<tr class="sum"><td class="l">合計</td>${num(r[2])}${num(r[3])}</tr>`
    : `<tr><td class="l">${acct(r[1])}</td>${r[2] == null ? "<td></td>" : num(r[2])}${num(r[3])}</tr>`;
  const part = side => `<tr class="head"><td class="l" colspan="3">${side === "資産" ? "資産の部" : "負債・資本の部"}</td></tr>` +
    Y.bs.filter(r => r[0] === side).map(row).join("");
  $("bs-box").innerHTML = `<div class="scroll"><table><thead><tr><th class="l">科目</th><th>期首</th><th>期末</th></tr></thead>` +
    `<tbody>${part("資産")}${part("負債・資本")}</tbody></table></div>`;
}

// ---- 仕訳帳 ----
function renderJournal() {
  const q = $("q").value.trim().toLowerCase(), a = $("j-acct").value, m = $("j-month").value;
  const rows = Y.entries.filter(e =>
    (!a || e[2] === a || e[3] === a) && (!m || e[1].slice(5, 7) === m) &&
    (!q || [e[0], e[2], e[3], e[5], e[6]].join(" ").toLowerCase().includes(q)));
  $("j-count").textContent = `${rows.length}件・${yen(rows.reduce((s, e) => s + e[4], 0))}円`;
  $("j-body").innerHTML = rows.map(e => `<tr><td class="no num">${e[0]}</td><td class="l num">${e[1]}</td>` +
    `<td class="l">${acct(e[2])}</td><td class="l">${acct(e[3])}</td>${num(e[4])}` +
    `<td class="memo">${esc(e[5])}</td><td class="l ev">${ev(e[6])}</td></tr>`).join("");
}
["q", "j-acct", "j-month"].forEach(id => $(id).addEventListener("input", renderJournal));

// ---- 総勘定元帳 ----
function renderLedger(name) {
  const a = ACC[name] || Y.accounts[0];
  $("tb-body").innerHTML = Y.accounts.map(x =>
    `<tr class="click${x === a ? " sel" : ""}" data-acct="${esc(x.name)}"><td class="l">${esc(x.name)}</td>` +
    (x.open == null ? "<td></td>" : num(x.open)) + num(x.dr) + num(x.cr) + num(x.close) + "</tr>").join("");
  $("l-title").textContent = a.name;
  $("l-sub").textContent = `${a.kind}・${debitNormal(a) ? "借方" : "貸方"}残高`;
  const sign = debitNormal(a) ? 1 : -1;
  let run = a.open ?? 0;
  let html = a.open != null
    ? `<tr><td class="l num">${Y.year}/01/01</td><td></td><td></td><td class="memo">前期繰越</td><td></td><td></td>${num(run)}<td></td></tr>` : "";
  for (const e of Y.entries) {
    const isDr = e[2] === a.name;
    if (!isDr && e[3] !== a.name) continue;
    run += sign * (isDr ? e[4] : -e[4]);
    html += `<tr><td class="l num">${e[1]}</td><td class="no num">${e[0]}</td><td class="l">${acct(isDr ? e[3] : e[2])}</td>` +
      `<td class="memo">${esc(e[5])}</td>${isDr ? num(e[4]) + "<td></td>" : "<td></td>" + num(e[4])}${num(run)}` +
      `<td class="l ev">${ev(e[6])}</td></tr>`;
  }
  html += `<tr class="sum"><td class="l" colspan="4">合計</td>${num(a.dr)}${num(a.cr)}${num(a.close)}<td></td></tr>`;
  $("l-body").innerHTML = html;
}
$("tb-body").addEventListener("click", e => {
  const tr = e.target.closest("tr[data-acct]");
  if (tr) location.hash = href(Y.year, "ledger", tr.dataset.acct);
});

// ---- 入金状況 ----
const AR_BADGE = {"通帳で確認": "ok", "仮置き": "t", "未入金": "n"};
const md = d => d ? `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}` : "";
function renderReceipts() {
  const rows = Y.receivables || [];
  const csv = `${Y.year}/帳簿/入金日_${Y.year}.csv`;
  $("ar-note").innerHTML = `入金日は <a href="${esc(encodeURI(csv))}" target="_blank" rel="noopener">${esc(csv.split("/").pop())}</a>` +
    " に書くと「通帳で確認」になる。書いていない分は支払期限（月度の翌月末）で仮置き。" +
    (ALL.privateBank ? "入金先の口座は個人の口座として扱うので、通帳の明細そのものは載せない" : "");
  const count = st => rows.filter(r => r[5] === st);
  const open = count("未入金");
  $("ar-sum").innerHTML = ["通帳で確認", "仮置き", "未入金"].map(st =>
    `<span><span class="badge ${AR_BADGE[st]}">${st}</span> <span class="num">${count(st).length}</span>件</span>`).join("") +
    `<span>売掛金の残高 <span class="num">${yen(open.reduce((s, r) => s + r[2], 0))}</span>円</span>`;
  const clients = [...new Set(rows.map(r => r[1]))];
  const months = [...new Set(rows.map(r => r[0]))];
  const by = new Map(rows.map(r => [r[0] + "\t" + r[1], r]));
  const cell = r => {
    if (!r) return `<td class="l zero">—</td>`;
    const [, , amount, , paid, st, due, src] = r;
    const when = st === "未入金" ? (due ? `<span class="s">期限 <span class="num">${md(due)}</span></span>` : "")
      : `<span class="d num">${md(paid)}</span>`;
    const amt = src ? `<a href="${esc(encodeURI(src))}" target="_blank" rel="noopener">${yen(amount)}</a>` : yen(amount);
    return `<td class="l ar-cell">${st === "未入金" ? "" : when}<span class="badge ${AR_BADGE[st]}">${st}</span>` +
      `${st === "未入金" ? " " + when : ""}<span class="a num">${amt}円</span></td>`;
  };
  $("ar-head").innerHTML = `<tr><th class="l">月度</th>${clients.map(c => `<th class="l">${esc(c)}</th>`).join("")}</tr>`;
  $("ar-body").innerHTML = months.map(m => `<tr><td class="l">${esc(m)}</td>` +
    clients.map(c => cell(by.get(m + "\t" + c))).join("") + "</tr>").join("");
}

// ---- 年次推移 ----
function renderTrend() {
  const ys = [...ALL.years].reverse();
  const names = new Map();  // 経費科目 → 最初に出た欄番号（並び順に使う）
  for (const y of ys) for (const [no, name] of y.pl) {
    const n = lineNo(no);
    if (n >= 8 && n <= 31 && (!names.has(name) || names.get(name) > n)) names.set(name, n);
  }
  const byName = (y, name) => {
    const hit = y.pl.filter(([no, n]) => n === name && lineNo(no) >= 8 && lineNo(no) <= 31);
    return hit.length ? hit.reduce((s, r) => s + r[2], 0) : null;
  };
  // どの年も0円の欄（決算書の定型の科目）は出さない
  const expenses = [...names].sort((a, b) => a[1] - b[1] || a[0].localeCompare(b[0])).map(([n]) => n)
    .filter(n => ys.some(y => byName(y, n)));
  const cell = (y, v, pct) => `<td class="num${y === Y ? " cur" : ""}${v ? "" : " zero"}">` +
    (v == null || v === 0 ? "—" : pct ? v.toFixed(1) + "%" : yen(v)) + "</td>";
  const row = (label, get, cls = "", pct = false) =>
    `<tr class="${cls}"><td class="l">${esc(label)}</td>${ys.map(y => cell(y, get(y), pct)).join("")}</tr>`;
  $("tr-head").innerHTML = `<tr><th class="l">科目</th>${ys.map(y =>
    `<th class="${y === Y ? "cur" : ""}"><a href="${href(y.year, "pl")}">${y.year}</a><small>${status(y)}</small></th>`).join("")}</tr>`;
  $("tr-body").innerHTML =
    row("売上（収入）金額", y => pick(y, "①"), "sum") +
    expenses.map(n => row(n, y => byName(y, n))).join("") +
    row("経費 計", y => pick(y, "㉜"), "sum") +
    row("差引金額", y => pick(y, "㉝")) +
    row("専従者給与", y => pick(y, "㊳")) +
    row("青色申告特別控除前の所得金額", y => pick(y, "㊸"), "sum") +
    row("青色申告特別控除額", y => pick(y, "㊹")) +
    row("所得金額", y => pick(y, "㊺"), "sum") +
    row("経費率（経費 計 ÷ 売上）", y => pick(y, "①") ? pick(y, "㉜") / pick(y, "①") * 100 : null, "", true);
}

// ---- 転記シート ----
const linkify = s => esc(s).replace(/https?:\/\/[^\s）)]+/g, u => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);
const badge = st => !st ? "" : `<span class="badge ${/要確認/.test(st) ? "c" : /暫定/.test(st) ? "t" : "n"}">${esc(st)}</span>`;
function renderSheet() {
  if (!Y.sheet) return;
  const docs = new Map();
  for (const r of Y.sheet) {
    if (!docs.has(r[0])) docs.set(r[0], new Map());
    const parts = docs.get(r[0]);
    if (!parts.has(r[1])) parts.set(r[1], []);
    parts.get(r[1]).push(r);
  }
  let h = "";
  for (const [doc, parts] of docs) {
    h += `<div class="sheet-doc"><h2>${esc(doc)}</h2>`;
    for (const [part, rows] of parts) {
      h += `<h3>${esc(part)}</h3><div class="scroll"><table><thead><tr><th class="l">欄</th><th class="l">項目</th>` +
        `<th>金額</th><th class="l">内容</th><th class="l">状態</th><th class="l">出典</th></tr></thead><tbody>` +
        rows.map(([, , no, item, v, text, src, st]) => `<tr><td class="mark">${esc(no)}</td><td class="l">${esc(item)}</td>` +
          (v == null ? "<td></td>" : `<td class="num copy${v === 0 ? " zero" : ""}" data-v="${v}" title="押すとコピー">${yen(v)}</td>`) +
          `<td class="memo">${linkify(text)}</td><td class="l">${badge(st)}</td><td class="l ev">${ev(src)}</td></tr>`).join("") +
        `</tbody></table></div>`;
    }
    h += "</div>";
  }
  $("sheet-box").innerHTML = h;
}
function copyText(t) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(t);
  const ta = document.createElement("textarea");
  ta.value = t; document.body.appendChild(ta); ta.select();
  document.execCommand("copy"); ta.remove();
  return Promise.resolve();
}
$("sheet-box").addEventListener("click", e => {
  const td = e.target.closest("td.copy");
  if (!td) return;
  copyText(td.dataset.v).then(() => {
    td.classList.add("copied");
    setTimeout(() => td.classList.remove("copied"), 700);
  });
});

// ---- 年とタブの切り替え ----
function route() {
  let h = "";
  try { h = decodeURIComponent(location.hash.slice(1)); } catch (e) { h = ""; }
  const [ys, t, arg] = h.split("/");
  const year = BY_YEAR[Number(ys)] || ALL.years[0];
  tab = TABS.includes(t) ? t : "pl";
  if (year !== Y) { Y = year; renderYear(); }
  $("years").innerHTML = ALL.years.map(y => `<a href="${href(y.year, tab)}"` +
    (y === Y ? ` aria-current="page"` : "") + `>${y.year}</a>`).join("");
  document.querySelectorAll("#tabs a").forEach(a => {
    a.setAttribute("href", href(Y.year, a.dataset.tab));
    if (a.dataset.tab === tab) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.querySelectorAll(".panel").forEach(p => p.hidden = p.id !== tab);
  if (tab === "journal" && Y.entries) renderJournal();
  if (tab === "ledger" && Y.entries) renderLedger(arg);
  if (tab === "receipts" && Y.entries) renderReceipts();
  if (tab === "trend") renderTrend();
  if (tab === "sheet") renderSheet();
}
window.addEventListener("hashchange", route);
route();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
