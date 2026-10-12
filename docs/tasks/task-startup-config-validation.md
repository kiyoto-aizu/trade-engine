# 改修タスク: 起動時の売買設定値検証

- 起票日: 2026-10-04
- 対象: `src/config/config.py`、関連する設定・起動テスト、`docs/reference/config-reference.md`
- ステータス: **`run_trading.py`への適用を実装済み・コミット済み（`c56be4a`、2026-10-04）**。2026-10-12、適用範囲は`run_trading.py`のみで据え置き、他の起動口へは広げないと決定
- 実装: `src/config/startup_validation.py`（`validate_startup_config` / `find_config_violations` / `ConfigValidationError`）、`run_trading._validate_config_or_exit()`、`tests/test_startup_validation.py`
- 運用者確認: `.\.venv\Scripts\python.exe scripts/check_startup_config.py`（現在の`.env`で検証し、違反があれば全件表示して終了コード1。秘密値は表示しない）
- 実施時期: **2026-10-04**
- 関連調査: 2026-10-02「売買部分サイレントスキップ横並び調査」。第1弾は実行時の見送り理由を記録するが、不正な売買パラメータを起動時に網羅検証するものではない。

## 背景

設定値の型変換が行われる項目でも、値の範囲や項目間の関係が起動時に検査されていないものがある。設定の誤りが無注文、損失制限の即時発動、異常な資金配分などとして取引開始後に現れる可能性がある。

## 検証項目一覧

以下の範囲を新しい起動時検証の決定値とする。設定名・既定値は`src/config/config.py`で再確認した。数値変換自体が失敗した場合も設定不正として集約対象にする。

| グループ | 項目 | 既定値 | 現状の検証 | 起動時に許容する範囲 |
|---|---|---:|---|---|
| 資金・上限 | `OPERATING_CAPITAL` | 100000 | なし | 0より大きい |
| 資金・上限 | `MAX_ORDER_AMOUNT_PER_TRADE` | 30000 | なし | 0より大きく、`OPERATING_CAPITAL`以下 |
| 資金・上限 | `TARGET_POSITIONS` | 3 | `get_screening_price_cap()`呼出時のみ、1以上相当を検査 | 1以上の整数 |
| 資金・上限 | `MAX_ORDER_COUNT_PER_DAY` | 10 | なし | 1以上の整数 |
| 資金・上限 | `DAILY_LOSS_LIMIT_RATIO` | 0.02 | なし | 0より大きく1以下 |
| 資金・上限 | `API_SOFT_LIMIT` | 1000000 | なし | 0より大きい |
| RSI | `RSI_PERIOD` | 14 | なし | 2以上の整数 |
| RSI | `RSI_MINIMUM_CLOSES` | 30 | なし | `RSI_PERIOD`より大きい |
| RSI | `RSI_ENTRY_THRESHOLD` | 55 | なし | 0〜100 |
| RSI | `RSI_EXIT_THRESHOLD` | 45 | なし | 0〜100 |
| RSI | `RSI_ENTRY_THRESHOLD_CAUTION` | 60 | なし | 0〜100 |
| RSI | 閾値間の関係 | — | なし | 売り閾値 < 通常買い閾値 ≦ CAUTION買い閾値 |
| レジーム | `MARKET_REGIME_REALIZED_VOL_CAUTION` / `_DANGER` | 17.0 / 29.0 | `MarketRegimeThresholds`が有限・非負かつDANGER > CAUTIONを検証 | CAUTION < DANGER |
| レジーム | `MARKET_REGIME_VIX_CAUTION` / `_DANGER` | 17.0 / 27.0 | `MarketRegimeThresholds`が有限・非負かつDANGER > CAUTIONを検証 | CAUTION < DANGER |
| レジーム | `MARKET_REGIME_NIKKEI_CHANGE_UPGRADE` | 2.0 | 有限・非負を検証 | 0より大きい |
| 決済時刻 | `MARKET_LIQUIDATION_HOUR` / `MARKET_LIQUIDATION_MINUTE` | 15 / 20 | 整数変換のみ | 0〜23 / 0〜59 |
| 通信・復旧 | `BOARD_FETCH_CONSECUTIVE_FAILURE_THRESHOLD` | 3 | 整数変換のみ | 1以上の整数 |
| 通信・復旧 | `LIQUIDATION_POSITIONS_FETCH_RETRIES` | 3 | 整数変換のみ | 1以上の整数 |
| 通信・復旧 | `STATE_SAVE_CONSECUTIVE_FAILURE_THRESHOLD` | 3 | import時に1以上を検証（タスクBで追加） | 1以上の整数（追加。決定値表には元々無かった） |
| 通信・復旧 | `KABU_TOKEN_REFRESH_MIN_INTERVAL_SECONDS` | 60 | なし | 0以上 |
| 通信・復旧 | `KABU_TOKEN_REFRESH_FAILURE_BACKOFF_SECONDS` | 300 | なし | 0以上 |
| 通信・復旧 | `API_REQUEST_INTERVAL_SECONDS` | 0.12 | なし | 0以上 |
| 通信・復旧 | `LIQUIDATION_POSITIONS_FETCH_RETRY_BACKOFF_SECONDS` | 5 | なし | 0以上 |

## 検証済み（変更なし）

| 項目 | 既定値 | 既存の検証 |
|---|---:|---|
| `TRADING_MODE` | `paper` | `paper` / `live`以外をconfig import時に拒否 |
| live注文ガード | `IS_DEMO=true`, `ENABLE_LIVE_ORDERING=false` | `TRADING_MODE=live`には`IS_DEMO=false`かつ`ENABLE_LIVE_ORDERING=true`を要求 |
| API password・Slack webhook | 秘密値 | 通常runtimeでは`_load_required_env()`が必須値の欠落を拒否 |
| `ORDER_UNIT` | 100 | `get_screening_price_cap()`呼出時に正数を確認（環境変数ではない定数） |
| `ATR_PERIOD` / `ATR_CAUTION_RATIO` / `ATR_DANGER_RATIO` / `ATR_CAUTION_LOT_RATIO` / `ATR_DANGER_ACTION` | 14 / 1.5 / 2.0 / 0.5 / `skip` | 正数、DANGER > CAUTION、比率(0,1]、許容アクションを検証 |
| `ATR_STOP_*_MULTIPLIER` / `ATR_PROFIT_LOCK_*_MULTIPLIER` / `ATR_PROFIT_LOCK_TRIGGER_ATR_MULTIPLE` | 1.5/1.0/0.7 / 2.5/2.0/1.0 / 0.5 | 各倍率は正数、triggerは0以上 |
| `FILTER_DECISION_OBSERVATION_DAYS` / `MARKET_REGIME_REALIZED_VOL_WINDOW` / `MARKET_REGIME_ADX_TREND_THRESHOLD` | 5 / 20 / 27.0 | 期間は正数、ADX閾値は0以上 |
| `MarketRegimeThresholds`の実現vol/VIX・日経変化 | 17/29 / 17/27 / 2.0 | `__post_init__()`が全値finite・非負、DANGER > CAUTIONを確認 |
| `BACKTEST_FEE_RATE` / `BACKTEST_MARKET_SLIPPAGE_BPS` / `BACKTEST_EXECUTION_DELAY_BARS` / `BACKTEST_ORDER_TYPE` | 0.00055 / 5 / 1 / `market` | コスト・遅延は0以上、order typeは`market` / `limit` |
| `PAPER_FEE_RATE` / `PAPER_MARKET_SLIPPAGE_BPS` | Backtest値を継承 | 0以上 |
| `SCREENING_ALTERNATE_PRICE_CAPS` | 450, 900 | 各値は正数かつ重複なし |

これら既存検証はconfig import中に実行されるため、例外になる入力では`configure_logging()`やSlack通知より先に停止する。本タスクでは既存検証の移動・変更は行わず、既知の弱点として残す。`TARGET_POSITIONS`の既存検査はimport時ではなく、`get_screening_price_cap()`呼出時である。

### 対象entrypoint数

`src/entrypoints/`には20ファイルがあり、そのうち現行ASTで`src.config.config`を直接importするものは13件（`run_trading.py`等）だった。レビュー調査メモの14件とは一致しない。この調査結果にかかわらず、2026-10-12に起動時検証は`run_trading.py`のみへの適用で据え置き、他の起動口へは広げないと決定した。

## 必須対応

- 売買・サイジングに影響する設定項目と相互制約について、許容範囲、範囲外時の結果、検証タイミングを一覧化する。
- coding-guidelines §3のfail-fast方針と整合し、無効な値で売買処理が開始されないことを保証する。起動時検証にする場合は、起動入口で必要な全設定が検証されることをテストする。
- 既存のATR・Backtest・Paper等の検証との重複や一貫性を整理し、同じ設定の検証結果が使用経路ごとに変わらないようにする。
- 設定値そのものや秘密値を例外・ログへ出さない。エラーは設定名と不正理由を特定できる形にする。

## 決定事項

1. **範囲外値は起動を止める**: 上記「検証項目一覧」の範囲を適用し、coding-guidelines §3のfail-fastに揃える。
2. **違反は一括で報告する**: 新設検証は全対象の違反をリストへ集めてから一度に例外化し、最初の違反だけで止めない。既存のATR等のimport時検証は本タスクでは変更しない。
3. **検証はlogging設定後に実行する**: 候補関数名は`validate_startup_config(settings)`。値を引数で受け取る純粋な検証部とし、`config.py`をimportせず単体テスト可能にする。呼出し位置は`run_trading.py`の`main()`で`configure_logging()`直後とする。違反一覧をログとSlackへまとめて出し、その後起動を止める。
4. **`.env`はこの作業で読まない**: 実値の適合確認は実装前に運用者が各環境で行う。
5. **適用範囲は`run_trading.py`のみで据え置く（2026-10-12決定）**: 他の起動口へは広げない。

## 未完了の確認事項

1. 運用者は実装前に`.env`実値を確認し、特に`MAX_ORDER_AMOUNT_PER_TRADE <= OPERATING_CAPITAL`等の新制約に適合するか、値そのものを共有せず適合/不適合を確認する。

## 売買結果への影響

**変わりうる。** 無効値を起動時に拒否すると、従来は起動後に無注文・数量0・早期停止となっていた運用が起動不可になる。現在の運用値が範囲外なら取引開始日が変わり、約定数・損益および**40件カウントの起点**に影響する。

## 完了条件

- 範囲外値が複数ある場合、違反がまとめて表示されて起動が停止する。
- 範囲違反一覧が`configure_logging()`後にログとSlackの両方へ出力される。
- 既定値と運用者確認済みの`.env`実値で起動が停止しない（実値の確認は運用者が行う）。
- 検証関数は値を直接渡して単体テストでき、`config.py`のimportを必要としない。
- 有効境界値・範囲外値・NaN/inf・相互制約違反を確認し、範囲表とfail-fast挙動が一致する。
- 検証対象として決定したentrypointで、違反時にUseCase・PaperOrderClient・注文送信が開始されないことをテストする。
- テストは環境変数を明示的に隔離し、実`.env`の秘密値やユーザー設定を読み書きしない。
- 実kabu注文を発生させるテストは行わない。

## 関連資料

- [coding-guidelines.md §3](../architecture/coding-guidelines.md)（秘密情報・起動時fail-fast）
- [設定項目リファレンス](../reference/config-reference.md)（生成済み環境変数一覧）