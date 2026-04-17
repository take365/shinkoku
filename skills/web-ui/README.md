# `/web-ui` -- Web UI の起動・案内・検索式生成

shinkoku の Web UI を参照用に使うためのスキルです。

## このスキルでできること

- Web UI の起動、停止、再起動
- 主要画面の案内
- 条件付き URL の生成
- `query` 検索式の生成
- サマリ画面から `仕訳一覧` へ降りる導線の説明

## 前提

- `shinkoku.config.yaml` は必須ではありません
- 必要なのは Web UI が参照する `db_path` と `fiscal_year` です
- config があればそこから読み、なければ DB パスと年度を明示して扱います
- 起動・停止の例は Linux 前提です

## 起動例

```bash
shinkoku web --db-path ./shinkoku.db --fiscal-year 2025 --port 8010
```

## 停止例

```bash
lsof -i :8010
kill <PID>
```

## このスキルでやらないこと

- 仕訳の追加
- 仕訳の修正
- 仕訳の削除

更新系は `/journal` スキルを使います。

## 主な画面

- `/journals` 仕訳一覧
- `/trial-balance` 勘定科目サマリ
- `/summary/monthly` 月次サマリ
- `/summary/description` 摘要サマリ
- `/summary/counterparty` 取引先サマリ
- `/source-links` 入力元サマリ
- `/pl` 損益計算書
- `/bs` 貸借対照表
- `/gl/{account_code}` 総勘定元帳

## 検索式の例

- `acc:5270`
- `cp:"ココナラ" cat:revenue`
- `desc:"業務委託売上" from:2025-01-01 to:2025-12-31`
- `from:2025-09-01 to:2025-09-30 cat:revenue,expense`
- `acc:5270 mfrom:10000 sort:date desc`

## 使いどころ

- 「この条件で仕訳一覧を開きたい」
- 「どの画面を見ればよいか知りたい」
- 「月次サマリから見える数字の中身を確認したい」
- 「画面が落ちているので再起動したい」
