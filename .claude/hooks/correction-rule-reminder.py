#!/usr/bin/env python3
"""訂正削除履歴.csv に行が増えたら、終了時に「ルール化するか」を Claude に促す hook。

  SessionStart: python3 correction-rule-reminder.py start  … その時点の行を記録
  Stop:         python3 correction-rule-reminder.py stop   … 増えた行があれば decision=block で差し戻す

同じ訂正で何度も止めないよう、差し戻したら基準を現在の行に更新する。
"""
import csv
import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path(__file__).resolve().parents[2])
LOG = PROJECT / "訂正削除履歴.csv"
STATE_DIR = Path(tempfile.gettempdir()) / "claude-correction-baseline"
MAX_LISTED = 10


def read_rows():
    """ヘッダ行（対象ファイル,…）より後のデータ行を返す。"""
    if not LOG.exists():
        return []
    with LOG.open(encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    for i, row in enumerate(rows):
        if row and row[0] == "対象ファイル":
            return [r for r in rows[i + 1:] if any(c.strip() for c in r)]
    return []


def key(row):
    return ",".join(row)


def save(path, rows):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([key(r) for r in rows], ensure_ascii=False), encoding="utf-8")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    payload = json.load(sys.stdin)
    state = STATE_DIR / f"{payload.get('session_id', 'unknown')}.json"
    rows = read_rows()

    if mode == "start":
        # resume / compact でも同じ session_id で呼ばれる。基準を上書きすると未処理の訂正を見落とす
        if not state.exists():
            save(state, rows)
        return

    if mode != "stop" or payload.get("stop_hook_active"):
        return
    if not state.exists():
        # hook 導入前に始まったセッション。ここから先を見る
        save(state, rows)
        return

    seen = set(json.loads(state.read_text(encoding="utf-8")))
    new = [r for r in rows if key(r) not in seen]
    if not new:
        return
    save(state, rows)

    lines = []
    for r in new[:MAX_LISTED]:
        target, _, action, reason = (r + [""] * 4)[:4]
        lines.append(f"- {target}（{action}）: {reason}")
    if len(new) > MAX_LISTED:
        lines.append(f"- ほか {len(new) - MAX_LISTED} 件")

    reason = (
        f"訂正削除履歴.csv にこのセッションで {len(new)} 件の訂正が追加されました。\n"
        + "\n".join(lines)
        + "\n\n同じ誤りを次回防げるか振り返ってください。\n"
        "1. 原因を1行でまとめる（読み取り誤り・分類ルールの穴・命名揺れ・誤格納など）\n"
        "2. CLAUDE.md / .claude/skills / scripts に、それを防ぐルールやチェックが既にあるか確認する\n"
        "3. 既にある・一過性なら「ルール化不要（理由）」と一言で終える\n"
        "4. 無ければ、反映先と追記文案（1〜3行）をユーザーに示して承認を待つ。勝手に書き換えない\n"
        "   （CLAUDE.md・skill・hook・scripts の変更は、ユーザーが承認してから行う）"
    )
    print(json.dumps({"decision": "block", "reason": reason}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # hook の失敗で作業を止めない
        print(f"correction-rule-reminder: {e}", file=sys.stderr)
