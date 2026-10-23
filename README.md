# trade-project

三菱UFJ eスマート証券の **kabuステーションAPI** と Yahoo Finance を利用した、株式の自動スクリーニング・フィルタリング・取引システムです。

## 概要

本システムは、次の3段階で取引対象を決定します。

1. **スクリーニング**: 上場銘柄マスタとYahoo日足から候補を評価し、価格上限を優先して抽出
2. **フィルタリング**: 出来高急騰率を計算し、候補を上位銘柄に絞り込み
3. **取引実行**: 移動平均を基準に売買シグナルを生成し、注文を送信

ビジネスロジックを `domain` 層に集約し、API・永続化・通知などの外部依存を分離しています。

## 主な機能

### スクリーニング

- 上場銘柄マスタとYahoo日足から売買代金・値上がり率を計算
- 価格上限を先行適用し、規制・市場APIの結果を日付単位で記録・再利用
- `SCREENING_ALTERNATE_PRICE_CAPS`による価格帯別検証結果を別保存
- 規制銘柄と対象外市場の銘柄を除外
- 前日大引け後から翌朝のランキングデータクリア前に実行

### フィルタリング

- 前日のスクリーニング結果を読み込み
- kabuステーションから当日の出来高を取得
- Yahoo Finance から過去20営業日の平均出来高を取得
- 出来高急騰率の上位銘柄を選択
- 取引開始前に、現在の買付可能額と単元株価格で購入可能性を判定
- 候補順位を維持し、残予算を次の候補へ回して最大3ポジションへ配分

### 取引

- 確定日足からSMAバンド・RSI・ATRを計算し、MarketRegimeに応じて新規買いを制御
- ATRによる数量調整・損切り・トレーリング利確と、15:20の持ち越し防止決済を行う
- 資金、保有枠、重複注文、注文間隔、注文数、日次損失などを確認
- 取引終了後に日次レポートを保存し、Slackへ通知

## アーキテクチャ

```text
entrypoints       起動処理と依存性の組み立て
       |
application       ユースケースの実行制御
       |
domain            モデルと外部依存のないビジネスルール
       |
infrastructure    kabuステーション、Yahoo Finance、JSON、Slack
```

| 層 | 主な責務 |
| --- | --- |
| `domain` | ドメインモデル、列挙型、売買ルール |
| `application` | スクリーニング・フィルタリング・取引のユースケース |
| `infrastructure` | 外部API、マーケットデータ、永続化、通知 |
| `entrypoints` | 各処理の起動と依存性注入 |

## プロジェクト構成

```text
src/
├── api/              infrastructure専用HTTP共通処理
├── application/       ユースケース
├── config/            環境設定
├── domain/            モデル、Enum、ビジネスルール
├── entrypoints/       スクリーニング等の起動処理
├── infrastructure/
│   ├── kabu/           kabuステーションAPI連携
│   ├── market_data/    Yahoo Finance連携
│   ├── notification/   Slack通知
│   └── persistence/    JSON永続化
├── screening/          スクリーニング実行ラッパー
├── filter_dynamic/     フィルタリング実行ラッパー
├── trading/            TradingBot
└── executor/           取引実行ラッパー

docs/                   ADR、詳細設計、フロー、レビュー、改修タスク
scripts/                analysis/、common/、ops/、tasks/ に用途別分類
references/             kabuステーションAPI仕様書
tests/                  pytestテスト
```

## データ構成

`data/` は、処理の入力データ、実行結果、運用時の状態を保存する領域です。生成データはGitで無視し、各フォルダ直下の `用途_*.md` のみ構成説明として追跡します。必要なデータは別途バックアップしてください。

公開用の日次サマリーを別のGitHub Pagesリポジトリへ出す手順、公開許可項目、専用トークンの最小権限は[日次公開レポート](docs/architecture/public-report-publication.md)を参照してください。取引日報や口座・注文データそのものは公開しません。

```text
data/
├── analysis/             指標・分析結果
├── backtest/
│   ├── comparisons/      比較実験
│   ├── fixtures/         固定入力データ
│   ├── latest/           最新結果
│   └── runs/             日時付き実行履歴
├── filtering/            日別フィルタリング結果
├── logs/
│   ├── application/      アプリケーションログ
│   ├── jobs/              バッチ標準エラー
│   └── tests/             pytestログ
├── minute_bars/          旧形式JSON分足
├── minute_bars_parquet/  Parquet分足
├── notes/
│   ├── diary/             生成日記
│   └── operations/        手動運用メモ
├── reports/
│   ├── daily/             日次レポート
│   ├── monthly/           月次レポート
│   ├── weekly/            週次レポート
│   └── diagnose_unsold/   保有銘柄の売却状況の診断結果
├── regulation/           規制銘柄履歴
├── screening/            日別スクリーニング候補
├── state/                判定イベントDB・分析状態
├── trading/              注文・口座・停止状態
└── universe/             上場銘柄マスタ
```

| パス | 保存内容 | 利用する処理・目的 |
| --- | --- | --- |
| `data/analysis/` | ADX、ATR比率、市場ボラティリティ、VIXなど | 分析結果と取引前の市場レジーム判定を確認します。 |
| `data/backtest/latest/`、`runs/`、`comparisons/`、`fixtures/` | 最新結果、日時付き履歴、比較結果、固定入力 | 売買ルール変更後の比較と週次・月次レポートの集計に使います。 |
| `data/filtering/` | 日付別の抽出銘柄と出来高急騰率など | 当日の取引対象を決定し、時系列バックテストでその日の対象銘柄を再現するために使います。 |
| `data/logs/application/`、`tests/`、`jobs/` | アプリ、pytest、バッチ標準エラーのログ | 処理失敗の原因調査と定期タスクの実行確認に使います。 |
| `data/minute_bars/` | 日付・銘柄ごとのJSON形式の分足データ | 既存のJSON分足データを保管・参照する互換用フォルダです。新規保存はParquet形式を使います。 |
| `data/minute_bars_parquet/` | 銘柄・日付単位のParquet形式の分足データ | 分足バックテストの価格系列として読み込み、分足バックフィルの出力先として使います。 |
| `data/notes/operations/`、`diary/` | 手動の運用メモ、生成日記 | 日々の作業・検証記録と日記作成に使います。 |
| `data/regulation/` | `historical_regulations.csv` | 過去日スクリーニング時に、その日時点で除外すべき規制銘柄を判定するために使います。 |
| `data/reports/daily/`、`weekly/`、`monthly/` | 取引日報、週次・月次の集計結果 | LINE通知の記録を残し、定期分析と日記作成の集計元として使います。 |
| `data/reports/diagnose_unsold/` | 保有銘柄の売却状況の診断結果 | 売却されない原因を調査したMarkdown/JSONレポートを保存します。 |
| `data/screening/` | 日付別のランキング候補 | 翌営業日のフィルタリングの入力と、過去条件を用いた検証の候補集合として使います。 |
| `data/state/` | 判定イベントSQLite、LLMエラー状態 | 取引・バックテストの判定記録と分析状態を保持します。 |
| `data/trading/` | 注文履歴、ペーパートレード口座状態、非常停止フラグ | 重複注文を防ぎ、口座状態と安全停止状態を再起動後も保持します。 |
| `data/universe/` | `listed_securities.csv` | 過去日時点で上場していた銘柄だけを対象に、日足ベースのランキングを再計算するために使います。 |

`screening/`、`filtering/`、`regulation/`、`universe/`、分足データは、再現可能な検証に必要なら別途バックアップまたは共有してください。`trading/`、`state/`、`reports/`、`logs/`、日記・メモは個別の運用記録なので、公開リポジトリには含めない運用を推奨します。

## セットアップ

### 前提条件

- Python 3.10以上
- 三菱UFJ eスマート証券の口座
- kabuステーションのインストールと起動
- Yahoo Financeへ接続できる環境
- LINE通知を利用する場合はMessaging APIの設定

### インストール

```
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 環境変数

`.env` またはシステム環境変数に設定してください。パスワードなどの秘密情報はコミットしないでください。
全設定項目は[docs/reference/config-reference.md](docs/reference/config-reference.md)を参照してください。
J-Quants接続確認スクリプトでは、リポジトリ直下の`.env`に`JQUANTS_API_KEY`を設定すると自動で読み込みます（未設定の場合は実行時に入力できます）。

#### 運用モード

次の3つの組み合わせを使い分けます。

| モード | `IS_DEMO` | `TRADING_MODE` | `ENABLE_LIVE_ORDERING` | API接続 | 注文 |
| --- | --- | --- | --- | --- | --- |
| デモモード | `true` | `paper` | `false` | デモ | 仮想注文 |
| ペーパートレード | `false` | `paper` | `false` | 本番 | 仮想注文 |
| 本番モード | `false` | `live` | `true` | 本番 | 実注文 |

各設定の役割は次のとおりです。

- `IS_DEMO`: kabuステーションAPIの接続先と認証情報を選択します。
- `TRADING_MODE`: 取引監視の注文方法を選択します。`paper`なら仮想注文、`live`なら実注文です。
- `ENABLE_LIVE_ORDERING`: 実注文を許可する最終スイッチです。`TRADING_MODE=live`の場合に`true`が必要です。

デモモードは、現在の実装ではデモAPIを使った仮想注文です。デモAPIへ実注文を送る構成にはしていません。

```env
IS_DEMO=true
API_PASSWORD_DEV=<デモ環境のAPIパスワード>
API_PASSWORD_PRD=<本番環境のAPIパスワード>
API_PORT_DEV=18081
API_PORT_PRD=18080
TRADING_MODE=paper
ENABLE_LIVE_ORDERING=false

SLACK_WEBHOOK_CRITICAL=<criticalチャンネルのWebhook>
SLACK_WEBHOOK_DAILY=<dailyチャンネルのWebhook>
SLACK_WEBHOOK_ANALYSIS=<analysisチャンネルのWebhook>

# 日次LLM分析（任意。利用時はAPIキーを秘密情報として管理する）
LLM_DAILY_ANALYSIS_ENABLED=false
OPENAI_API_KEY=<LLM APIキー>
LLM_MODEL=gpt-4o-mini
LLM_API_URL=https://api.openai.com/v1/chat/completions

MAX_ORDER_AMOUNT_PER_TRADE=30000
MAX_ORDER_COUNT_PER_DAY=10
DAILY_LOSS_LIMIT_RATIO=0.02
OPERATING_CAPITAL=100000
API_SOFT_LIMIT=1000000

# 持ち越し防止の全保有成行売却時刻（日本標準時）
MARKET_LIQUIDATION_HOUR=15
MARKET_LIQUIDATION_MINUTE=20

# true の場合のみ持ち越しを許可する（既定は false）
ALLOW_OVERNIGHT_HOLDING=false
```

本番APIを使うペーパートレードでは、`IS_DEMO=false`、`TRADING_MODE=paper`、`ENABLE_LIVE_ORDERING=false`にします。
実注文を行う場合だけ、`TRADING_MODE=live`と`ENABLE_LIVE_ORDERING=true`に変更してください。

## 実行方法

### 1. スクリーニング

前日15:35頃に実行し、翌営業日の候補銘柄を保存します。

```
python -m src.entrypoints.run_screening
```

#### Windowsでの計画実行

kabuステーションを起動・ログインしたWindowsユーザーで、平日15:35にスクリーニングを実行するタスクを登録します。初回のみ、PowerShellから次を実行してください。

```
.\scripts\tasks\register_screening_task.ps1
```

登録内容の確認と手動起動は次のとおりです。

```
Get-ScheduledTask -TaskName trade-pjoject-screening
Start-ScheduledTask -TaskName trade-pjoject-screening
```

タスクはログオン中にのみ実行されます。実行時刻の変更と削除は次のコマンドで行えます。

```
.\scripts\tasks\register_screening_task.ps1 -At '15:40'
.\scripts\tasks\register_screening_task.ps1 -Remove
```

実行時にはkabuステーションが起動済みで、`.env` に本番用の `IS_DEMO=false`、`API_PASSWORD_PRD`、`API_PORT_PRD` が設定されている必要があります。休場日またはランキング未取得時は、スクリーニング処理がエラー終了し、推測値で候補を作成しません。

### 2. フィルタリング

営業開始後の9:30頃に実行し、出来高急騰率の高い銘柄へ絞り込みます。

```
python -m src.entrypoints.run_filtering
```

保存済みのスクリーニング結果に対して過去日を再計算する場合は、対象日を指定します。過去日モードではkabuステーションに接続せず、Yahoo Financeの日足から対象日の売買代金と直前20営業日の平均を計算します。

```
python -m src.entrypoints.run_filtering --date 2026-09-08
.\scripts\tasks\run_filtering.ps1 -Date '2026-09-08'
```

スクリーニングのランキングAPIには過去日を指定して取得する機能がないため、過去のランキングを後から完全に再現することはできません。スクリーニング結果は実行日に `data/screening/YYYY-MM-DD.json` として保存されるため、今後の条件変更に備えてこのファイルを履歴として保管してください。過去のスクリーニング条件自体を変更して再計算するには、ランキング取得元の履歴データを別途保存する必要があります。

### 3. 過去日のスクリーニング

日付付きの上場銘柄マスタを `data/universe/listed_securities.csv` に配置すると、kabu APIのランキング結果ではなく、その日時点で上場していた銘柄を母集団にして日足ランキングを計算できます。

```csv
symbol,exchange_division,listed_from,listed_to
7203,TP,2020-01-01,
8306,TS,2020-01-01,2026-09-07
```

`listed_to` は上場継続中なら空欄にします。JPX等から取得した日付付きマスタをこの形式に変換してから、次のコマンドを実行します。

```
python -m src.entrypoints.run_screening --date 2026-09-08
```

このモードでは、売買代金と値上がり率をYahoo Financeの日足から計算します。規制情報は `data/regulation/historical_regulations.csv` を参照します。

規制マスタは、規制期間ごとに次の形式で記録します。規制がない期間は行を作らず、`restricted_to` が空欄の場合は現在も継続中として扱います。

```csv
symbol,primary_exchange,restricted_from,restricted_to,reason
7203,1,2026-09-01,2026-09-07,売買規制
7203,1,2026-09-10,,監視措置
```

#### Windowsでの計画実行

kabuステーションを起動・ログインしたWindowsユーザーで、平日9:30にフィルタリングを実行するタスクを登録します。初回のみ、PowerShellから次を実行してください。

```
.\scripts\tasks\register_filtering_task.ps1
```

登録内容の確認と手動起動は次のとおりです。

```
Get-ScheduledTask -TaskName trade-pjoject-filtering
Start-ScheduledTask -TaskName trade-pjoject-filtering
```

実行時刻の変更と削除は次のコマンドで行えます。

```
.\scripts\tasks\register_filtering_task.ps1 -At '09:35'
.\scripts\tasks\register_filtering_task.ps1 -Remove
```

タスクはログオン中にのみ実行されます。前営業日のスクリーニング結果がない場合は、フィルタリング結果を0件として保存・通知します。

### 3. 取引

営業開始前に起動し、15:30まで価格を監視します。

```
python -m src.entrypoints.run_trading
```

Windowsの計画実行では、スクリーニング・フィルタリングと同じ本番API設定を使用し、取引注文だけをペーパー約定に固定できます。初回のみ、次を実行してください。

```
.\scripts\tasks\register_trading_task.ps1
```

このタスクは `TRADING_MODE=paper` と `ENABLE_LIVE_ORDERING=false` をプロセス内で設定するため、`.env` の本番API設定を変更せずにペーパートレードを実行します。kabuステーションは起動・ログイン済みにしてください。

`register_trading_task.ps1`で登録した取引タスクは常にペーパー固定です。本番モードで実注文を行う場合は、設定を確認したうえで `python -m src.entrypoints.run_trading` を直接起動してください。

起動時に売買設定値（資金・RSI・決済時刻など）を検証し、範囲外なら全件をログとSlackへ出して起動を止めます。`.env`変更後は `.\.venv\Scripts\python.exe scripts/check_startup_config.py` で事前に確認できます（違反があれば終了コード1。秘密値は表示しません）。

取引終了時のレポートはLINEへ通知されるほか、日付別に `data/reports/daily/YYYY-MM-DD.json` へ保存されます。JSONには注文数、注文内容、保有銘柄の評価損益、キルスイッチ状態、LINE本文、LLM日次評価（有効時）が含まれます。注文履歴は `data/trading/order_history.json`、ペーパー口座状態は `data/trading/paper_account_state.json` に保存されます。

#### 緊急停止

取引プロセスの次ループで停止フラグを検知し、即時LINE通知を送信したうえで保有ポジションを成行決済します。停止を実行するには次を実行してください。

```
.\scripts\ops\emergency_stop.ps1
```

停止解除後に再起動する場合は、次を実行します。解除前に注文・口座状態を確認してください。

```
.\scripts\ops\clear_emergency_stop.ps1
```

キルスイッチ（損失上限・発注回数上限・API上限取得失敗）の発動時も、日次レポートを待たず即時LINE通知されます。自動キルスイッチは新規発注を停止し、手動緊急停止は保有ポジションも決済します。

### 4. バックテスト

日付ごとのフィルタリング結果を時系列に再生し、その日に選ばれた銘柄だけを対象にバックテストします。過去の銘柄を未来の日付へ持ち越さないため、実運用に近い評価になります。価格データはYahoo Financeから取得し、直近730日（約2年）分のフィルタリング結果を対象にします。通常の週次確認は2年、売買ルールや設定を変更したときは3〜5年を再検証の目安にします。

```
.\scripts\tasks\run_backtest.ps1
```

結果は最新結果として `data/backtest/latest/latest_timeseries.json` に保存され、日時付き履歴は `data/backtest/runs/latest_timeseries_YYYYMMDD_HHMMSS_ffffff.json` に保存されます。LINE設定がある場合は、対象期間・総損益・勝率・取引数・最大ドローダウン・最終保有数のサマリーと、LLMによる参考評価（有効時）も通知します。LLM評価は投資判断やロジック変更の指示ではなく、統計の解釈・不確実性・追加確認事項を扱います。詳細な取引履歴はJSONで確認できます。毎週土曜08:00に自動実行するタスクは、初回のみ次で登録します。

バックテストは既定で片道手数料0.055%、成行スリッページ5bps、1バーの執行遅延を反映します。成行は遅延後の観測価格に対し、買いは上振れ・売りは下振れで約定させます。指値はkabuステーションAPIの `FrontOrderType=20` に対応する想定として、シグナル時価格で約定し成行スリッページは加えません。分足がない日足再生では遅延は次の終値、分足再生では次の分足価格を使います。手数料プランと実運用の約定履歴に合わせる場合は、`BACKTEST_FEE_RATE`、`BACKTEST_MARKET_SLIPPAGE_BPS`、`BACKTEST_EXECUTION_DELAY_BARS`、`BACKTEST_ORDER_TYPE` を環境変数で上書きするか、`--fee`、`--market-slippage-bps`、`--execution-delay-bars`、`--order-type` を指定します。

```
.\scripts\tasks\register_backtest_task.ps1
```

登録内容の確認、手動起動、削除は次のとおりです。

```
Get-ScheduledTask -TaskName trade-pjoject-backtest
Start-ScheduledTask -TaskName trade-pjoject-backtest
.\scripts\tasks\register_backtest_task.ps1 -Remove
```

### 5. 月次総合分析

対象月の日次ペーパートレードレポートと、月内に実行した週次バックテスト結果を集約します。LLM設定が有効な場合は、「観測事実」「差分」「仮説」「次に確認するデータ」の観点で参考評価を作成し、`data/reports/monthly/YYYY-MM.json` に保存してLINEへ通知します。LLMの評価は投資判断や自動的なロジック変更には使用しません。

初回のみ、毎日17:00に起動し、Python側のガードで月末だけ実行するタスクを登録します。Windows PowerShellの標準タスク登録では月末指定に制約があるため、この方式を採用しています。

```
.\scripts\tasks\register_monthly_analysis_task.ps1
```

手動で前月分を実行する場合は次のコマンドを使います。月末以外に当月分を確認する場合は `--force` を追加します。

```
python -m src.entrypoints.run_monthly_analysis --month 2026-08
python -m src.entrypoints.run_monthly_analysis --month 2026-09 --force
```

### 6. 週次総合分析

土曜の分足バックフィルとバックテスト後に、直近の月曜から金曜までの日次ペーパートレードとバックテストを集計します。結果は `data/reports/weekly/YYYY-MM-DD_YYYY-MM-DD.json` に保存し、LLM評価（有効時）とともにLINEへ通知します。

```
.\scripts\tasks\register_weekly_analysis_task.ps1
python -m src.entrypoints.run_weekly_analysis --week-start 2026-09-07 --force
```

### 7. 日次タスク予定・実行結果通知

毎朝07:00に当日の予定タスクをSlackのdailyチャンネルへ通知し、毎日18:00にアプリケーションログと取引日報から実行結果を確認します。異常終了・未実行・終了記録や日報の欠落があればcriticalチャンネルにも通知します。土曜タスクは祝日にかかわらず週次予定として表示します。

初回のみ、次のタスクを登録してください。

```
.\scripts\tasks\register_daily_task_plan_task.ps1
.\scripts\tasks\register_daily_task_check_task.ps1
```

通知対象日の曜日・休場日・月末最終営業日の判定は `src/config/task_schedule.py` で一元管理します。

従来の固定銘柄による検証を行う場合は、`run_backtest.py` に `--symbols` と `--history` を指定します。

## 取引フロー

```text
前日15:35頃
  スクリーニング -> 候補銘柄を保存
       |
当日9:30頃
  フィルタリング -> 出来高急騰銘柄を保存
       |
当日9:35-15:30
  価格監視 -> シグナル判定 -> 安全性確認 -> 注文
       |
15:30
  取引終了 -> 最終状態を通知
                      |
平日16:30
       直近90日の日次フィルタリング結果 -> 時系列バックテスト
```

## 設計書

- [ドキュメント案内・機能と設計書の対応表](docs/README.md)
- [コーディング規約](docs/architecture/coding-guidelines.md)
- [全体フロー](docs/architecture/flow.md)
- [実行スケジュール](docs/architecture/schedule.md)
- [Slack通知](docs/architecture/notifications.md)
- [スクリーニング詳細設計](docs/detail_design/01-screening-design.md)
- [フィルタリング詳細設計](docs/detail_design/02-filtering-design.md)
- [取引ループ詳細設計](docs/detail_design/03-trading-loop-design.md)
- [価格帯別トレンド答え合わせ詳細設計](docs/detail_design/03b-price-band-trend-check.md)
- [分足バックフィル詳細設計](docs/detail_design/04-minute-bar-backfill-design.md)
- [バックテスト詳細設計](docs/detail_design/05-backtest-design.md)
- [ヒストリカル再生詳細設計](docs/detail_design/06-trading-usecase-historical-replay.md)
- [LLM連携詳細設計](docs/detail_design/07-llm-integration-design.md)
- [日次分析・日足更新詳細設計](docs/detail_design/08-daily-analysis-design.md)
- [週次・月次分析詳細設計](docs/detail_design/09-weekly-monthly-analysis-design.md)
- [市場分析詳細設計](docs/detail_design/10-market-analysis-design.md)
- [日次日記詳細設計](docs/detail_design/11-daily-diary-design.md)
- [戦略レビュー詳細設計](docs/detail_design/12-strategy-review-design.md)
- [通知詳細設計](docs/detail_design/13-notification-design.md)
- [診断・保守詳細設計](docs/detail_design/14-diagnostics-and-maintenance-design.md)
- [J-Quants 指定日の全銘柄取得とcanonical Parquet](docs/detail_design/17-jquants-single-day-ingestion.md)
- [kabuステーションAPI仕様書](references/kabu_STATION_API.yaml)

## テスト

変更中は、変更した機能に対応するテストファイルを実行します。たとえば売買処理の変更なら：

```
.\.venv\Scripts\python.exe -m pytest -q tests/test_trading_bot.py
```

変更をまとめる前に全件を実行し、遅いテストも確認します：

```
.\.venv\Scripts\python.exe -m pytest -q --durations=20
```

特定のテストだけを実行する場合：

```
.\.venv\Scripts\python.exe -m pytest -q tests/test_design_alignment.py -k filtering
```

対象ファイルが分からない場合は、まず `--durations=20` で時間のかかるテストを確認します。全件実行は変更のまとまりごとやマージ前に行い、日々の小さな修正では関連テストを優先します。
テスト中は任意の異常検知LLM呼び出しを無効化し、LLM専用テストだけが明示的に有効化します。

## 注意事項

- kabuステーションが起動していない場合、API通信は失敗します。
- 本番注文を有効化する前に、必ずデモ環境で動作を確認してください。
- 投資判断と注文結果は利用者自身の責任で管理してください。
