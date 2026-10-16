#!/usr/bin/env python3
"""起動時に、支払期限を過ぎたのに入金日を書いていない売上があれば、通帳のスクショを頼むよう促す SessionStart hook。

判定は scripts/check_receipt_dates.py に任せる（二重管理しない）。同じ日に何度も促さない。
"""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
# 同じ日に何度も促さないための印。プロジェクトごとに分ける
STAMP = (Path(tempfile.gettempdir()) / "claude-deposit-date-reminder"
         / hashlib.sha1(str(PROJECT.resolve()).encode()).hexdigest()[:12])

sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT / "scripts"))


def main():
    today = dt.date.today()
    if STAMP.exists() and STAMP.read_text().strip() == today.isoformat():
        return
    from check_receipt_dates import unconfirmed
    rows = unconfirmed(today)
    if not rows:
        return

    months = sorted({m for m, *_ in rows})
    status = f"入金日が未確認の売上が {len(rows)}件（{'・'.join(m[5:].lstrip('0') + '月度' for m in months)}。支払期限を過ぎた）"
    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(today.isoformat())

    detail = "\n".join(f"- {m}月度 {c} {a:,}円（支払期限 {d:%Y/%m/%d}）" for m, c, a, d, _ in rows)
    context = (
        f"{status}。\n{detail}\n"
        "ユーザーの最初の依頼に区切りがついたら、該当月の入出金明細のスクリーンショット（銀行アプリ）を"
        "送ってほしいと一言だけ頼む。受け取ったら /recording-deposit-dates の手順で入金日CSVに書く。"
        "ユーザーが後でと言ったらやめる（同じ日のうちは再度促されない）。"
    )
    print(json.dumps({
        "systemMessage": f"{status}。通帳のスクショを送ると入金日を記録します",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:  # hook の失敗（入金日CSVが読めない等の中止も含む）で起動を止めない
        print(f"deposit-date-reminder: {e}", file=sys.stderr)
