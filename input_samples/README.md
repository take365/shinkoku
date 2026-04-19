# input_samples

`input_samples` は `shinkoku` 用のサンプル入力データ置き場です。

実データではなく、個人情報が特定されにくいように調整した架空データを置いています。
事業イメージは「フリーランス Web 制作 + 小規模デジタル販売」です。

## 構成

- `erpnext/`
  - ERPNext 形式の売上、仕入サンプル
- `mufg_bank/`
  - MUFG 口座 CSV サンプル
- `smbc_bank/`
  - SMBC 口座 CSV サンプル
- `sumitomo_visa/`
  - 三井住友 VISA カード CSV サンプル
- `view_card/`
  - ビューカード CSV サンプル
- `rakuten_card/`
  - 楽天カード CSV サンプル
- `aeon_card/`
  - イオンカード CSV サンプル

## 方針

- 売上入金は口座明細側で表現しています
- MUFG / SMBC には、重複候補や資金移動確認に使えるような対になる入出金も含めています

## 使い方

- `input_samples` 配下の CSV をそのまま importer にかけて動作確認できます
- 画面確認や検索条件の確認、重複候補の表示確認などに使う想定です
