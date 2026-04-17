---
name: web-ui
description: >
  This skill should be used when the user wants to start, stop, or inspect the
  shinkoku Web UI, explain which screen to use, or generate direct URLs and
  query expressions for filtering journals and summary drilldowns. Trigger
  phrases include: "Web UI", "画面を開く", "画面を起動", "再起動",
  "URLを作って", "検索式を作って", "どの画面を見ればよい", "月次サマリから見たい",
  "仕訳一覧を絞り込みたい", "勘定科目サマリを開きたい".
---

# Web UI Navigation & Operations

shinkoku の Web UI を参照用の画面として扱うためのスキル。
このスキルは以下を担当する。

- Web UI の起動、停止、再起動
- 主要画面の案内
- 条件付き URL の生成
- `query` 検索式の生成
- サマリ画面から仕訳一覧へ降りる導線の説明

このスキルでは **更新系の操作は行わない**。仕訳の追加・修正・削除は `/journal` スキルで扱う。

## 基本方針

- Web UI は参照・確認・絞り込みを主目的とする
- 更新系はエージェント経由で扱う
- 明細確認の基本導線は `仕訳一覧`
- 帳簿形式が必要なときだけ `総勘定元帳`

## 前提確認

1. Web UI 起動に必要なのは `db_path` と `fiscal_year`
2. `shinkoku.config.yaml` がある場合は、そこから `db_path` などを読む
3. `shinkoku.config.yaml` がない場合でも、DB パスと年度が分かっていれば起動できる
4. Linux 環境の Python / 仮想環境を前提に扱う
5. Web UI の既存待受がある場合は再利用するか、必要に応じて再起動する

## Web UI の起動・停止

### 標準ポート

- `http://127.0.0.1:8010/`

### 起動

標準の起動方法は CLI の `web` サブコマンドを使う。

```bash
shinkoku web --db-path ./shinkoku.db --fiscal-year 2025 --port 8010
```

または、仮想環境の Python が必要なら以下のように実行する。

```bash
. .venv/bin/activate
python -m shinkoku.cli web --db-path ./shinkoku.db --fiscal-year 2025 --port 8010
```

`shinkoku.config.yaml` は必須ではない。FastAPI アプリ自体は `create_app(db_path=..., fiscal_year=...)` で起動するため、必要なのは DB パスと会計年度だけである。

#### config がある場合

- `db_path` を設定から読む
- 必要なら `tax_year` などから年度を補う

#### config がない場合

- 明示的に DB パスと年度を決めて起動する
- 既存の運用では `shinkoku.db` と `FY 2025` を使っているため、迷う場合はその組み合わせを確認する

バックグラウンド起動時は、必要に応じて標準出力・標準エラーをログへリダイレクトする。

```bash
nohup shinkoku web --db-path ./shinkoku.db --fiscal-year 2025 --port 8010 > web8010.out.log 2> web8010.err.log &
```

### 停止

8010 番ポートの待受プロセスを確認して停止する。

```bash
lsof -i :8010
kill <PID>
```

### 再起動

1. 8010 番ポートの待受プロセスを停止
2. CLI の `web` サブコマンドで再起動
3. `/health` または対象画面に HTTP 200 で応答するか確認

## 主要画面

- `/journals`
  - 参照・絞り込みの中心画面
- `/trial-balance`
  - 勘定科目サマリ
- `/summary/monthly`
  - 月次サマリ
- `/summary/description`
  - 摘要サマリ
- `/summary/counterparty`
  - 取引先サマリ
- `/source-links`
  - 入力元サマリ
- `/pl`
  - 損益計算書
- `/bs`
  - 貸借対照表
- `/fixed-assets`
  - 固定資産
- `/duplicates`
  - 重複候補
- `/audit`
  - 監査ログ
- `/gl/{account_code}`
  - 総勘定元帳

## 画面の使い分け

### 基本

- 仕訳を絞って見たい → `/journals`
- 月別に全体感を見たい → `/summary/monthly`
- 科目ごとに見たい → `/trial-balance`
- 入力ファイルとの突合せをしたい → `/source-links`
- 帳簿形式で残高推移を見たい → `/gl/{account_code}`

### 詳細確認の考え方

- 摘要サマリ、取引先サマリ、月次サマリの明細は基本的に `仕訳一覧` へ降りる
- 勘定科目は `仕訳一覧` を基本導線とし、必要なときだけ `総勘定元帳`

## 検索式 (`query`) のルール

Google 検索風の軽い文法で、`/journals?query=...` に入れる。

- スペース区切り: AND
- `"..."`: フレーズ指定
- `-word`: 除外
- `acc:` 勘定科目コード
- `cat:` 区分
- `desc:` 摘要
- `cp:` 取引先
- `src:` source
- `file:` 入力元
- `from:` / `to:` 日付範囲
- `mfrom:` / `mto:` 金額範囲
- `sort:` と `asc` / `desc`

### `cat:` の値

- `revenue` = 売上
- `expense` = 費用
- `asset` = 資産
- `liability` = 負債
- `equity` = 純資産
- `revenue,expense` = 売上または費用

## URL 生成ルール

ユーザーが見たい内容から、まず `query` を作り、それを `/journals` に乗せる。

### 例

- 2025-09 の売上
  - `from:2025-09-01 to:2025-09-30 cat:revenue`
- ココナラの売上
  - `cp:"ココナラ" cat:revenue`
- 雑費 1 万円以上
  - `acc:5270 mfrom:10000`
- 摘要に「業務委託売上」を含む 2025 年分
  - `desc:"業務委託売上" from:2025-01-01 to:2025-12-31`
- 2025-09 の売上または費用
  - `from:2025-09-01 to:2025-09-30 cat:revenue,expense`

URL は以下の形で返す。

```text
/journals?query=<urlencoded query>
```

## サマリ画面からのドリルダウン

### 月次サマリ

- 月リンク → その月の全仕訳
- 売上 → `cat:revenue`
- 費用 → `cat:expense`
- 純利益 → `cat:revenue,expense`
- 月末資産 → `cat:asset`
- 月末負債 → `cat:liability`
- 月末純資産 → `cat:equity`

### 摘要サマリ

- `desc:"..."` を使って `仕訳一覧` へ遷移

### 取引先サマリ

- `cp:"..."` を使って `仕訳一覧` へ遷移

### 勘定科目サマリ

- `acc:XXXX` を使って `仕訳一覧` へ遷移
- 必要なら別導線で `総勘定元帳`

## 更新系の扱い

Web UI では更新しない。

- 変更依頼は `仕訳一覧` のコピー導線を使ってエージェントへ渡す
- 実際の仕訳変更、追加、削除は `/journal` スキルで扱う

## 期待される出力

- 起動依頼なら、起動結果と URL を返す
- 画面案内なら、使うべき画面を 1 つか 2 つに絞って返す
- URL 生成依頼なら、`query` と URL の両方を返す
- 条件が曖昧でも、危険でなければ合理的に補って URL を組む
