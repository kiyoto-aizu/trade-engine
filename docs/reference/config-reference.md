# 環境変数リファレンス

`src/config/config.py`をimportせずAST解析して生成。既定値はソース上の環境変数フォールバック式です。
秘密情報に該当する名前は値を表示しません。説明は設定宣言直前のコメントのみを転記しています。

## ATR

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `ATR_CAUTION_LOT_RATIO` | "0.5" | 任意 |  |
| `ATR_CAUTION_RATIO` | "1.5" | 任意 |  |
| `ATR_DANGER_ACTION` | "skip" | 任意 |  |
| `ATR_DANGER_RATIO` | "2.0" | 任意 |  |
| `ATR_PERIOD` | "14" | 任意 | ATRベースの銘柄別ボラティリティ調整 |
| `ATR_PROFIT_LOCK_CAUTION_MULTIPLIER` | "2.0" | 任意 |  |
| `ATR_PROFIT_LOCK_DANGER_MULTIPLIER` | "1.0" | 任意 |  |
| `ATR_PROFIT_LOCK_NORMAL_MULTIPLIER` | "2.5" | 任意 | ADR-0006: 含み益が一定以上(ATR基準)乗った場合のみ使う、利確専用のATRトレーリング倍率 |
| `ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE` | "0.5" | 任意 |  |
| `ATR_STOP_CAUTION_MULTIPLIER` | "1.0" | 任意 |  |
| `ATR_STOP_DANGER_MULTIPLIER` | "0.7" | 任意 |  |
| `ATR_STOP_NORMAL_MULTIPLIER` | "1.5" | 任意 |  |

## レジーム

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `MARKET_REGIME_ADX_TREND_THRESHOLD` | "27.0" | 任意 |  |
| `MARKET_REGIME_DATA_RANGE` | "3mo" | 任意 |  |
| `MARKET_REGIME_NIKKEI_CHANGE_UPGRADE` | "2.0" | 任意 | 市場全体の荒れ具合（MarketRegime） |
| `MARKET_REGIME_REALIZED_VOL_CAUTION` | "17.0" | 任意 | 市場全体の荒れ具合（MarketRegime） |
| `MARKET_REGIME_REALIZED_VOL_DANGER` | "29.0" | 任意 | 市場全体の荒れ具合（MarketRegime） |
| `MARKET_REGIME_REALIZED_VOL_WINDOW` | "20" | 任意 |  |
| `MARKET_REGIME_VIX_CAUTION` | "17.0" | 任意 | 市場全体の荒れ具合（MarketRegime） |
| `MARKET_REGIME_VIX_DANGER` | "27.0" | 任意 | 市場全体の荒れ具合（MarketRegime） |

## スクリーニング

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `SCREENING_ALTERNATE_PRICE_CAPS` | "450,900" | 任意 |  |
| `SCREENING_ANOMALY_MIN_SYMBOLS` | "5" | 任意 |  |
| `SCREENING_API_CHECK_DIRECTORY` | str(_repo_root / 'data' / 'regulation' / 'screening_api_checks') | 任意 |  |
| `SCREENING_EXCHANGE_DIVISIONS` | "TP,TS,TG" | 任意 | スクリーニングのランキング取得対象とする市場区分（/rankingのExchangeDivision） 全市場(ALL)は1回の呼び出しにつき上位50件しか返らず、値がさ株に偏りやすいため 市場区分ごとに個別取得して母集団を拡大する（福証・札証は取引対象外のため含めない） |
| `SCREENING_PRICE_BAND_RESULT_ROOT` | str(_repo_root / 'data' / 'screening_price_bands') | 任意 |  |
| `SCREENING_PRICE_MARGIN` | "0.9" | 任意 | スクリーニング時の株価上限に掛ける安全マージン |
| `SCREENING_RESULT_DIRECTORY` | str(_repo_root / 'data' / 'screening') | 任意 |  |

## フィルタ

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `FILTERING_ANOMALY_MIN_SYMBOLS` | "3" | 任意 |  |
| `FILTERING_DIAGNOSTICS_DIRECTORY` | str(_repo_root / 'data' / 'filtering_diagnostics') | 任意 |  |
| `FILTERING_MORNING_ALTERNATE_BANDS_ENABLED` | "false" | 任意 | 朝(09:30)の追加価格帯(450円・900円)の板取得。既定は停止し、昼のYahoo分足推定へ回す。trueで朝の実施に戻せる |
| `FILTERING_PRICE_BAND_DEADLINE_TIME` | "09:34" | 任意 |  |
| `FILTERING_PRICE_BAND_RESULT_ROOT` | str(_repo_root / 'data' / 'filtering_price_bands') | 任意 |  |
| `FILTERING_RESULT_DIRECTORY` | str(_repo_root / 'data' / 'filtering') | 任意 |  |
| `FILTER_BOARD_429_MAX_RETRIES` | "2" | 任意 | 429応答時に再試行する最大回数 |
| `FILTER_BOARD_429_RETRY_WAIT_SECONDS` | "1" | 任意 | board APIが429を返した際の待機時間と追加再試行回数 |
| `FILTER_BOARD_MAX_CONCURRENCY` | "3" | 任意 | 全価格帯をまとめて板取得する際の最大同時リクエスト数 |
| `FILTER_BOARD_RETRY_ENABLED` | "true" | 任意 | 全価格帯で板の売買代金・売買高が取れなかった銘柄を、結果保存前に再取得する |
| `FILTER_BOARD_RETRY_MARGIN_SECONDS` | "30" | 任意 | リトライの打ち切り時刻 = FILTERING_PRICE_BAND_DEADLINE_TIME - この秒数 |
| `FILTER_BOARD_RETRY_MAX_ROUNDS` | "2" | 任意 |  |
| `FILTER_BOARD_RETRY_WAIT_SECONDS` | "10" | 任意 |  |
| `FILTER_DECISION_OBSERVATION_DAYS` | "5" | 任意 |  |
| `MIDDAY_FILTERING_DEADLINE_TIME` | "12:25" | 任意 |  |
| `MIDDAY_FILTERING_FETCH_RETRIES` | "1" | 任意 |  |
| `MIDDAY_FILTERING_FETCH_TIMEOUT_SECONDS` | "30" | 任意 | Yahoo分足1銘柄あたりのタイムアウト秒と、失敗時の追加リトライ回数(0で再試行なし)・リトライ前の待機秒 |
| `MIDDAY_FILTERING_RESULT_ROOT` | str(_repo_root / 'data' / 'filtering_midday') | 任意 | 昼の結果・診断・270円比較の保存先。朝の板の結果(FILTERING_*)とは別に保存する |
| `MIDDAY_FILTERING_RETRY_WAIT_SECONDS` | "2" | 任意 |  |
| `MIDDAY_FILTERING_SKIP_DATES` | "" | 任意 | 午前のみの取引日など、昼の処理を実行しない日付(YYYY-MM-DD, カンマ区切り) |
| `MIDDAY_FILTERING_START_TIME` | "12:00" | 任意 | 昼フィルタ(追加価格帯をYahoo 1分足の09:00〜09:30売買代金から推定する処理) |
| `MIDDAY_FILTERING_VERIFY_RESULT_ROOT` | str(_repo_root / 'data' / 'filtering_midday_verify') | 任意 | 昼フィルタの検証用実行(--verify)の保存先。本番の保存先とは分ける |

## paper

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `PAPER_FEE_RATE` | "0.00055" | 任意 | ペーパートレードでは発注後の次回価格を観測できないため、現在価格に不利方向の成行スリッページを適用する。 |
| `PAPER_MARKET_SLIPPAGE_BPS` | "5" | 任意 |  |

## Slack

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `SLACK_WEBHOOK_ANALYSIS` | (非表示) | 必須 |  |
| `SLACK_WEBHOOK_CRITICAL` | (非表示) | 必須 | チャンネル別Incoming Webhook URL（緊急度別に3分割） critical: 約定・キルスイッチ・例外 / daily: 定型の日次ログ / analysis: 週次・月次分析等 |
| `SLACK_WEBHOOK_DAILY` | (非表示) | 必須 |  |

## LLM

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `LLM_ANOMALY_ANALYSIS_ENABLED` | "false" | 任意 | スクリーニング/フィルタリング異常検知用LLM設定（明示的に有効化した場合のみ利用。閾値を下回った時のみLLMを呼び出す） |
| `LLM_API_KEY` | (非表示) | 任意 |  |
| `LLM_API_URL` | "https://api.openai.com/v1/chat/completions" | 任意 |  |
| `LLM_DAILY_ANALYSIS_ENABLED` | "false" | 任意 | 日次LLM分析設定（明示的に有効化した場合のみ利用） |
| `LLM_DIARY_ENABLED` | "false" | 任意 |  |
| `LLM_ERROR_ANALYSIS_COOLDOWN_MINUTES` | "60" | 任意 |  |
| `LLM_ERROR_ANALYSIS_ENABLED` | "false" | 任意 | 例外原因分析用LLM設定（明示的に有効化した場合のみ利用。同一原因のエラーはキャッシュを再利用し、クールダウン間隔でのみ再分析） |
| `LLM_ERROR_ANALYSIS_MODEL` | "gpt-4o-mini" | 任意 |  |
| `LLM_ERROR_ANALYSIS_SKIP_EXCEPTION_TYPES` | "ConnectionError,Timeout,ConnectTimeout,ReadTimeout,JSONDecodeError" | 任意 | リトライで解決しうる想定内の例外はLLM分析の対象外とする（クラス名でMRO照合） |
| `LLM_MODEL` | "gpt-4o-mini" | 任意 |  |
| `LLM_TIMEOUT_SECONDS` | "120" | 任意 |  |

## ログ

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `LOG_BACKUP_COUNT` | str(5) | 任意 | 保持する日次ログの世代数 |
| `LOG_LEVEL` | "INFO" | 任意 | ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL) |

## その他

| 環境変数名 | 既定値 | 必須か | 説明（直前コメント） |
|---|---|---|---|
| `ALLOW_MISSING_ENV` | "" | 任意 |  |
| `ALLOW_OVERNIGHT_HOLDING` | "false" | 任意 |  |
| `API_PASSWORD_DEV` | (非表示) | 必須 | 環境に応じたAPIパスワード選択 |
| `API_PASSWORD_PRD` | (非表示) | 必須 | 環境に応じたAPIパスワード選択 |
| `API_PORT_DEV` | "18081" | 任意 | 環境に応じたAPIポート選択（デモ/本番） |
| `API_PORT_PRD` | "18080" | 任意 | 環境に応じたAPIポート選択（デモ/本番） |
| `API_REQUEST_INTERVAL_SECONDS` | "0.12" | 任意 | kabuステーションAPIの実行回数制限を超えないための最小呼出間隔（秒） |
| `API_SOFT_LIMIT` | "1000000" | 任意 | APIソフトリミット - リスク管理用の内部閾値 |
| `BACKTEST_EXECUTION_DELAY_BARS` | "1" | 任意 |  |
| `BACKTEST_FEE_RATE` | "0.00055" | 任意 | バックテストの約定コスト。各値は証券会社の料金プラン・運用実績に合わせて環境変数で調整する。 |
| `BACKTEST_MARKET_SLIPPAGE_BPS` | "5" | 任意 |  |
| `BACKTEST_ORDER_TYPE` | "market" | 任意 |  |
| `BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD` | "3" | 任意 | 対象銘柄全件の板取得が何回連続で失敗したら通知するか（取引ループの1周を1回と数える） |
| `DAILY_ANALYSIS_START_TIME` | "16:30" | 任意 | 日次分析(確定日足更新・答え合わせ・分析通知)の開始時刻 |
| `DAILY_LOSS_LIMIT_RATIO` | "0.02" | 任意 | 1日の損失限度額（運用資本の比率、例：0.02 = 2%） |
| `DAILY_MINUTE_BAR_INCLUDE_PRICE_BANDS` | "false" | 任意 | 日次分析での当日分足保存に、価格帯別(450円帯・900円帯)のフィルタ結果の銘柄も含めるか。 |
| `EMERGENCY_STOP_FILE` | str(_repo_root / 'data' / 'trading' / 'emergency_stop') | 任意 |  |
| `ENABLE_LIVE_ORDERING` | "false" | 任意 |  |
| `INDEX_MINUTE_BAR_PARQUET_DIR` | str(_repo_root / 'data' / 'index_minute_bars_parquet') | 任意 | 日経225など指数の分足Parquetの保存先。個別銘柄用とはDuckDB読み込み(symbol列がBIGINT)を壊さないため分離する。 |
| `IS_DEMO` | "true" | 任意 |  |
| `JQUANTS_429_WAIT_SECONDS` | "120" | 任意 |  |
| `JQUANTS_API_KEY` | (非表示) | 任意 | J-Quants API V2。接続先はAPIキーの誤送信を避けるため固定する。 |
| `JQUANTS_CANONICAL_DIRECTORY` | str(_repo_root / 'data' / 'jquants' / 'canonical') | 任意 |  |
| `JQUANTS_LEDGER_DATABASE_FILE` | str(_repo_root / 'data' / 'jquants' / 'ledger.sqlite3') | 任意 |  |
| `JQUANTS_EXECUTION_METRICS_DIRECTORY` | str(_repo_root / 'data' / 'jquants' / 'metrics') | 任意 |  |
| `JQUANTS_MAX_ATTEMPTS` | "4" | 任意 |  |
| `JQUANTS_MAX_429_PER_RUN` | "3" | 任意 | 1実行内の429累計がこの回数に達したら、残りの取得を全て打ち切る(正数)。 |
| `JQUANTS_RAW_DIRECTORY` | str(_repo_root / 'data' / 'jquants' / 'raw') | 任意 |  |
| `JQUANTS_REQUEST_INTERVAL_SECONDS` | "1.2" | 任意 |  |
| `JQUANTS_RETRY_BASE_SECONDS` | "1" | 任意 |  |
| `JQUANTS_RETRY_MAX_SECONDS` | "30" | 任意 |  |
| `JQUANTS_TIMEOUT_SECONDS` | "30" | 任意 |  |
| `KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS` | (非表示) | 任意 | トークン再取得後も401が続く（復旧失敗）場合に、次の再取得試行まであける間隔（秒）。 他プロセスとのトークン発行の奪い合いを防ぐための間隔 |
| `KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS` | (非表示) | 任意 | 401応答を受けた際にトークンを再取得する最小間隔（秒）。この間隔内は取得済みの トークンで再試行し、/tokenへの呼び出し自体は増やさない |
| `LIQUIDATION_POSITIONS_FETCH_RETRIES` | "3" | 任意 | 強制決済(緊急停止・EOD)時に保有株一覧の取得が失敗した場合の再試行回数・間隔(秒)。 失敗(None)と保有ゼロ([])を取り違えないための安全策 |
| `LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS` | "5" | 任意 |  |
| `MARKET_LIQUIDATION_HOUR` | "15" | 任意 | 持ち越しを防止するため、全保有を成行決済する時刻（日本標準時） |
| `MARKET_LIQUIDATION_MINUTE` | "20" | 任意 |  |
| `MAX_ORDER_AMOUNT_PER_TRADE` | "30000" | 任意 | 1回の取引あたりの最大注文金額（運用資金10万円の30%） |
| `MAX_ORDER_COUNT_PER_DAY` | "10" | 任意 | 1日あたりの最大注文数 |
| `MINUTE_BACKFILL_UNSELECTED_TIMEOUT_SECONDS` | "1800" | 任意 | 定期分足バックフィルで昼推定の選定外銘柄を取得する時間上限(秒)。 |
| `MINUTE_BAR_PARQUET_DIR` | str(_repo_root / 'data' / 'minute_bars_parquet') | 任意 | 分足Parquetの保存先。 |
| `MINUTE_BAR_PARQUET_VERIFY_DIR` | str(_repo_root / 'data' / 'minute_bars_parquet_verify') | 任意 |  |
| `OPENAI_API_KEY` | (非表示) | 任意 |  |
| `OPERATING_CAPITAL` | "100000" | 任意 | 取引に利用可能な運用資本 |
| `PRICE_GAIN_WEIGHT` | "1.0" | 任意 | ADR-0001: 出来高ランキングと値上がり率ランキングを統合する際、値上がり率側に 掛ける重み係数。1.0が従来通りの等重み。値上がり率ランキング上位の銘柄ほど 既に株価が伸びており、予算上限(株価上限)に近づきやすいという診断結果 (2026-09-19, analyze_filter_entry_alignment.py)を踏まえ、値上がり率側の 影響を弱めることで、予算内に収まる候補を増やす狙い。 【要調整】この数値は実データでの検証前の暫定値であり、1.0(現状維持)としている。 値上がり率と事後の予算超過率との相関を見た上で、適切な値に調整すること。 |
| `RSI_BUY_THRESHOLD` | "55" | 任意 |  |
| `RSI_ENTRY_THRESHOLD` | "RSI_BUY_THRESHOLD" (未設定時 "55") | 任意 |  |
| `RSI_ENTRY_THRESHOLD_CAUTION` | "60" | 任意 |  |
| `RSI_EXIT_THRESHOLD` | "RSI_SELL_THRESHOLD" (未設定時 "45") | 任意 |  |
| `RSI_MINIMUM_CLOSES` | "30" | 任意 |  |
| `RSI_PERIOD` | "14" | 任意 | 売買シグナルのトレンド確認（Wilder方式のRSI） |
| `RSI_SELL_THRESHOLD` | "45" | 任意 |  |
| `STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD` | "3" | 任意 | 注文履歴・Paper状態ファイルの保存が何回連続で失敗したら新規買いを止めるか(売り・決済は止めない) |
| `TARGET_POSITIONS` | "3" | 任意 | フィルタリング候補から目指す分散ポジション数 |
| `TRADING_MODE` | "paper" | 任意 |  |
| `TRADING_PROGRESS_REPORT_1_HOUR` | "11" | 任意 | 取引中間報告の送信時刻（日本標準時） |
| `TRADING_PROGRESS_REPORT_1_MINUTE` | "30" | 任意 |  |
| `TRADING_PROGRESS_REPORT_2_HOUR` | "14" | 任意 |  |
| `TRADING_PROGRESS_REPORT_2_MINUTE` | "0" | 任意 |  |

説明欄が空の項目: 71件。
