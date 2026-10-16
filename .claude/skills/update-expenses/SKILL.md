---
name: update-expenses
description: Orchestrator skill. Scans the `images/` folder for unsorted receipt images, chains `/sorting-receipt-images` → `/summarizing-monthly-expenses` to process and update both monthly and annual ledgers in one go. Use when the user asks to "update expenses", "最新に更新", "refresh ledger", or otherwise wants the full pipeline to run end-to-end.
argument-hint: [subfolder (optional, e.g. "4月")]
disable-model-invocation: true
allowed-tools: Read, Glob, Bash(python3 scripts/*), Bash(git add:*), Bash(git commit:*), Skill
---

## 経費データ一括更新（監督skill）

`images/` 配下の未仕分け画像を検出し、仕分け → 月次集計 → 年間台帳更新までを連鎖実行する。

### ワークフロー

1. **未仕分け画像の検出**
   - `images/`（引数があれば `images/$ARGUMENTS/`）配下のJPG/PNG/PDFを列挙
   - `YYYYMMDD_*_*.{ext}` のパターンに合致しないファイル（=未仕分け）のみ対象
   - 件数を表示。0件なら手順2〜4をスキップして手順5へ進む（定期項目と入金日の確認だけ行う）

2. **仕分けskillを起動**
   - `sorting-receipt-images` skill を呼び出す（引数は本skillの引数をそのまま渡す）
   - ユーザー確認後、リネーム・移動が完了するまで待つ

3. **影響を受けた月の特定**
   - 移動先のファイル名 `YYYYMMDD_*` から対象月（YYYY/MM）を抽出
   - 重複排除して一覧化（例: `2026/04`, `2026/05`）

4. **月次台帳の再生成**
   - 手順3で特定した各月について `summarizing-monthly-expenses YYYY/MM` を順次呼び出す
   - 各skillは内部で年間台帳 `YYYY/管理台帳_YYYY.csv` も更新する。途中のコミットはさせない（手順8でまとめる）

5. **定期項目の追従確認**
   - 手順4を実行したときは `summarizing-monthly-expenses` が内部で実施する
   - 手順4を飛ばしたときは、CLAUDE.md「領収書が発生しない定期項目」の表のCSVを開いて最終記載月を確認する
     （遅れていたら、ユーザーに引落実績を確かめてから追記する）
   - 遅れがあれば本skillの完了レポートにも明記する

6. **入金日の追従確認**（通帳のスクショが無いと進まない項目）
   - `python3 scripts/check_receipt_dates.py` を実行し、支払期限を過ぎたのに入金日がない売上を確認する
   - 0件なら何もしない。1件以上なら完了レポートに載せ、該当月の入出金明細のスクショを頼む
   - スクショを受け取ったら `recording-deposit-dates` skill の手順で記録する（このskill内で再実装しない）

7. **帳簿とダッシュボードの再生成**
   - `python3 scripts/build_books.py YYYY` → `python3 scripts/build_dashboard.py`
   - 何も変わっていなければ（仕分け0件・定期項目と入金日の追記なし）飛ばす

8. **git commit**（変更があれば必須）
   - `git add -A && git commit -m "<日本語の要約>"`（この依頼の変更をまとめて1コミット）
   - ユーザーの指示を待たない。金額が動いた場合は理由をメッセージに書く
   - push は指示があったときのみ

9. **完了レポート**
   - 下記フォーマットで結果を表示

### 完了レポート（厳守）

```
## 経費更新レポート

### 仕分け
- 処理: XX件
- スキップ（読取不可等）: Y件

### 台帳更新
- 月次: 2026/04, 2026/05（2件）
- 年間: 2026

### 定期項目（領収書なし）
- 定期経費: 8月まで反映済 / 掛金: 7月まで（8月は引落日未到来）

### 入金日（通帳）
- 未確認: 8月度 B社（架空）（支払期限 9/30 経過）→ 9月の入出金明細のスクショを送ってください

### コミット
- abc1234 8月分45件を仕分け

### 次のアクション
- 検証するなら `/verifying-expense-entries 2026/04` を実行
```

### 注意事項

- **集計・仕分けロジックを本skill内で再実装しない**（既存skillへ委譲）
- 未仕分け画像が0件で、定期項目・入金日にも追記がなければ「更新対象なし」と報告して終了
- ユーザーが仕分け提案を拒否した場合は手順3以降をスキップ
- エラーが出た場合はどの手順で止まったかを明示
