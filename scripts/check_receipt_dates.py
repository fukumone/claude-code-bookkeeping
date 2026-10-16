#!/usr/bin/env python3
"""支払期限を過ぎたのに、入金日_YYYY.csv に入金日がない売上を一覧にする（読むだけ・何も書かない）。

使い方:
    python3 scripts/check_receipt_dates.py      # 今日の時点
    python3 scripts/check_receipt_dates.py 2026/12/31

支払期限は月度の翌月末。12月度の入金日は翌年の 入金日_YYYY.csv に書く（build_books.py と同じ）。
入金日CSVを使い始めた FIRST_YEAR より前の売上は見ない（前年までの決算で入金済みとして扱う）。
見つかれば1件1行で出力し、終了コード1で終わる。0件なら何も出さず0で終わる。
SessionStart hook（.claude/hooks/deposit-date-reminder.py）と /update-expenses から呼ばれる。
"""

import re
import sys
from datetime import date
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_books import ROOT, load_receipt_dates, next_month_end, read_rows, rel, to_int  # noqa: E402

FIRST_YEAR = 2026  # 入金日_YYYY.csv を使い始めた年


def unconfirmed(today):
    """[(月度 'YYYY/MM', 取引先, 金額, 支払期限, 書く先の入金日CSV)]。支払期限が today より前のものだけ。"""
    out, receipts = [], {}
    for path in sorted(ROOT.glob("[0-9][0-9][0-9][0-9]/売上高_*.csv")):
        year = int(path.parent.name)
        if year < FIRST_YEAR:
            continue
        rows = read_rows(path)
        clients = rows[0][1:-1]
        for r in rows[1:]:
            mm = re.fullmatch(rf"{year}/(\d\d)", r[0]) if r else None
            if not mm:
                continue
            m = int(mm.group(1))
            due = next_month_end(year, m)
            if due >= today:
                continue
            target = year + 1 if m == 12 else year
            if target not in receipts:
                receipts[target] = load_receipt_dates(target)
            dates, csv_path = receipts[target]
            for client, v in zip(clients, r[1:1 + len(clients)]):
                amount = to_int(v)
                if amount and (r[0], client) not in dates:
                    out.append((r[0], client, amount, due, rel(csv_path)))
    return out


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    mm = re.fullmatch(r"(\d{4})/(\d{1,2})/(\d{1,2})", arg)
    if arg and not mm:
        sys.exit(__doc__)
    today = date(*map(int, mm.groups())) if mm else date.today()
    rows = unconfirmed(today)
    for month, client, amount, due, path in rows:
        print(f"{month}月度 {client} {amount:,}円 支払期限 {due:%Y/%m/%d} → {path}")
    sys.exit(1 if rows else 0)


if __name__ == "__main__":
    main()
