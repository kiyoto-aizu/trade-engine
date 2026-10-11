# 処理状態と判定イベント

取引・バックテストの判定イベントSQLiteと、LLMエラー分析の状態を保持します。週次・月次分析とエラー分析が利用します。

`market_regime_daily.sqlite3` は、取引開始時の市場レジーム判定(緩和前後のレジーム、実現ボラ・VIX・日経前日比・ADX、使った日足の日付と終値、閾値、取得失敗理由)を1取引日・1実行モードにつき1行で保存します。`run_trading.py`が書き込み、CAUTION判定の検証にだけ使います。

`filter_decision_events.sqlite3` の `nikkei_intraday_change_percent`(見送り時刻の日経225の当日前日比%)と `nikkei_intraday_as_of`(算出に使った最終1分足の時刻)は、`MARKET_REGIME_DANGER_SKIP`・`MARKET_REGIME_CAUTION_RSI_FILTER`・`ATR_DANGER_SKIP`・`ADX_TREND_RELIEF` だけに記録します。取得失敗時や過去分はNULLです。