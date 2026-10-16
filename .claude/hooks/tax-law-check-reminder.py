#!/usr/bin/env python3
"""起動時に、税制改正チェックが30日以上空いていたら /tracking-tax-law-changes の実行を促す SessionStart hook。

docs/税制改正/YYYY-MM-DD_*.md の最新の日付で判定する。同じ日に何度も促さない。
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
REPORT_DIR = PROJECT / "docs" / "税制改正"
INTERVAL_DAYS = 30
# 同じ日に何度も促さないための印。プロジェクトごとに分ける
STAMP = (Path(tempfile.gettempdir()) / "claude-tax-law-reminder"
         / hashlib.sha1(str(PROJECT.resolve()).encode()).hexdigest()[:12])


def latest_report():
    dates = []
    for p in REPORT_DIR.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})_", p.name)
        if m:
            dates.append((dt.date.fromisoformat(m.group(1)), p.name))
    return max(dates) if dates else None


def main():
    today = dt.date.today()
    if STAMP.exists() and STAMP.read_text().strip() == today.isoformat():
        return

    latest = latest_report()
    if latest and (today - latest[0]).days < INTERVAL_DAYS:
        return

    if latest:
        status = f"前回の税制改正チェックから {(today - latest[0]).days} 日経過（{latest[1]}）"
    else:
        status = "税制改正チェックはまだ一度も実行されていない"

    STAMP.parent.mkdir(parents=True, exist_ok=True)
    STAMP.write_text(today.isoformat())

    context = (
        f"{status}。ユーザーの最初の依頼に区切りがついたら `/tracking-tax-law-changes` を実行する。"
        "始める前に「税制改正チェックを実行します（前回から◯日）」と一言添え、"
        "ユーザーが不要・後でと言ったらやめる（同じ日のうちは再度促されない）。"
    )
    print(json.dumps({
        "systemMessage": f"{status}。最初の依頼のあとで /tracking-tax-law-changes を実行します",
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context},
    }, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # hook の失敗で起動を止めない
        print(f"tax-law-check-reminder: {e}", file=sys.stderr)
