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

### 0-6. 実効保存先（API 呼び出し前・必須）
- 運用の保存先は raw / canonical / ledger / metrics / backfill state / calendar の全てが D: であること。
- 次の読み取り専用チェックを、実行予定の取込コマンドと同じ Python、作業ディレクトリ、保存先環境変数で実行する。チェックは `.env` を読み込まないため、6つの保存先変数はチェックと取込の両方で使うプロセス環境に設定し、`.env` にだけ設定しない。
  ```powershell
  .\.venv\Scripts\python.exe -m src.entrypoints.check_jquants_storage_config
  if ($LASTEXITCODE -ne 0) { throw "J-Quants storage check failed; ingestion was not started." }
  ```
- `status` が `PASS` であることを確認する。PASS は6つ全てがプロセス環境変数に明示され、絶対パスで D: 上と判定されたことだけを示す。1つでも未設定（`source=default`）なら、既定値が D: 上でもFAILEDとなる。D: の接続・存在・書込権限、ジャンクション・リンク・ドライブ割当の実体は確認しない。UNC、相対パス、ドライブ相対パスではPASSにならない。
- スケジューラ実行では、実際のタスクアカウント・作業ディレクトリ・環境変数で同じコマンドを確認する。チェック失敗時は取込を実行しない。リポジトリからスケジューラの実登録状態は確認できないため、タスク側の設定は人間が確認する。
- チェックは取込設定モジュールや `.env` を読み込まず、保存先の正規化パスと環境変数/既定値の別のみを表示する。他の環境変数値は参照せず、API通信・保存先作成・保存データ読み取りも行わない。絶対パスを含むため Issue・ログには貼らない。
- C: のパイロットデータは、2026-10-27 の削除記録後に生成されたもの、またはそのときの削除対象外だった関連ファイルが残っている場合も原本を維持し、読み取り専用の所在・件数・ハッシュ棚卸しだけを行う。D: へのコピー/移行や削除は、別途明示承認を得るまで行わない。

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
- 上限は設計書18 §4 の固定値（daily 60秒、minute 600秒。実測7件（daily 4回・minute 3回）を根拠に、2026-10-25 に人間承認）。実行が10回程度たまったら見直す
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
- 補填は契約範囲の古い日付から進め、1日分（dataset×date）を最小単位とする
- 停止条件: 401と契約範囲内の403は全ジョブ停止（停止状態を保持し、人間が解除）。429・連続FAILED・ディスク不足・品質検証失敗の扱いはジョブ種別ごとに設計書18 §2 の表に従う
- 同時実行の防止: 日次・補填・手動単日（通常/refresh/canonical-only）は `JQUANTS_BACKFILL_STATE_DIRECTORY` の同じ `backfill.lock` を処理全体で保持する（Issue #54）。競合時は待機・奪取せず終了コード2。手動とスケジューラで状態ディレクトリが異なると排他・フラグを共有できないため、同じ設定を使う。スケジューラ環境の確認・登録は #37/#53 の範囲
- 再開手順:
  1. 未完了（PENDING/RUNNING/PARTIAL/FAILED）に加え、要確認（EMPTY_UNKNOWN/EMPTY_HELD）も確認し、RUNNINGは稼働中でないことを確認する
  2. 原因を記録（機密情報なし）する
  3. 同じ `--date` を `--refresh` で再実行する（変換のみ失敗した場合は `--canonical-only`）
  4. 失敗時は旧公開世代のままで、成功扱いにしない
- 日次 `--date` の再実行は、補填の要確認リスト（403・400・未知status）にある dataset×date を `REVIEW_EXCLUDED` として除外し他の dataset は続行する。結果の `run_date` と各 outcome の `dataset` / `reason` を確認する。単日CLIの明示的な手動再実行に要確認リストだけによる阻止は追加しないが、原因確認は省略しない
- 補填の予算・閾値は承認済みの初期値を維持する。#37では日足/分足合計でJSTの1日7200秒、Hold時50%の共有ソフト予算とし、同日再起動でリセットしない。見直しには別途人間判断と実測が必要

## 4-3. 補填の実行後に見る確認手順（方式は設計書18 §1-1・§2-1 で決定済み・Issue #49 人間承認済み）
実装は `src/entrypoints/run_jquants_backfill.py`（既定は dry-run、`--execute` で実API）。状態は `JQUANTS_BACKFILL_STATE_DIRECTORY` の `backfill_state.json`（停止フラグ・当日停止履歴・要確認リスト・解除監査履歴・連続FAILED）と `backfill.lock`。いずれも Git 管理外。出力は日付と列挙カテゴリのみで、停止フラグは scope・reason・set_on を含む JSON オブジェクトとして出力する。

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
- 停止フラグ（`stop_flag`）: 補填全体（`ALL_FLAG` は全ジョブ）を止める。原因確認後、人間が解除する。補填結果の JSON では `stop_flag` に scope・reason・set_on が出る（未設定は `null`）。解除は `--clear-stop-flag` で行い、実行中は失敗する。エージェント・スケジューラは解除しない
- Issue #54: 設定済み `ALL_FLAG` は日次更新・過去日の `--date`・単日CLIの通常/`--refresh`/`--canonical-only` を取得・公開処理前に止める（終了コード2、`BLOCKED_BY_FLAG` と理由コード）。`BACKFILL_FLAG` は日次・単日を止めない。状態の破損・読取り不能・ロック作成不能は失敗（終了コード1）であり、自動修復・解除しない。新たな日次/手動の401等からのフラグ永続化は未実装
- 要確認リスト（`review`）: 日付単位で補填対象から外す（403・範囲内外の400・未知status）。他の日の補填は続く。補填結果の `review_excluded` に出る。ただし範囲内400・未知statusが同一原因で3走行日連続すると `REPEATED_REVIEW_STOP` で停止フラグが立つ（系統的な不具合。原因を直してから、該当する要確認項目と停止フラグをそれぞれ解除する）
- 要確認の一覧（通信なし・状態変更なし。読み取り中に補填処理が状態ファイルを置き換えないよう、全体ロックを取る。補填・日次・単日処理中は失敗し、再実行する）:
  ```powershell
  .\.venv\Scripts\python.exe -m src.entrypoints.manage_jquants_backfill_review list
  .\.venv\Scripts\python.exe -m src.entrypoints.manage_jquants_backfill_review list --dataset daily
  ```
- 要確認の解除手順（人間のみ。エージェント・スケジューラは解除しない）: ①原因（契約範囲の定数・権限・実装不具合）を解消、②補填プロセスが動いていないことを確認、③`backfill_state.json` をバックアップ、④該当する dataset・日付・理由コードの1件を解除する:
  ```powershell
  .\.venv\Scripts\python.exe -m src.entrypoints.manage_jquants_backfill_review clear `
    --dataset daily --date 2026-10-28 --reason-code UNKNOWN_STATUS
  ```
  解除時は全体ロックを取る。補填がロックを保持している場合は失敗し、状態は変わらない。ロックを自動削除・奪取しない。解除成功時のみ、`backfill_state.json` の `review_clear_audit` に dataset ごとに対象日・操作日（JST）・理由コードを記録する。一覧表示や失敗した解除は記録しない。契約範囲外の400は `CONTRACT_YEARS` 更新（設計書18 §2-2）まで解除しない。解除後は `list --dataset <dataset>` を再実行し、解除した日付と理由コードの項目が一覧から消えたことを確認する。同じ dataset・日付に別の理由コードが残っている場合、その日付は引き続き要確認で補填対象から除外される。dry-run では要確認リストの解除を確認できない
- `REPEATED_REVIEW_STOP` が設定されている場合は、原因を解消して要確認項目を確認した後、上記の要確認解除と `--clear-stop-flag` を別々に実行する。要確認解除だけでは停止フラグは解除されない

### ③ 残った backfill.lock
- LOCK_SKIPPED が続く場合、補填プロセスが実行中でないこと（タスクスケジューラ／プロセス一覧）を確認する
- 稼働中でなければ `backfill.lock` を削除する。稼働中のロックは削除しない（API枠を倍消費する）
- `--clear-stop-flag`・要確認の `clear`・要確認の `list` と dry-run の状態読み取りは、ファイル読み書きの競合を避けるため全体ロックを取る。補填・日次・手動単日の実行中は失敗（終了コード1）し、状態を変更・読み取りせず、実行終了後に再実行する。`LOCK_SKIPPED` の補填結果はロックを取得できていないため stop flag を読み取らず、`stop_flag_status=UNKNOWN_LOCK_HELD` として不明を明示する

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
429打ち切りは取込側で通知し、実行基盤で重複送信しない。#37は日次/カレンダーの非成功・要確認を通知する。補填フラグ設定・BackfillStateError・連続LOCK_SKIPPEDの通知は#53で実装/統合するまで無人化不可。実行後は出力と終了コードを必ず確認する。

## 4-4. J-Quantsタスク登録・解除・手動再開（#37）

以下は**別途承認を得た人間向けの手順**。コード/モック検証の承認だけで実行しない。今回の実装で実タスク・実API/Slack・運用データ・環境変数を変更したことにはならない。

### 事前ゲート

- #37の独立Claude第三者レビュー・指摘解消・人間Go、#53の補填通知統合、#12最終Goを確認する。補助code-reviewは独立第三者レビューの代替ではない。
- 安定した実行checkout/venv/アカウント、WindowsのTokyo Standard Timeと時計、ログオン中の実行条件を確認する。一時worktreeが削除されると登録パスは無効になる。エージェントがmain checkoutを参照/登録する手順にはしない。
- 実タスクと同じPython・作業dir・アカウント・環境で0-6の6先監査PASSを確認する。確認済み手動環境とスケジューラ環境は別物。PASSはD:絶対パス・environmentの字句検証だけで、実体・接続・書込権限・台帳raw_directory整合を別確認する。権限確認の書込試験にも許可が必要。
- C: pilot原本とD:の照合/必要な移行、state/calendarの継承を判断する。未発見を未生成の証拠にしない。元state削除/初期化で停止や要確認履歴を失わせない。
- カレンダーは日足契約範囲5年分から当日までを取得する。通信量・応答範囲・所要時間とバックアップ/復旧運用を確認し、実API/Slack少量試運転の別許可を得る。

### 定義の確認と無効登録

読み取りとWhatIfにも運用PCへのアクセス許可が必要。アカウントは人間が確定する。

```powershell
.\scripts\tasks\register_jquants_task.ps1 -Account '<実行アカウント>' -WhatIf
# 実登録の承認後だけ実行。登録しても無効のまま
.\scripts\tasks\register_jquants_task.ps1 -Account '<実行アカウント>'
Get-ScheduledTask -TaskPath '\' -TaskName 'trade-engine-jquants'
```

17:00 JSTに単一タスクでカレンダー→日次（日足→分足）→両方COMPLETEかつ評価完了Go時だけ補填を実行する。日次開始はカレンダー取得分だけ17:00より後になる。休場/未公開/空結果/ATTENTION/review/ロックスキップ/未評価なら補填を開始しない。OSのWakeToRun、開始漏れ後追い、自動再試行は無効、Interactive/Limitedのためログオフ中は走らない。異なる実行条件が必要なら別の設計/許可とする。

**処理が固まった場合、Slack通知なしでロックと実行中タスクが残り、IgnoreNewにより翌日以降も新規起動されない可能性がある。** Python自体が起動されないため、連続ロックスキップ通知も検知できない。強制終了しない承認済み方針を維持し、人間が毎日タスクの最終起動・実行中時間・完了とローカルイベントを確認する。期待する完了がなければ無通知でも異常として扱い、稼働を確認せずロックを削除しない。

同名定義の更新は現定義/稼働を確認後、別の変更承認を得て `-Replace` を指定する。更新時も無効登録。アカウントやパスの違いを黙って上書きしない。

### 手動確認と有効化

```powershell
# 通信/運用state操作なしの計画表示
.\scripts\tasks\run_jquants.ps1
# 実API/運用データ操作の別承認後のみ。無効タスクのまま手動確認する
.\scripts\tasks\run_jquants.ps1 -Execute
```

Pythonの運用イベント（設定metrics配下の `ops\events.jsonl`）と終了コードを確認する。取込metricsとは別の領域なので、全ファイルを取込サマリに混ぜない。runnerは漏えい防止のため生stdout/stderrを破棄し、`stage=RUNNER`と終了コードだけ表示する。非0/通知失敗/記録不能を成功扱いしない。タスク未起動やPython未起動ではSlackを送れないため、OS履歴とローカル出力を確認する。

イベントの `day` はJST実行日。過去日取得の429・単体カレンダー取得の対象日は `target_day` で区別する。カレンダー差異は `CALENDAR_MISMATCH`、不足は `CALENDAR_MISSING`、形式不正は `CALENDAR_INVALID` として記録し、人間が原因を確認する。APIエラーは許可された理由コードのみを残し、例外本文は記録しない。

実行アカウントでの手動確認が終わり、全ゲートを満たして**有効化の別承認**を得た後だけ人間がタスクを有効にする。コードは自動で有効化しない。無人化後も各営業日の起動/完了・要確認・通知/記録失敗を人間が確認する。

### 停止・解除と再開

```powershell
# 解除の別承認後。WhatIfで対象を確認する
.\scripts\tasks\register_jquants_task.ps1 -Remove -WhatIf
.\scripts\tasks\register_jquants_task.ps1 -Remove
```

- 解除は専用タスク定義のみ。現在の処理は停止せず、state/lock/データを消さない。新規起動を止めたうえで、現在のdataset×date処理が終了する境界を待つ。強制終了は通常の停止手順ではない。
- 残ったロックはプロセス/タスク状態を確認し、稼働なしを確定して人間が許可した場合だけ削除する。ロック競合で停止状態UNKNOWNなら「フラグなし」と判断しない。
- 日次STOP_TODAYはそのJST実行日の全dataset、STOP_JOBは該当datasetに効き、同日の手動refresh/canonical-onlyも阻止する。日次401/403/容量危険はALL_FLAG。既存のフラグ/要確認解除CLIを原因確認後だけ使う。
- 日次の3営業日連続昇格は非429のCONSECUTIVE_FAILED_3のみ。429/RATE_LIMIT_ABORTEDは昇格しないが、原因確認前に当日再取得しない。補填の既存昇格規則と混同しない。
- 連続FAILEDの3回は「実行記録の数」であり日数ではない。同日手動再実行でも3回に達すれば当日停止する。その当日停止が3営業日連続する場合の昇格とは別の判定である。
- 補填予算はJST日単位・日足/分足合計7200秒、Hold時50%。`backfill_state.json` の `shared_budget` に消費と `in_progress` を保持する。処理途中を強制終了しないソフト上限なので最後の1日分の超過はあり得る。異常終了で消費未確定の `in_progress` が残る場合は翌日も補填を止める。自動リセットや解除CLIは用意しない。タスクの新規起動を無効にし、稼働終了を人間が確認してstateをバックアップし、実消費を確定したうえで共有ロック下の手動復旧を別途承認する。実消費を確定できなければ再開しない。state全体の削除や再起動で予算を取り戻さず、フラグ・要確認・日次履歴を保持する。
- 未公開/空結果/失敗は自動再試行しない。人間の原因確認後に既存CLIで対象日を指定して再開する。翌日の定時日次が前日の欠損を自動補完する保証はない。

## 5. 注意事項
- APIキー、認証ヘッダー、raw本文、ページキーをログや報告に貼らない
- 実行計測JSONやIssueコメントに株価レコード・銘柄コードを貼らない
- `trade-report` に有料データ生値・復元可能な派生を出さない
- パイロット中は日足と分足を同時に走らせない
- 失敗時は再実行前に原因を記録してから実施する