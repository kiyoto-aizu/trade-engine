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
  - 日足：`2021-10-21` 以降（ローリング。判定は doc 18 §2-2）
  - 分足：`2024-10-21` 以降（ローリング。判定は doc 18 §2-2）
- 実行は Windows / `.venv` 前提
- APIキーは環境変数 `JQUANTS_API_KEY` で設定済み
- 実行日は `2026-10-23` 時点

---

## 0. 事前チェック（必須）

### 0-1. 作業ブランチと差分確認
- 未コミット差分がある場合はそのままでよいが、記録しておく
- 実行前後で余計なファイルが増えていないか追跡できる状態にする

### 0-2. APIキー
- 実APIパイロットを実施する場合のみ、運用手順に従い設定する（値を表示・記録しない）
- 合成データでのテストではAPIキーを設定せず、ネットワークを使用しない

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
- 未許可の未知項目出現（`ExRT` と `MktCap` は確認済み・canonical未対応の記録対象であり、Holdにしない）

---

## 1. コマンド（PowerShell）

実CLIは `src.entrypoints.ingest_jquants_single_day`。以下は実APIを実行する場合の例であり、
この手順書の更新・テストでは実APIを呼び出さない。

### 1-1. 日足（全銘柄1日）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.ingest_jquants_single_day `
  --dataset daily `
  --date 2026-10-22 `
  --refresh
```

### 1-2. 分足（全銘柄1日）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.ingest_jquants_single_day `
  --dataset minute `
  --date 2026-10-22 `
  --refresh
```

### 1-3. 通信なし再変換確認（任意）
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.ingest_jquants_single_day `
  --dataset daily `
  --date 2026-10-22 `
  --canonical-only
```

> `--refresh` と `--canonical-only` は同時指定しない。

---

## 2. 実行中に見る項目

- ステータス推移（RUNNING / COMPLETE / EMPTY_UNKNOWN / EMPTY_HELD / PARTIAL / FAILED）
- ページ数、行数、銘柄数
- リクエスト数、HTTPステータス別件数、リトライ回数、429件数
- 待機合計秒数
- 取得 / raw検証 / 変換 / canonical検証 / 合計の所要時間
- raw/canonical の保存サイズ
- ピークメモリ（Python標準ライブラリの計測範囲。取得できない場合は `未計測`）
- 未対応フィールドごとの存在件数・null件数（値・株価レコードは記録しない）
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
- 未許可の未知項目あり
- 警告が想定より多い

### 失敗（No-Go）
- 認証/契約エラー
- 429で再試行枯渇
- 品質検証エラー
- ディスク不足
- 公開確定に失敗

---

実行計測JSONは `data/jquants/metrics/<dataset>/date=<date>/`（既定。`JQUANTS_EXECUTION_METRICS_DIRECTORY` で変更可）に実行ごとに保存される。
この保存先はGit管理外。

実行計測の一覧化（読み取り専用・通信なし・Git管理外の `data/jquants/metrics_summary.csv`（既定。metrics ディレクトリを変えた場合はその親）に出力）:
```powershell
.\.venv\Scripts\python.exe -m src.entrypoints.summarize_jquants_metrics `
  --daily-elapsed-limit 60 --minute-elapsed-limit 600 [--fail-on-no-go]
```
- 上限は設計書18 §4 の固定値（daily 60秒、minute 600秒。実測4回を根拠に、2026-10-25 に人間承認）。実行が10回程度たまったら見直す
- 出力は検証済みの数値と列挙コードのみ（株価レコード・銘柄コード・自由文字列は含まない）
- `partial_verdict` は実装済み項目だけの部分判定。`not_evaluated` 列の項目は評価されていない
- 所要時間の上限（wall-clock の total。429待機などの待ちも含む）が未指定だと Hold になる。参考として `active_seconds` 列（total − wait_seconds）を出す
- `--fail-on-no-go` は、各 dataset×date の最新試行が No-Go のとき終了コード3（metrics ディレクトリが無い場合は4）
JSONにはリクエスト/ページ数、HTTPステータス別件数、リトライ、
429、待機時間、処理時間、保存サイズ、ピークメモリ、フィールドごとの件数/null件数を記録し、
APIキー・認証情報・raw本文・株価レコードは含めない。

同じ日付の日足・分足canonicalが両方公開済みなら、出来高>0の日足銘柄数、分足銘柄数、
片方だけに存在する銘柄数と、日足OHLCが全てnullかどうかの内訳を追記する。
この比較は情報記録のみで、公開可否に使わず、差異の原因も断定しない。

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
- http_status_counts:
- retries_total:
- http_429_count:
- rows_total:
- symbols_total:
- duplicates_exact:
- duplicates_conflict:
- unknown_fields_count:
- unknown_field_metrics: （項目ごとのcount/null_count。値は記載しない）
- raw_size_mb:
- canonical_size_mb:
- elapsed_fetch_sec:
- elapsed_raw_validate_sec:
- elapsed_transform_sec:
- elapsed_canonical_validate_sec:
- elapsed_total_sec:
- peak_memory_mb: （既定は未計測。計測する場合のみ --measure-peak-memory を付与。所要時間が悪化するため、時間の比較には使わない）
- metrics_json:
- free_disk_before_gb:
- free_disk_after_gb:

## 判定
- Go / Hold / No-Go
- 理由:

## 追記
- エラー要約（機密情報なし）:
- 次アクション:

---

## 4-2. 日次更新・過去補填の運用ルール（案）
詳細は `docs/detail_design/18-jquants-ops-orchestration.md`。

- 優先度: 日次更新（日足→分足）> 再開 > 過去補填。すべて直列で、日足と分足は同時に走らせない
- ロック取得に失敗したジョブは待機せずスキップして記録する（稼働中の処理を奪わない）
- 補填は新しい日付から契約境界へ遡り、1日分（dataset×date）を最小単位とする
- 停止条件: 401と契約範囲内の403は全ジョブ停止（停止状態を保持し、人間が解除）。429・連続FAILED・ディスク不足・品質検証失敗の扱いはジョブ種別ごとに設計書18 §2 の表に従う
- 同時実行の防止: 実行基盤で「実行中なら新規開始しない」設定にする（別プロセスの重複はAPI枠を倍消費する）
- 再開手順:
  1. 未完了（PENDING/RUNNING/PARTIAL/FAILED）に加え、要確認（EMPTY_UNKNOWN/EMPTY_HELD）も確認し、RUNNINGは稼働中でないことを確認する
  2. 原因を記録（機密情報なし）する
  3. 同じ `--date` を `--refresh` で再実行する（変換のみ失敗した場合は `--canonical-only`）
  4. 失敗時は旧公開世代のままで、成功扱いにしない
- 補填の1日予算・閾値は初期値で、実測後に見直す（未確定）

## 4-3. 補填の実行後に見る確認手順（方式は設計書18 §1-1・§2-1 で決定済み・Issue #49 人間承認済み）
実装は `src/entrypoints/run_jquants_backfill.py`（既定は dry-run、`--execute` で実API）。状態は `JQUANTS_BACKFILL_STATE_DIRECTORY` の `backfill_state.json`（停止フラグ・当日停止履歴・要確認リスト・連続FAILED）と `backfill.lock`。いずれも Git 管理外。出力は日付とカテゴリのみ。

### ① outcome 別の対処
| outcome | 終了コード | 意味 | 対処 |
|---|---|---|---|
| COMPLETED | 0 | 対象日をすべて処理 | 対処なし。台帳で日付を確認 |
| COMPLETED_WITH_REVIEW | 0 | 完了したが要確認の日付が残る | 出力の `attention` / `review_excluded` の日付を確認（②の手順） |
| BUDGET_EXHAUSTED | 0 | 1日予算・`--max-days` で正常終了 | 次回実行で続きから進む。対処なし |
| STOPPED | 1 | 停止条件（401/403/400/ディスク不足/品質検証失敗など） | 出力の理由コードを確認し原因を記録。フラグが立っていれば②へ |
| STOPPED_TODAY | 2 | 当日停止（429・連続FAILED・5xx継続など） | 翌日以降の実行で再開。同一原因が3走行日連続ならフラグが立つ |
| BLOCKED_BY_FLAG | 2 | 停止フラグにより開始せず | ②の手順で原因確認後、人間が解除 |
| LOCK_SKIPPED | 2 | 別ジョブがロック保持中で待たずにスキップ | ③を確認。稼働中でなければロック削除 |

### ② 停止フラグと要確認リストの違い
- 停止フラグ（`stop_flag`）: 補填全体（`ALL_FLAG` は全ジョブ）を止める。解除は人間のみ: 原因確認 → ジョブが動いていないことを確認 → `--clear-stop-flag`。エージェント・スケジューラは解除しない
- 要確認リスト（`review`）: 日付単位で補填対象から外す（403・範囲内外の400・未知status）。他の日の補填は続く。出力の `review_excluded` に出る。ただし範囲内400・未知statusが同一原因で3走行日連続すると `REPEATED_REVIEW_STOP` で停止フラグが立つ（系統的な不具合。原因を直してから、要確認の日付と併せて解除する）
- 要確認の解除手順（専用コマンドは未実装・持ち越し）: ①原因（契約範囲の定数・権限・実装不具合）を解消、②ジョブが動いていないこと（③）を確認、③`backfill_state.json` をバックアップ、④該当日付を `review` の該当 dataset の配列から削除、⑤dry-run で対象に戻ったことを確認してから実行。契約範囲外の400は `CONTRACT_YEARS` 更新（設計書18 §2-2）まで解除しない

### ③ 残った backfill.lock
- LOCK_SKIPPED が続く場合、補填プロセスが実行中でないこと（タスクスケジューラ／プロセス一覧）を確認する
- 稼働中でなければ `backfill.lock` を削除する。稼働中のロックは削除しない（API枠を倍消費する）
- `--clear-stop-flag` はロックを取って解除する。補填の実行中は失敗（終了コード1）するので、実行終了後にやり直す。dry-run はロックを取らない（読み取りのみ）

### ④ RUNNING が残った日
強制終了などで台帳が RUNNING のまま残る。稼働中でないことを確認し、同じ `--date` を `--refresh` で再実行する（4-2 の再開手順）。補填ジョブは自動再実行しない。

### ⑤ ディスク空き
- 補填は保存先の空きが10GB未満で補填停止（`DISK_FLOOR`）。判定は raw と canonical の両ボリューム
- 分足は1日の容量が大きく、日足より先に下限へ近づく。実行前に 0-3 で空きを確認し、近い場合は先に容量を確保する
- 空きが危険域なら全ジョブ停止フラグ（`DISK_SPACE_CRITICAL`）になる。空き確保後に人間が解除する

### ⑥ 状態ファイル破損時
- `backfill_state.json` が読めない・形式が古い（旧版の当日停止履歴はリスト形式）場合は `BackfillStateError` で実行せず、フェイルクローズで止まる
- 勝手に削除・再作成しない。内容を確認して保全し、停止フラグ・要確認リストの内容を把握したうえで修復または再作成する（再作成は停止フラグと要確認リストが消えるため、再開前に台帳と出力から復元する）
- 書込失敗時は `BackfillStateError`（not writable）で実行が失敗（終了コード1）し、続行しない。ただし停止理由が永続化されていない可能性があるため、出力の停止理由を記録し、書込先（権限・空き）を解消してから再実行する

### ⑦ 通知
現状 Slack critical は429打ち切りのみ。停止フラグ設定時などの通知は未実装（スケジューラ側 Issue へ持ち越し）。実行後は出力と終了コードを必ず確認する。

## 5. 注意事項
- APIキー、認証ヘッダー、raw本文、ページキーをログや報告に貼らない
- 実行計測JSONやIssueコメントに株価レコード・銘柄コードを貼らない
- `trade-report` に有料データ生値・復元可能な派生を出さない
- パイロット中は日足と分足を同時に走らせない
- 失敗時は再実行前に原因を記録してから実施する