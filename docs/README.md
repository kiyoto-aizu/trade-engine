# ドキュメント案内

このページを`docs/`の入口として、資料の置き場所と機能ごとの設計書を案内します。

## フォルダの役割

| フォルダ | 内容 |
|---|---|
| [`architecture/`](./architecture/) | 全体構成・データフロー・実行スケジュール・通知・コーディング規約 |
| [`reference/`](./reference/) | 設定項目などの生成参照資料 |
| [`detail_design/`](./detail_design/) | 機能ごとの詳細設計書と[_template.md](./detail_design/_template.md) |
| [`adr/`](./adr/) | 設計上の決定記録 |
| [`tasks/`](./tasks/) | 未完了・完了の改修タスク |
| [`reviews/`](./reviews/) | コード・戦略のレビュー記録と検証結果 |
| [`prompts/`](./prompts/) | 現在は未配置。新しい実装依頼プロンプトの置き場 |
| [`archive/`](./archive/) | 現行仕様ではない過去資料・置き換え済みプロンプト |

### architecture資料

- [全体データフロー](./architecture/flow.md)
- [実行スケジュール](./architecture/schedule.md)
- [Slack通知](./architecture/notifications.md)
- [コーディング規約](./architecture/coding-guidelines.md)

## 機能と詳細設計書

`src/entrypoints/*.py`を基準に、起動スクリプトから設計書・図をたどれるようにしています。「設計書なし」は詳細設計書が未整備、「一部未反映」は記載範囲が機能全体を覆っていない状態です。

| 機能 | 起動スクリプト | 設計書 | 図 | 状態 |
|---|---|---|---|---|
| バックテストv2複数日検証 | `backtest_v2_multi_day_check.py` | [06 ヒストリカル再生](./detail_design/06-trading-usecase-historical-replay.md) | 4章 | 一部未反映 |
| バックテストv2単日検証 | `backtest_v2_single_day_check.py` | [06 ヒストリカル再生](./detail_design/06-trading-usecase-historical-replay.md) | 4章 | 一部未反映 |
| 板売買代金診断 | `diagnose_board_turnover.py` | [14 診断・保守](./detail_design/14-diagnostics-and-maintenance-design.md) | 4章 | 現行 |
| 分足売買代金欠損診断 | `diagnose_missing_turnover_by_minute_bars.py` | [14 診断・保守](./detail_design/14-diagnostics-and-maintenance-design.md) | 4章 | 現行 |
| ADX分析 | `run_adx_analysis.py` | [10 市場分析](./detail_design/10-market-analysis-design.md) | 4章 | 現行 |
| ATR比率分析 | `run_atr_ratio_analysis.py` | [10 市場分析](./detail_design/10-market-analysis-design.md) | 4章 | 現行 |
| バックテスト | `run_backtest.py` | [05 バックテスト](./detail_design/05-backtest-design.md) | 4章 | 現行 |
| 日次分析 | `run_daily_analysis.py` | [08 日次分析](./detail_design/08-daily-analysis-design.md)、[03b 価格帯別トレンド答え合わせ](./detail_design/03b-price-band-trend-check.md) | 4章 | 現行 |
| 日次日記 | `run_daily_diary.py` | [11 日次日記](./detail_design/11-daily-diary-design.md) | 4章 | 現行 |
| J-Quants V2取得クライアント | 後続取得処理から利用 | [15 J-Quants V2取得クライアント](./detail_design/15-jquants-v2-client.md) | 4章 | 一部未反映 |
| J-Quants raw・取得台帳 | 後続取得処理から利用 | [16 J-Quants raw・取得台帳・確定](./detail_design/16-jquants-raw-ledger.md) | 4章 | 一部未反映 |
| J-Quants 指定日の全銘柄取得・canonical | `ingest_jquants_single_day.py` | [17 指定日取得とcanonical](./detail_design/17-jquants-single-day-ingestion.md) | 4章 | 一部未反映 |
| J-Quants canonical schema運用・利用側の入力契約 | （方針と静的検査のみ） | [19 利用側の契約](./detail_design/19-jquants-consumer-contract.md) | なし | 一部未反映 |
| 日次タスク確認 | `run_daily_task_check.py` | [14 診断・保守](./detail_design/14-diagnostics-and-maintenance-design.md) | 4章 | 現行 |
| 日次タスク計画 | `run_daily_task_plan.py` | [14 診断・保守](./detail_design/14-diagnostics-and-maintenance-design.md) | 4章 | 現行 |
| フィルタリング | `run_filtering.py` | [02 フィルタリング](./detail_design/02-filtering-design.md) | 4章 | 現行 |
| フィルタリング上書き | `run_filtering_override.py` | [02 フィルタリング](./detail_design/02-filtering-design.md) 5章 | 4章（共通処理） | 現行 |
| 市場レジーム分析 | `run_market_regime.py` | [10 市場分析](./detail_design/10-market-analysis-design.md) | 4章 | 現行 |
| 市場ボラティリティ分析 | `run_market_volatility_analysis.py` | [10 市場分析](./detail_design/10-market-analysis-design.md) | 4章 | 現行 |
| 分足バックフィル | `run_minute_backfill.py` | [04 分足バックフィル](./detail_design/04-minute-bar-backfill-design.md) | 4章 | 一部未反映 |
| 月次分析 | `run_monthly_analysis.py` | [09 週次・月次分析](./detail_design/09-weekly-monthly-analysis-design.md) | 4章 | 現行 |
| スクリーニング | `run_screening.py` | [01 スクリーニング](./detail_design/01-screening-design.md) | 4章 | 現行 |
| 戦略レビュー | `run_strategy_review.py` | [12 戦略レビュー](./detail_design/12-strategy-review-design.md) | 4章 | 現行 |
| 取引ループ | `run_trading.py` | [03 取引ループ](./detail_design/03-trading-loop-design.md) | 4章 | 現行 |
| トレンドチェック | `run_trend_check.py` | [08 日次分析](./detail_design/08-daily-analysis-design.md) | 4章 | 現行 |
| VIX分析 | `run_vix_analysis.py` | [10 市場分析](./detail_design/10-market-analysis-design.md) | 4章 | 現行 |
| 週次分析 | `run_weekly_analysis.py` | [09 週次・月次分析](./detail_design/09-weekly-monthly-analysis-design.md) | 4章 | 現行 |
| 日足キャッシュ更新 | `update_daily_bar_cache.py` | [08 日次分析](./detail_design/08-daily-analysis-design.md) | 4章 | 現行 |
| Slack通知の出し分け・開始通知の傾向文 | 複数entrypoints | [13 通知](./detail_design/13-notification-design.md) | 4・5章 | 現行 |
| LLM連携 | 日次・週次・月次分析、バックテスト、日記、戦略レビュー、通知例外分析 | [07 LLM連携](./detail_design/07-llm-integration-design.md) | 4章 | 現行 |

## 運用ルール

- 新機能を追加したら、この対応表とルートの[README.md](../README.md)のドキュメントリンクも更新してください。
- 詳細設計書は[共通ひな形](./detail_design/_template.md)から作成し、章番号と章名を揃えてください。
- 全体構成・データフロー・実行スケジュール・Slack通知は[`architecture/`](./architecture/)の各資料を更新してください。
