#!/usr/bin/env python3
"""月次管理台帳と年間管理台帳を再生成する。

使い方:
    python3 scripts/rebuild_ledger.py 2026/08     # 指定月 + 年間を再生成
    python3 scripts/rebuild_ledger.py 2026        # その年の全月 + 年間を再生成

集計元はファイル名（YYYYMMDD_取引先_金額.拡張子）。CSVに明細行は持たない。
`_重複` を含むファイルは同一領収証の複数撮影として集計から除外する。

このスクリプトは /summarizing-monthly-expenses skill から呼ばれる想定。
ロジックを skill 側で再実装しないこと（二重管理になる）。
"""

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".pdf", ".JPG", ".JPEG", ".PNG", ".PDF"}

# 同一取引の別証憑・別撮影。金額を持つが集計してはいけない。
EXCLUDE_MARKERS = ("重複", "カード控え", "_dup")

# 末尾の金額。以下の接尾辞を許容する:
#   _460      そのまま
#   _460_b    同日同店の2件目を区別する _b / _c
#   _20a      同上（アンダースコアなし）
#   _394am    時間帯を表す am / pm
AMOUNT_RE = re.compile(r"_(\d+)([a-zA-Z]*)(?:_[a-zA-Z]+)?$")
# 金額の直後に付いていたら外貨のまま（円に換算していない）とみなして読まない（_22USD など）
CURRENCIES = {"USD", "EUR", "GBP", "AUD", "CAD", "CHF", "SGD", "HKD", "CNY", "KRW", "TWD", "THB"}
# 先頭の日付（YYYYMMDD_）
DATE_PREFIX_RE = re.compile(r"\d{8}_")


def parse_amount(stem: str):
    """ファイル名（拡張子なし）から金額を取り出す。取れなければ None。"""
    m = AMOUNT_RE.search(stem)
    return int(m.group(1)) if m and m.group(2).upper() not in CURRENCIES else None


def collect_month(month_dir: Path):
    """勘定科目ごとに (件数, 合計金額) を返す。除外・警告も返す。"""
    totals, skipped = {}, []
    for cat_dir in sorted(p for p in month_dir.iterdir() if p.is_dir()):
        count = total = 0
        for f in sorted(cat_dir.iterdir()):
            if f.name.startswith("."):
                continue
            # HEIC などを黙って飛ばすと、その分だけ台帳が過少になる
            if f.suffix not in IMAGE_EXT:
                skipped.append((f, "対応していない拡張子（jpg/jpeg/png/pdf のみ）"))
                continue
            marker = next((m for m in EXCLUDE_MARKERS if m in f.stem), None)
            if marker:
                skipped.append((f, f"同一取引の別証憑・別撮影（{marker}）のため除外"))
                continue
            # 日付で始まらない名前（リネーム前の IMG_0001 など）は、末尾の数字を金額と取り違える
            amount = parse_amount(f.stem) if DATE_PREFIX_RE.match(f.stem) else None
            if amount is None:
                skipped.append((f, "ファイル名から日付・金額を取得できない"))
                continue
            count += 1
            total += amount
        if count:
            totals[cat_dir.name] = (count, total)
    return totals, skipped


def write_month(month_dir: Path, totals: dict):
    ym = f"{month_dir.parent.name}{month_dir.name}"
    out = month_dir / f"管理台帳_{ym}.csv"
    order = sorted(totals, key=lambda k: -totals[k][1])
    rows = [["勘定科目", "件数", "合計金額", "借方", "貸方"]]
    for k in order:
        c, t = totals[k]
        rows.append([k, c, t, k, "事業主借"])
    rows.append(["合計", sum(c for c, _ in totals.values()),
                 sum(t for _, t in totals.values()), "", ""])
    with out.open("w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(rows)
    return out


def rebuild_year(year_dir: Path):
    """その年の全月次CSVを集約して年間台帳を再生成する。"""
    amt = defaultdict(lambda: [0] * 12)
    cnt = defaultdict(lambda: [0] * 12)
    for i in range(12):
        path = year_dir / f"{i + 1:02d}" / f"管理台帳_{year_dir.name}{i + 1:02d}.csv"
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if row["勘定科目"] == "合計":
                    continue
                amt[row["勘定科目"]][i] += int(row["合計金額"])
                cnt[row["勘定科目"]][i] += int(row["件数"])

    order = sorted(amt, key=lambda k: -sum(amt[k]))
    total = sum(sum(amt[k]) for k in order)
    total_cnt = sum(sum(cnt[k]) for k in order)
    months = [f"{i}月" for i in range(1, 13)]
    lines = [f"{year_dir.name}年 経費管理台帳（年間）", ""]

    lines += ["【勘定科目別 月次推移】", "勘定科目," + ",".join(months) + ",年間合計"]
    for k in order:
        lines.append(k + "," + ",".join(map(str, amt[k])) + f",{sum(amt[k])}")
    lines.append("月合計," + ",".join(
        str(sum(amt[k][i] for k in order)) for i in range(12)) + f",{total}")

    lines += ["", "【勘定科目別 件数推移】", "勘定科目," + ",".join(months) + ",年間合計"]
    for k in order:
        lines.append(k + "," + ",".join(map(str, cnt[k])) + f",{sum(cnt[k])}")
    lines.append("月合計," + ",".join(
        str(sum(cnt[k][i] for k in order)) for i in range(12)) + f",{total_cnt}")

    lines += ["", "【勘定科目別 年間サマリー】", "勘定科目,件数,合計金額,構成比,借方,貸方"]
    for k in order:
        s = sum(amt[k])
        pct = s / total * 100 if total else 0
        lines.append(f"{k},{sum(cnt[k])},{s},{pct:.1f}%,{k},事業主借")
    lines.append(f"合計,{total_cnt},{total},100.0%,,")

    out = year_dir / f"管理台帳_{year_dir.name}.csv"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out, total, total_cnt


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)

    target = sys.argv[1].strip("/")
    parts = target.split("/")
    year_dir = ROOT / parts[0]
    if not year_dir.is_dir():
        sys.exit(f"エラー: {year_dir} が存在しません")

    month_dirs = ([year_dir / parts[1]] if len(parts) == 2
                  else sorted(p for p in year_dir.iterdir()
                              if p.is_dir() and re.fullmatch(r"\d{2}", p.name)))

    # --- 第1段階: 全月を集計するだけ。1件でも日付・金額を読めなければ何も書かずに中止する ---
    # 金額を読めないファイルを黙って0円扱いにすると、台帳が過少なまま上書きされ、
    # 確定申告の数字が静かに狂う。書き込み前に必ず検出して止める。
    collected, unparsable, duplicates = [], [], []
    for month_dir in month_dirs:
        if not month_dir.is_dir():
            sys.exit(f"エラー: {month_dir} が存在しません")
        totals, skipped = collect_month(month_dir)
        collected.append((month_dir, totals))
        for f, reason in skipped:
            (duplicates if "除外" in reason else unparsable).append((f, reason))

    if unparsable:
        print("エラー: ファイル名から日付・金額を読み取れない、または対応していない拡張子のファイルがあります。",
              file=sys.stderr)
        print("台帳を過少に上書きしてしまうため、集計を中止しました。\n", file=sys.stderr)
        for f, reason in unparsable:
            print(f"  {f.relative_to(ROOT)}（{reason}）", file=sys.stderr)
        print("\n対処: ファイル名を YYYYMMDD_取引先_金額.拡張子 に直してから再実行してください。",
              file=sys.stderr)
        print("HEIC などは JPEG・PNG・PDF に変換してから置きます。", file=sys.stderr)
        print("外貨建て（_USD22・_22USD 等）は取引月の月中平均TTMで円換算した金額に置き換えます。",
              file=sys.stderr)
        print("（CLAUDE.md「外貨建て経費の換算」参照）", file=sys.stderr)
        sys.exit(1)

    # --- 第2段階: ここまで来たら安全に書き込める ---
    for month_dir, totals in collected:
        out = write_month(month_dir, totals)
        detail = " / ".join(f"{k} {c}件 {t:,}円" for k, (c, t) in totals.items())
        print(f"{out.relative_to(ROOT)}: {detail}")

    out, total, total_cnt = rebuild_year(year_dir)
    print(f"\n{out.relative_to(ROOT)}: 年間 {total:,}円 / {total_cnt}件")

    if duplicates:
        print("\n--- 同一取引として集計から除外したファイル ---")
        for f, reason in duplicates:
            print(f"  {f.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
