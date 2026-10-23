# config.py
# ================================================================================
# 取引ボットの設定モジュール
# APIクレデンシャル、取引パラメータ、環境固有の設定など、
# すべての設定を管理するモジュール
# ================================================================================


import os
import sys
from datetime import time
from pathlib import Path

from src.domain.enums import OrderSide  # noqa: F401  # config.OrderSide として再エクスポート（重複定義を避ける）
from src.domain.market_regime import MarketRegimeThresholds

# .envファイルから環境変数を読み込む（利用可能な場合）
try:
    from dotenv import load_dotenv
    _DOTENV_AVAILABLE = True
except ImportError:
    _DOTENV_AVAILABLE = False

# リポジトリのルートディレクトリを取得
# config.py -> config/ -> src/ -> リポジトリルート の順にたどる。
_repo_root = Path(__file__).resolve().parents[2]
_env_path = _repo_root / '.env'
if _DOTENV_AVAILABLE and _env_path.exists():
    load_dotenv(dotenv_path=str(_env_path))


# ================================================================================
# ランタイム検出関数
# ================================================================================

def _is_test_runtime() -> bool:
    """
    テストモードで実行されているかを判定する。
    pytestがアクティブまたはALLOW_MISSING_ENVが明示的に設定されている場合、Trueを返す。
    """
    return "pytest" in sys.modules or os.getenv("ALLOW_MISSING_ENV", "").strip().lower() in {"1", "true", "yes", "on"}


# ================================================================================
# 環境変数読み込み関数
# ================================================================================

def _load_required_env(name: str, *, allow_missing: bool | None = None) -> str:
    """
    必須環境変数を読み込む。
    
    Args:
        name: 読み込む環境変数名
        allow_missing: Trueの場合、見つからないときは空文字列を返す。Noneの場合はテストランタイムの動作を使用。
    
    Returns:
        環境変数の値、またはallow_missingがTrueの場合は空文字列
    
    Raises:
        ValueError: 変数が見つからずallow_missingがFalseの場合
    """
    value = os.getenv(name, "").strip()
    if value:
        return value

    if allow_missing is None:
        allow_missing = _is_test_runtime()
    if allow_missing:
        return ""

    raise ValueError(f"Missing required environment variable: {name}")

# ================================================================================
# 環境モード設定
# ================================================================================

_IS_DEMO_ENV = os.getenv("IS_DEMO", "true").strip().lower()
IS_DEMO = _IS_DEMO_ENV in ("1", "true", "yes")  # True: デモモード、False: 本番モード
ENABLE_LIVE_ORDERING = os.getenv("ENABLE_LIVE_ORDERING", "false").strip().lower() in ("1", "true", "yes")  # 実取引を許可
TRADING_MODE = os.getenv("TRADING_MODE", "paper").strip().lower()
if TRADING_MODE not in {"paper", "live"}:
    raise ValueError("TRADING_MODEはpaperまたはliveを指定してください。")
TRADING_MODE_LABEL = "ペーパートレード" if TRADING_MODE == "paper" else "本番取引"
_ALLOW_MISSING_ENV = _is_test_runtime()  # テスト中は環境変数がなくても許可


# ================================================================================
# 取引パラメータ
# ================================================================================

# メイン取引ループの実行間隔（秒）
LOOP_INTERVAL = 60

# 日本株の売買単位。買い数量はこの単位の整数倍で動的に決める。
ORDER_UNIT = 100

# 持ち越しを防止するため、全保有を成行決済する時刻（日本標準時）
MARKET_LIQUIDATION_HOUR = int(os.getenv("MARKET_LIQUIDATION_HOUR", "15"))
MARKET_LIQUIDATION_MINUTE = int(os.getenv("MARKET_LIQUIDATION_MINUTE", "20"))
ALLOW_OVERNIGHT_HOLDING = os.getenv("ALLOW_OVERNIGHT_HOLDING", "false").strip().lower() in ("1", "true", "yes")

# 同じ銘柄の重複注文を防ぐためのロック期間（秒）
ORDER_LOCK_SECONDS = 60

# 1回の取引あたりの最大注文金額（運用資金10万円の30%）
MAX_ORDER_AMOUNT_PER_TRADE = float(os.getenv("MAX_ORDER_AMOUNT_PER_TRADE", "30000"))

# フィルタリング候補から目指す分散ポジション数
TARGET_POSITIONS = int(os.getenv("TARGET_POSITIONS", "3"))

# 1日あたりの最大注文数
MAX_ORDER_COUNT_PER_DAY = int(os.getenv("MAX_ORDER_COUNT_PER_DAY", "10"))

# 1日の損失限度額（運用資本の比率、例：0.02 = 2%）
DAILY_LOSS_LIMIT_RATIO = float(os.getenv("DAILY_LOSS_LIMIT_RATIO", "0.02"))

# APIソフトリミット - リスク管理用の内部閾値
API_SOFT_LIMIT = float(os.getenv("API_SOFT_LIMIT", "1000000"))

# kabuステーションAPIの実行回数制限を超えないための最小呼出間隔（秒）
API_REQUEST_INTERVAL_SECONDS = float(os.getenv("API_REQUEST_INTERVAL_SECONDS", "0.12"))

# 401応答を受けた際にトークンを再取得する最小間隔（秒）。この間隔内は取得済みの
# トークンで再試行し、/tokenへの呼び出し自体は増やさない
KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS = float(os.getenv("KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS", "60"))

# トークン再取得後も401が続く（復旧失敗）場合に、次の再取得試行まであける間隔（秒）。
# 他プロセスとのトークン発行の奪い合いを防ぐための間隔
KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS = float(os.getenv("KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS", "300"))

# 対象銘柄全件の板取得が何回連続で失敗したら通知するか（取引ループの1周を1回と数える）
BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD = int(os.getenv("BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD", "3"))


def _positive_int_env(name: str, raw: str) -> int:
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name}は1以上の整数を指定してください: {raw!r}")
    return value


# 注文履歴・Paper状態ファイルの保存が何回連続で失敗したら新規買いを止めるか(売り・決済は止めない)
STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD = _positive_int_env(
    "STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", os.getenv("STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD", "3")
)

# 強制決済(緊急停止・EOD)時に保有株一覧の取得が失敗した場合の再試行回数・間隔(秒)。
# 失敗(None)と保有ゼロ([])を取り違えないための安全策
LIQUIDATION_POSITIONS_FETCH_RETRIES = int(os.getenv("LIQUIDATION_POSITIONS_FETCH_RETRIES", "3"))
LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS = float(
    os.getenv("LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS", "5")
)

# 取引に利用可能な運用資本
OPERATING_CAPITAL = float(os.getenv("OPERATING_CAPITAL", "100000"))

# 日次分析(確定日足更新・答え合わせ・分析通知)の開始時刻
DAILY_ANALYSIS_START_TIME = time.fromisoformat(
    os.getenv("DAILY_ANALYSIS_START_TIME", "16:30")
)

# スクリーニング時の株価上限に掛ける安全マージン
SCREENING_PRICE_MARGIN = float(os.getenv("SCREENING_PRICE_MARGIN", "0.9"))

# ADR-0001: 出来高ランキングと値上がり率ランキングを統合する際、値上がり率側に
# 掛ける重み係数。1.0が従来通りの等重み。値上がり率ランキング上位の銘柄ほど
# 既に株価が伸びており、予算上限(株価上限)に近づきやすいという診断結果
# (2026-09-19, analyze_filter_entry_alignment.py)を踏まえ、値上がり率側の
# 影響を弱めることで、予算内に収まる候補を増やす狙い。
# 【要調整】この数値は実データでの検証前の暫定値であり、1.0(現状維持)としている。
# 値上がり率と事後の予算超過率との相関を見た上で、適切な値に調整すること。
PRICE_GAIN_WEIGHT = float(os.getenv("PRICE_GAIN_WEIGHT", "1.0"))


def get_screening_price_cap() -> float:
    """スクリーニング時点で候補として残す株価の上限を動的に計算する。"""
    if TARGET_POSITIONS <= 0 or ORDER_UNIT <= 0:
        raise ValueError("TARGET_POSITIONSとORDER_UNITは正数を指定してください。")
    budget_per_position = min(
        OPERATING_CAPITAL / TARGET_POSITIONS,
        MAX_ORDER_AMOUNT_PER_TRADE,
    )
    return (budget_per_position / ORDER_UNIT) * SCREENING_PRICE_MARGIN

# スクリーニングのランキング取得対象とする市場区分（/rankingのExchangeDivision）
# 全市場(ALL)は1回の呼び出しにつき上位50件しか返らず、値がさ株に偏りやすいため
# 市場区分ごとに個別取得して母集団を拡大する（福証・札証は取引対象外のため含めない）
SCREENING_EXCHANGE_DIVISIONS = [
    division.strip()
    for division in os.getenv("SCREENING_EXCHANGE_DIVISIONS", "TP,TS,TG").split(",")
    if division.strip()
]

# Kabu Station APIの登録銘柄上限に合わせたスクリーニング処理単位
SCREENING_BATCH_SIZE = 50

# 売買シグナルのトレンド確認（Wilder方式のRSI）
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
RSI_ENTRY_THRESHOLD = float(os.getenv("RSI_ENTRY_THRESHOLD", os.getenv("RSI_BUY_THRESHOLD", "55")))
RSI_ENTRY_THRESHOLD_CAUTION = float(os.getenv("RSI_ENTRY_THRESHOLD_CAUTION", "60"))
RSI_EXIT_THRESHOLD = float(os.getenv("RSI_EXIT_THRESHOLD", os.getenv("RSI_SELL_THRESHOLD", "45")))
RSI_MINIMUM_CLOSES = int(os.getenv("RSI_MINIMUM_CLOSES", "30"))

# ATRベースの銘柄別ボラティリティ調整
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
ATR_CAUTION_RATIO = float(os.getenv("ATR_CAUTION_RATIO", "1.5"))
ATR_DANGER_RATIO = float(os.getenv("ATR_DANGER_RATIO", "2.0"))
ATR_CAUTION_LOT_RATIO = float(os.getenv("ATR_CAUTION_LOT_RATIO", "0.5"))
ATR_DANGER_ACTION = os.getenv("ATR_DANGER_ACTION", "skip").strip().lower()
if ATR_PERIOD <= 0:
    raise ValueError("ATR_PERIODは正数を指定してください。")
if ATR_CAUTION_RATIO <= 0 or ATR_DANGER_RATIO <= ATR_CAUTION_RATIO:
    raise ValueError("ATRのボラティリティ閾値が不正です。")
if not 0 < ATR_CAUTION_LOT_RATIO <= 1:
    raise ValueError("ATR_CAUTION_LOT_RATIOは0より大きく1以下を指定してください。")
if ATR_DANGER_ACTION not in {"skip", "minimum"}:
    raise ValueError("ATR_DANGER_ACTIONはskipまたはminimumを指定してください。")

ATR_STOP_NORMAL_MULTIPLIER = float(os.getenv("ATR_STOP_NORMAL_MULTIPLIER", "1.5"))
ATR_STOP_CAUTION_MULTIPLIER = float(os.getenv("ATR_STOP_CAUTION_MULTIPLIER", "1.0"))
ATR_STOP_DANGER_MULTIPLIER = float(os.getenv("ATR_STOP_DANGER_MULTIPLIER", "0.7"))
if min(ATR_STOP_NORMAL_MULTIPLIER, ATR_STOP_CAUTION_MULTIPLIER, ATR_STOP_DANGER_MULTIPLIER) <= 0:
    raise ValueError("ATR損切り倍率は正数を指定してください。")

# ADR-0006: 含み益が一定以上(ATR基準)乗った場合のみ使う、利確専用のATRトレーリング倍率
ATR_PROFIT_LOCK_NORMAL_MULTIPLIER = float(os.getenv("ATR_PROFIT_LOCK_NORMAL_MULTIPLIER", "2.5"))
ATR_PROFIT_LOCK_CAUTION_MULTIPLIER = float(os.getenv("ATR_PROFIT_LOCK_CAUTION_MULTIPLIER", "2.0"))
ATR_PROFIT_LOCK_DANGER_MULTIPLIER = float(os.getenv("ATR_PROFIT_LOCK_DANGER_MULTIPLIER", "1.0"))
if min(
    ATR_PROFIT_LOCK_NORMAL_MULTIPLIER,
    ATR_PROFIT_LOCK_CAUTION_MULTIPLIER,
    ATR_PROFIT_LOCK_DANGER_MULTIPLIER,
) <= 0:
    raise ValueError("ATR利確倍率は正数を指定してください。")
ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE = float(os.getenv("ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE", "0.5"))
if ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE < 0:
    raise ValueError("ATR利確トリガーの倍数は0以上を指定してください。")

FILTER_DECISION_OBSERVATION_DAYS = int(os.getenv("FILTER_DECISION_OBSERVATION_DAYS", "5"))
if FILTER_DECISION_OBSERVATION_DAYS <= 0:
    raise ValueError("判定イベントの観測継続営業日数は正数を指定してください。")

# 市場全体の荒れ具合（MarketRegime）
MARKET_REGIME_THRESHOLDS = MarketRegimeThresholds(
    realized_vol_caution=float(os.getenv("MARKET_REGIME_REALIZED_VOL_CAUTION", "17.0")),
    realized_vol_danger=float(os.getenv("MARKET_REGIME_REALIZED_VOL_DANGER", "29.0")),
    vix_caution=float(os.getenv("MARKET_REGIME_VIX_CAUTION", "17.0")),
    vix_danger=float(os.getenv("MARKET_REGIME_VIX_DANGER", "27.0")),
    nikkei_change_upgrade=float(os.getenv("MARKET_REGIME_NIKKEI_CHANGE_UPGRADE", "2.0")),
)
MARKET_REGIME_REALIZED_VOL_WINDOW = int(os.getenv("MARKET_REGIME_REALIZED_VOL_WINDOW", "20"))
if MARKET_REGIME_REALIZED_VOL_WINDOW <= 0:
    raise ValueError("MARKET_REGIME_REALIZED_VOL_WINDOWは正数を指定してください。")
MARKET_REGIME_DATA_RANGE = os.getenv("MARKET_REGIME_DATA_RANGE", "3mo")
# 日足が前営業日まで更新されていないときの再取得までの待機秒数
MARKET_REGIME_RETRY_WAIT_SECONDS = float(os.getenv("MARKET_REGIME_RETRY_WAIT_SECONDS", "20"))
MARKET_REGIME_ADX_TREND_THRESHOLD = float(
    os.getenv("MARKET_REGIME_ADX_TREND_THRESHOLD", "27.0")
)
if MARKET_REGIME_ADX_TREND_THRESHOLD < 0:
    raise ValueError("MARKET_REGIME_ADX_TREND_THRESHOLDは0以上を指定してください。")

# バックテストの約定コスト。各値は証券会社の料金プラン・運用実績に合わせて環境変数で調整する。
BACKTEST_FEE_RATE = float(os.getenv("BACKTEST_FEE_RATE", "0.00055"))
BACKTEST_MARKET_SLIPPAGE_BPS = float(os.getenv("BACKTEST_MARKET_SLIPPAGE_BPS", "5"))
BACKTEST_EXECUTION_DELAY_BARS = int(os.getenv("BACKTEST_EXECUTION_DELAY_BARS", "1"))
BACKTEST_ORDER_TYPE = os.getenv("BACKTEST_ORDER_TYPE", "market").strip().lower()
if BACKTEST_FEE_RATE < 0:
    raise ValueError("BACKTEST_FEE_RATE は0以上を指定してください。")
if BACKTEST_MARKET_SLIPPAGE_BPS < 0:
    raise ValueError("BACKTEST_MARKET_SLIPPAGE_BPS は0以上を指定してください。")
if BACKTEST_EXECUTION_DELAY_BARS < 0:
    raise ValueError("BACKTEST_EXECUTION_DELAY_BARS は0以上を指定してください。")
if BACKTEST_ORDER_TYPE not in {"market", "limit"}:
    raise ValueError("BACKTEST_ORDER_TYPE は market または limit を指定してください。")

# ペーパートレードでは発注後の次回価格を観測できないため、現在価格に不利方向の成行スリッページを適用する。
PAPER_FEE_RATE = float(os.getenv("PAPER_FEE_RATE", str(BACKTEST_FEE_RATE)))
PAPER_MARKET_SLIPPAGE_BPS = float(os.getenv("PAPER_MARKET_SLIPPAGE_BPS", str(BACKTEST_MARKET_SLIPPAGE_BPS)))
if PAPER_FEE_RATE < 0:
    raise ValueError("PAPER_FEE_RATE は0以上を指定してください。")
if PAPER_MARKET_SLIPPAGE_BPS < 0:
    raise ValueError("PAPER_MARKET_SLIPPAGE_BPS は0以上を指定してください。")

# 分足Parquetの保存先。
MINUTE_BAR_PARQUET_DIR = Path(
    os.getenv("MINUTE_BAR_PARQUET_DIR", str(_repo_root / "data" / "minute_bars_parquet"))
)
# 日経225など指数の分足Parquetの保存先。個別銘柄用とはDuckDB読み込み(symbol列がBIGINT)を壊さないため分離する。
INDEX_MINUTE_BAR_PARQUET_DIR = Path(
    os.getenv("INDEX_MINUTE_BAR_PARQUET_DIR", str(_repo_root / "data" / "index_minute_bars_parquet"))
)
# 日次分析での当日分足保存に、価格帯別(450円帯・900円帯)のフィルタ結果の銘柄も含めるか。
DAILY_MINUTE_BAR_INCLUDE_PRICE_BANDS = os.getenv(
    "DAILY_MINUTE_BAR_INCLUDE_PRICE_BANDS", "false"
).strip().lower() in ("1", "true", "yes")
SCREENING_RESULT_DIRECTORY = Path(
    os.getenv("SCREENING_RESULT_DIRECTORY", str(_repo_root / "data" / "screening"))
)
FILTERING_RESULT_DIRECTORY = Path(
    os.getenv("FILTERING_RESULT_DIRECTORY", str(_repo_root / "data" / "filtering"))
)
FILTERING_DIAGNOSTICS_DIRECTORY = Path(
    os.getenv("FILTERING_DIAGNOSTICS_DIRECTORY", str(_repo_root / "data" / "filtering_diagnostics"))
)
SCREENING_API_CHECK_DIRECTORY = Path(
    os.getenv("SCREENING_API_CHECK_DIRECTORY", str(_repo_root / "data" / "regulation" / "screening_api_checks"))
)
SCREENING_PRICE_BAND_RESULT_ROOT = Path(
    os.getenv("SCREENING_PRICE_BAND_RESULT_ROOT", str(_repo_root / "data" / "screening_price_bands"))
)
FILTERING_PRICE_BAND_RESULT_ROOT = Path(
    os.getenv("FILTERING_PRICE_BAND_RESULT_ROOT", str(_repo_root / "data" / "filtering_price_bands"))
)
SCREENING_ALTERNATE_PRICE_CAPS = tuple(
    float(value.strip())
    for value in os.getenv("SCREENING_ALTERNATE_PRICE_CAPS", "450,900").split(",")
    if value.strip()
)
if any(value <= 0 for value in SCREENING_ALTERNATE_PRICE_CAPS):
    raise ValueError("SCREENING_ALTERNATE_PRICE_CAPSは正数を指定してください。")
if len(set(SCREENING_ALTERNATE_PRICE_CAPS)) != len(SCREENING_ALTERNATE_PRICE_CAPS):
    raise ValueError("SCREENING_ALTERNATE_PRICE_CAPSに重複があります。")
FILTERING_PRICE_BAND_DEADLINE_TIME = time.fromisoformat(
    os.getenv("FILTERING_PRICE_BAND_DEADLINE_TIME", "09:34")
)
# 全価格帯をまとめて板取得する際の最大同時リクエスト数
FILTER_BOARD_MAX_CONCURRENCY = _positive_int_env(
    "FILTER_BOARD_MAX_CONCURRENCY", os.getenv("FILTER_BOARD_MAX_CONCURRENCY", "3")
)


def _non_negative_env(name: str, default: str, cast):
    raw = os.getenv(name, default)
    try:
        value = cast(raw.strip())
    except ValueError:
        value = -1
    if value < 0:
        raise ValueError(f"{name}は0以上の数値を指定してください: {raw!r}")
    return value


# J-Quants API V2。接続先はAPIキーの誤送信を避けるため固定する。
JQUANTS_API_KEY = os.getenv("JQUANTS_API_KEY", "").strip()
JQUANTS_API_BASE_URL = "https://api.jquants.com/v2"
JQUANTS_TIMEOUT_SECONDS = _non_negative_env("JQUANTS_TIMEOUT_SECONDS", "30", float)
JQUANTS_MAX_ATTEMPTS = _non_negative_env("JQUANTS_MAX_ATTEMPTS", "4", int)
JQUANTS_REQUEST_INTERVAL_SECONDS = _non_negative_env(
    "JQUANTS_REQUEST_INTERVAL_SECONDS", "1.2", float
)
JQUANTS_429_WAIT_SECONDS = _non_negative_env("JQUANTS_429_WAIT_SECONDS", "120", float)
JQUANTS_RETRY_BASE_SECONDS = _non_negative_env("JQUANTS_RETRY_BASE_SECONDS", "1", float)
JQUANTS_RETRY_MAX_SECONDS = _non_negative_env("JQUANTS_RETRY_MAX_SECONDS", "30", float)
JQUANTS_RAW_DIRECTORY = Path(
    os.getenv("JQUANTS_RAW_DIRECTORY", str(_repo_root / "data" / "jquants" / "raw"))
)
JQUANTS_CANONICAL_DIRECTORY = Path(
    os.getenv("JQUANTS_CANONICAL_DIRECTORY", str(_repo_root / "data" / "jquants" / "canonical"))
)
JQUANTS_LEDGER_DATABASE_FILE = Path(
    os.getenv(
        "JQUANTS_LEDGER_DATABASE_FILE",
        str(_repo_root / "data" / "jquants" / "ledger.sqlite3"),
    )
)
if JQUANTS_TIMEOUT_SECONDS <= 0:
    raise ValueError("JQUANTS_TIMEOUT_SECONDSは正数を指定してください。")
if JQUANTS_MAX_ATTEMPTS <= 0:
    raise ValueError("JQUANTS_MAX_ATTEMPTSは正数を指定してください。")

# 全価格帯で板の売買代金・売買高が取れなかった銘柄を、結果保存前に再取得する
FILTER_BOARD_RETRY_ENABLED = os.getenv("FILTER_BOARD_RETRY_ENABLED", "true").strip().lower() in ("1", "true", "yes")
FILTER_BOARD_RETRY_MAX_ROUNDS = _non_negative_env("FILTER_BOARD_RETRY_MAX_ROUNDS", "2", int)
FILTER_BOARD_RETRY_WAIT_SECONDS = _non_negative_env("FILTER_BOARD_RETRY_WAIT_SECONDS", "10", float)
# board APIが429を返した際の待機時間と追加再試行回数
FILTER_BOARD_429_RETRY_WAIT_SECONDS = _non_negative_env("FILTER_BOARD_429_RETRY_WAIT_SECONDS", "1", float)
# 429応答時に再試行する最大回数
FILTER_BOARD_429_MAX_RETRIES = _non_negative_env("FILTER_BOARD_429_MAX_RETRIES", "2", int)
# リトライの打ち切り時刻 = FILTERING_PRICE_BAND_DEADLINE_TIME - この秒数
FILTER_BOARD_RETRY_MARGIN_SECONDS = _non_negative_env("FILTER_BOARD_RETRY_MARGIN_SECONDS", "30", float)

# 朝(09:30)の追加価格帯(450円・900円)の板取得。既定は停止し、昼のYahoo分足推定へ回す。trueで朝の実施に戻せる
FILTERING_MORNING_ALTERNATE_BANDS_ENABLED = os.getenv(
    "FILTERING_MORNING_ALTERNATE_BANDS_ENABLED", "false"
).strip().lower() in ("1", "true", "yes")

# 昼フィルタ(追加価格帯をYahoo 1分足の09:00〜09:30売買代金から推定する処理)
MIDDAY_FILTERING_START_TIME = time.fromisoformat(os.getenv("MIDDAY_FILTERING_START_TIME", "12:00"))
MIDDAY_FILTERING_DEADLINE_TIME = time.fromisoformat(os.getenv("MIDDAY_FILTERING_DEADLINE_TIME", "12:25"))
if MIDDAY_FILTERING_DEADLINE_TIME <= MIDDAY_FILTERING_START_TIME:
    raise ValueError("MIDDAY_FILTERING_DEADLINE_TIMEはMIDDAY_FILTERING_START_TIMEより後を指定してください。")
# Yahoo分足1銘柄あたりのタイムアウト秒と、失敗時の追加リトライ回数(0で再試行なし)・リトライ前の待機秒
MIDDAY_FILTERING_FETCH_TIMEOUT_SECONDS = _non_negative_env("MIDDAY_FILTERING_FETCH_TIMEOUT_SECONDS", "30", float)
MIDDAY_FILTERING_FETCH_RETRIES = _non_negative_env("MIDDAY_FILTERING_FETCH_RETRIES", "1", int)
MIDDAY_FILTERING_RETRY_WAIT_SECONDS = _non_negative_env("MIDDAY_FILTERING_RETRY_WAIT_SECONDS", "2", float)
# 午前のみの取引日など、昼の処理を実行しない日付(YYYY-MM-DD, カンマ区切り)
MIDDAY_FILTERING_SKIP_DATES = tuple(
    value.strip() for value in os.getenv("MIDDAY_FILTERING_SKIP_DATES", "").split(",") if value.strip()
)
# 昼の結果・診断・270円比較の保存先。朝の板の結果(FILTERING_*)とは別に保存する
MIDDAY_FILTERING_RESULT_ROOT = Path(
    os.getenv("MIDDAY_FILTERING_RESULT_ROOT", str(_repo_root / "data" / "filtering_midday"))
)
# 昼フィルタの検証用実行(--verify)の保存先。本番の保存先とは分ける
MIDDAY_FILTERING_VERIFY_RESULT_ROOT = Path(
    os.getenv("MIDDAY_FILTERING_VERIFY_RESULT_ROOT", str(_repo_root / "data" / "filtering_midday_verify"))
)
MINUTE_BAR_PARQUET_VERIFY_DIR = Path(
    os.getenv("MINUTE_BAR_PARQUET_VERIFY_DIR", str(_repo_root / "data" / "minute_bars_parquet_verify"))
)
MINUTE_BACKFILL_UNSELECTED_TIMEOUT_SECONDS = _non_negative_env(
    "MINUTE_BACKFILL_UNSELECTED_TIMEOUT_SECONDS", "1800", float
)

# ================================================================================
# 注文履歴・市場設定
# ================================================================================

# 注文履歴を保存するファイル
ORDER_HISTORY_FILE = "data/trading/order_history.json"
PAPER_ACCOUNT_STATE_FILE = "data/trading/paper_account_state.json"
KILL_SWITCH_BASELINE_FILE = "data/trading/kill_switch_baseline.json"
DAILY_REPORT_DIRECTORY = _repo_root / "data" / "reports" / "daily"
FILTER_DECISION_DATABASE_FILE = _repo_root / "data" / "state" / "filter_decision_events.sqlite3"
MARKET_REGIME_DAILY_DATABASE_FILE = _repo_root / "data" / "state" / "market_regime_daily.sqlite3"
BACKTEST_FILTER_DECISION_DIRECTORY = _repo_root / "data" / "backtest" / "filter_events"
BACKTEST_INPUT_SNAPSHOT_DIRECTORY = _repo_root / "data" / "backtest" / "input_snapshots"
# 移行スクリプトの移行先（過去分の保管用）専用。バックテスト本体は使用しない。
LEGACY_BACKTEST_FILTER_DECISION_DATABASE_FILE = _repo_root / "data" / "backtest" / "state" / "filter_decision_events.sqlite3"
EMERGENCY_STOP_FILE = Path(
    os.getenv("EMERGENCY_STOP_FILE", str(_repo_root / "data" / "trading" / "emergency_stop"))
)

# 市場クローズ時刻（日本標準時）
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 0
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30
# 取引中間報告の送信時刻（日本標準時）
TRADING_PROGRESS_REPORT_1_HOUR = int(os.getenv("TRADING_PROGRESS_REPORT_1_HOUR", "11"))
TRADING_PROGRESS_REPORT_1_MINUTE = int(os.getenv("TRADING_PROGRESS_REPORT_1_MINUTE", "30"))
TRADING_PROGRESS_REPORT_2_HOUR = int(os.getenv("TRADING_PROGRESS_REPORT_2_HOUR", "14"))
TRADING_PROGRESS_REPORT_2_MINUTE = int(os.getenv("TRADING_PROGRESS_REPORT_2_MINUTE", "0"))


# 日次LLM分析設定（明示的に有効化した場合のみ利用）
LLM_DAILY_ANALYSIS_ENABLED = os.getenv("LLM_DAILY_ANALYSIS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
LLM_DIARY_ENABLED = os.getenv("LLM_DIARY_ENABLED", "false").strip().lower() in ("1", "true", "yes")
LLM_API_KEY = os.getenv("OPENAI_API_KEY", os.getenv("LLM_API_KEY", ""))
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")
LLM_TIMEOUT_SECONDS = float(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
LLM_API_URL = os.getenv("LLM_API_URL", "https://api.openai.com/v1/chat/completions")

# 例外原因分析用LLM設定（明示的に有効化した場合のみ利用。同一原因のエラーはキャッシュを再利用し、クールダウン間隔でのみ再分析）
LLM_ERROR_ANALYSIS_ENABLED = os.getenv("LLM_ERROR_ANALYSIS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
LLM_ERROR_ANALYSIS_COOLDOWN_MINUTES = int(os.getenv("LLM_ERROR_ANALYSIS_COOLDOWN_MINUTES", "60"))
LLM_ERROR_ANALYSIS_MODEL = os.getenv("LLM_ERROR_ANALYSIS_MODEL", LLM_MODEL)
# リトライで解決しうる想定内の例外はLLM分析の対象外とする（クラス名でMRO照合）
LLM_ERROR_ANALYSIS_SKIP_EXCEPTION_TYPES = tuple(
    name.strip()
    for name in os.getenv(
        "LLM_ERROR_ANALYSIS_SKIP_EXCEPTION_TYPES",
        "ConnectionError,Timeout,ConnectTimeout,ReadTimeout,JSONDecodeError",
    ).split(",")
    if name.strip()
)

# スクリーニング/フィルタリング異常検知用LLM設定（明示的に有効化した場合のみ利用。閾値を下回った時のみLLMを呼び出す）
LLM_ANOMALY_ANALYSIS_ENABLED = os.getenv("LLM_ANOMALY_ANALYSIS_ENABLED", "false").strip().lower() in ("1", "true", "yes")
SCREENING_ANOMALY_MIN_SYMBOLS = int(os.getenv("SCREENING_ANOMALY_MIN_SYMBOLS", "5"))
FILTERING_ANOMALY_MIN_SYMBOLS = int(os.getenv("FILTERING_ANOMALY_MIN_SYMBOLS", "3"))


# ================================================================================
# API設定
# ================================================================================

# 環境に応じたAPIポート選択（デモ/本番）
API_PORT = os.getenv("API_PORT_DEV", "18081") if IS_DEMO else os.getenv("API_PORT_PRD", "18080")

# 環境に応じたAPIパスワード選択
API_PASSWORD = _load_required_env("API_PASSWORD_DEV" if IS_DEMO else "API_PASSWORD_PRD", allow_missing=_ALLOW_MISSING_ENV)

# バリデーション: 本番モードは実取引が明示的に有効化されている必要がある
if TRADING_MODE == "live" and (IS_DEMO or not ENABLE_LIVE_ORDERING):
    raise ValueError(
        "ライブ注文には IS_DEMO=false と ENABLE_LIVE_ORDERING=true が必要です。"
    )

# Kabu.com Station APIのベースURL
BASE_URL = f"http://localhost:{API_PORT}/kabusapi"


# ================================================================================
# ログ設定
# ================================================================================

# ログは実行元ごとに保存先を分ける
_LOG_CATEGORY = "tests" if _is_test_runtime() else "application"
LOG_DIRECTORY = _repo_root / "data" / "logs" / _LOG_CATEGORY
LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
_LOG_FILE_NAME = "pytest.log" if _is_test_runtime() else "trade_project.log"
LOG_FILE_PATH = str(LOG_DIRECTORY / _LOG_FILE_NAME)

# 保持する日次ログの世代数
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", str(5)))

# ログレベル (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper()



# ================================================================================
# Slack通知設定
# ================================================================================

# チャンネル別Incoming Webhook URL（緊急度別に3分割）
# critical: 約定・キルスイッチ・例外 / daily: 定型の日次ログ / analysis: 週次・月次分析等
SLACK_WEBHOOK_CRITICAL = _load_required_env("SLACK_WEBHOOK_CRITICAL", allow_missing=_ALLOW_MISSING_ENV)
SLACK_WEBHOOK_DAILY = _load_required_env("SLACK_WEBHOOK_DAILY", allow_missing=_ALLOW_MISSING_ENV)
SLACK_WEBHOOK_ANALYSIS = _load_required_env("SLACK_WEBHOOK_ANALYSIS", allow_missing=_ALLOW_MISSING_ENV)