# J-Quants 実APIパイロット手順書（1営業日・全銘柄）

## 目的
- 実装済みの単日取得フローを実APIで最小確認する
- 日足・分足を別々に1日分ずつ実行し、性能と品質を計測する
- 失敗時に安全に停止し、公開データを壊さないことを確認する

---

## 前提
- 対象リポジトリ：`kiyoto-aizu/trade-engine`
- プラン：Light + 分足アドオン
- 契約境界（確認時点）
  - 日足：`2021-10-21` 以降
  - 分足：`2024-10-21` 以降
- 実行は Windows / `.venv` 前提
- APIキーは環境変数 `JQUANTS_API_KEY` で設定済み
- 実行日は `2026-10-23` 時点

---

## 0. 事前チェック（必須）

### 0-1. 作業ブランチと差分確認
- 未コミット差分がある場合はそのままでよいが、記録しておく
- 実行前後で余計なファイルが増えていないか追跡できる状態にする

### 0-2. APIキー確認（値は表示しない）
- 存在のみ確認する（値のecho禁止）

### 0-3. ディスク空き確認
- raw + canonical + staging 分の空きが十分あること
- 最低でも数GB以上を推奨（分足日のため）

### 0-4. 実行対象日の決定
- 日足用：営業日 `D1`（契約範囲内）
- 分足用：営業日 `D2`（契約範囲内、D1と別日推奨）

### 0-5. 停止条件の確認
以下に該当したら即停止し、結果を記録する：
- 401 / 403 / 契約範囲外
- 429多発で時間上限超過
- 品質検証失敗
- ディスク不足
- 未知項目出現（方針上、人手確認まで後続停止）

---

## 1. コマンド（PowerShell）

> CLI名は実装に合わせて置き換えてください。  
> ここでは例として `src.entrypoints.run_jquants_single_day_ingestion` を使用します。

### 1-1. 日足（全銘柄1日）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.run_jquants_single_day_ingestion `
  --dataset daily `
  --date 2026-10-22 `
  --refresh
```

### 1-2. 分足（全銘柄1日）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.run_jquants_single_day_ingestion `
  --dataset minute `
  --date 2026-10-22 `
  --refresh
```

### 1-3. 通信なし再変換確認（任意）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.run_jquants_single_day_ingestion `
  --dataset daily `
  --date 2026-10-22 `
  --canonical-only
```

> `--refresh` と `--canonical-only` は同時指定しない。

---

## 2. 実行中に見る項目

- ステータス推移（RUNNING / COMPLETE / EMPTY_UNKNOWN / EMPTY_HELD / PARTIAL / FAILED）
- ページ数、行数、銘柄数
- リトライ回数、429発生の有無
- 取得時間 / 検証時間 / 変換時間
- raw/canonical の保存サイズ
- 警告件数（重複、未知項目、要確認）

---

## 3. 実行後の判定

### 成功（Go）
- raw確定・canonical確定まで到達
- FAILED/PARTIALなし
- 重大品質異常なし
- 期待したサマリが取得できる

### 要確認（Hold）
- EMPTY_UNKNOWN または EMPTY_HELD
- 未知項目あり
- 警告が想定より多い

### 失敗（No-Go）
- 認証/契約エラー
- 429で再試行枯渇
- 品質検証エラー
- ディスク不足
- 公開確定に失敗

---

## 4. 記録テンプレ（実行ごとに埋める）

## 実行ID
- dataset: daily / minute
- date: YYYY-MM-DD
- started_at:
- finished_at:
- operator:

## 結果
- status:
- raw_generation_id:
- canonical_generation_id:
- pages:
- requests:
- retries_total:
- http_429_count:
- rows_total:
- symbols_total:
- duplicates_exact:
- duplicates_conflict:
- unknown_fields_count:
- null_records_count:
- raw_size_mb:
- canonical_size_mb:
- elapsed_fetch_sec:
- elapsed_validate_sec:
- elapsed_transform_sec:
- elapsed_total_sec:
- peak_memory_mb:
- free_disk_before_gb:
- free_disk_after_gb:

## 判定
- Go / Hold / No-Go
- 理由:

## 追記
- エラー要約（機密情報なし）:
- 次アクション:

---

## 5. 注意事項
- APIキー、認証ヘッダー、raw本文、ページキーをログや報告に貼らない
- `trade-report` に有料データ生値・復元可能な派生を出さない
- パイロット中は日足と分足を同時に走らせない
- 失敗時は再実行前に原因を記録してから実施する