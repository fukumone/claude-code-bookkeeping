---
name: recording-deposit-dates
description: Reads bank-app statement screenshots (入出金明細), matches each sales deposit to 売上高_YYYY.csv by month and client, and records the confirmed date in `YYYY/帳簿/入金日_YYYY.csv`, then rebuilds the books and dashboard and commits. Use when the user sends bank or 通帳 screenshots, says 入金の画像, or a SessionStart hook reports unconfirmed deposit dates.
allowed-tools: Read, Write, Edit, Bash(python3 scripts/*), Bash(git add:*), Bash(git commit:*), Bash(git status:*), Bash(git diff:*), Bash(git log:*)
---

## 入金日の記録

通帳アプリのスクショから売上の入金日を読み、`YYYY/帳簿/入金日_YYYY.csv` に書く。
入金先の口座を個人の口座として扱う設定（`scripts/build_books.py` の `DEPOSIT_ACCOUNT`）なので、
**記録するのは売上の入金日だけ**。

### 手順

1. **未確認の一覧を出す**: `python3 scripts/check_receipt_dates.py`
   （支払期限を過ぎたのに入金日がない売上。0件でも、スクショに未記録の月があれば続ける）
2. **スクショを読む**: 各行の 日付・振込名義・金額 を読む
3. **売上と突き合わせる**: 入金は月度の翌月末ごろに来る（例: 4月度 → 5/29）
   - 振込名義 → 取引先（売上高CSVの列名）の対応は下の表を使う。表にない名義はユーザーに聞く
   - **金額が売上高CSVのその月度と一致したものだけ** 記録する。合わなければ書かずにユーザーに聞く
4. **CSVに書く**: 月度順に `月度,取引先,入金日` の行を足す（例: `2026/04,A社（架空）,2026/05/29`）
5. **再生成**: `python3 scripts/build_books.py YYYY` → `python3 scripts/build_dashboard.py`
   （出力の注記の「仮置き」件数が減ったか見る）
6. **コミット**: `入金日_YYYY.csv`・`YYYY/帳簿/`・`帳簿.html`・`dashboard.html` をまとめて1コミット。
   金額は変わらないので、メッセージには「どの月度を確定したか」と「日付が仮置きから動いた月度」を書く

### 振込名義と取引先の対応（自分の取引先に書き換える）

| 振込名義（通帳の表示） | 取引先（売上高CSVの列名） |
|---|---|
| `カ)エーシヤ`（例） | A社（架空） |
| `カ)ビーシヤ`（例） | B社（架空） |

### 書いてよい日付・書かない日付（厳守）

- **行の日付と金額が写っている入金だけ** を確定とする。残高から入金があったと推定できるだけ・行が画面外で切れている場合は
  書かず、その行が写るスクショを頼む
- ユーザーが「確定で」と言った場合だけ書いてよい。注記に「本人確認による入金日（YYYY/MM/DD時点）」と明細で確認できた範囲を残す
- **前年12月度の入金**は翌年のファイルに書く。ただし期首残高に売掛金がない年（前年の決算で入金済みとして扱った年）は書かない。
  書くと build_books.py が「売上のない月度・取引先」で中止する
- 利息・キャンペーン金利・キャッシュバック・少額の確認振込・自分の口座への振替は **事業と関係ない** ので何もしない

### 個人情報

- 口座番号・支店名・残高を CSV やコミットメッセージに書かない（日付・金額だけ）
- スクショを外部に送らない・リポジトリに保存しない

### 完了レポート

```
## 入金日の記録
- 確定: 4〜7月度（7件）  ← 仮置きから日付が動いたもの: 4月度 5/31→5/29
- 未確認: 7月度 B社（架空）（行の日付が写っていない）
- 仮置きの残り: 1件
- コミット: abc1234
```
