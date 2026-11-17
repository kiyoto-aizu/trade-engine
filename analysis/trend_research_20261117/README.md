# トレンド発生条件の定量調査（2026-11-17, Issue #71）

一時的な調査用コード。実運用コード（`src/`）からは参照されない読み取り専用の分析スクリプト群。

- 目的: トレンドが発生しやすい銘柄特性・市場状態を定量化し、スクリーニング・フィルタリング・売買条件・MarketRegime の改善候補を作る（Issue #71）。
- 入力:
  - `D:\jquants\canonical\daily\date=*/generations/<CURRENT>/part-00000.parquet`（J-Quants 日足、世代は各 `CURRENT` ファイルで解決）
  - `D:\jquants\canonical\minute\date=*/...`（J-Quants 分足）
  - `data/universe/listed_securities.csv`（時点整合マスタ、listed_from/listed_to 付き）
- 出力: `data/analysis/trend_research_20261117/`（DuckDB ファイル・JSON/CSV 集計・報告書）。既存ファイルは上書きしない。
- 実行環境: リポジトリの `.venv`（Python 3.14, duckdb のみ。pandas/numpy 不使用）。
- 制約: 既存データへの書き込みなし。外部 API 呼び出しなし。依存パッケージ追加なし。

## 実行順

```
.venv\Scripts\python.exe analysis\trend_research_20261117\01_data_quality.py
.venv\Scripts\python.exe analysis\trend_research_20261117\02_build_daily_panel.py
.venv\Scripts\python.exe analysis\trend_research_20261117\03_trend_analysis.py
.venv\Scripts\python.exe analysis\trend_research_20261117\04_intraday_analysis.py
.venv\Scripts\python.exe analysis\trend_research_20261117\05_walkforward.py
```

## 指標定義の注意

- RSI は Cutler 方式（単純移動平均）で近似する。本番の Wilder 方式 RSI とは値が異なる（傾向分析用途のため許容。差は報告書に明記）。
- ADX 等 Wilder 平滑化が必須の指標は、同様に SMA 平滑で近似し `*_sma` と命名する。
- 特徴量は「エントリー日 t の朝時点で利用可能な情報」= 前営業日 t-1 までの確定日足（および t 日 9:00-9:29 の分足）のみから計算する。
