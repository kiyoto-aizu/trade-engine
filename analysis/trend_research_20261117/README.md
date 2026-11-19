# トレンド発生条件の定量調査（2026-11-17, Issue #71）

一時的な調査用コード。実運用コード（`src/`）からは参照されない読み取り専用の分析スクリプト群。

- 目的: トレンドが発生しやすい銘柄特性・市場状態を定量化し、スクリーニング・フィルタリング・売買条件・MarketRegime の改善候補を作る（Issue #71）。
- 入力:
  - `D:\jquants\canonical\daily\date=*/generations/<CURRENT>/part-00000.parquet`（J-Quants 日足、世代は各 `CURRENT` ファイルで解決）
  - `D:\jquants\canonical\minute\date=*/...`（J-Quants 分足）
  - `data/universe/listed_securities.csv`（時点整合マスタ、listed_from/listed_to 付き）
  - `data/screening/*.json`、`data/filtering/*.json`、`data/state/market_regime_daily.sqlite3`（07・06、読み取りのみ）
- 出力: `data/analysis/trend_research_20261117/`（DuckDB ファイル・JSON 集計・報告書）。**本調査の出力（trend_research.duckdb と 0*_*.json）は再実行時に再作成する**。本調査の出力以外の既存ファイル（他の調査成果物・本番データ）には書き込まない。
- 実行環境: リポジトリの `.venv`（Python 3.14, duckdb のみ。pandas/numpy 不使用）。
- 制約: 既存データへの書き込みなし。外部 API 呼び出しなし。依存パッケージ追加なし。
- 比較期間: データ入手可能日とは分けて、分析入力を2026-11-17で固定する。canonicalには2026-11-18・19分も存在するが、第三者レビュー対象の期間と比較可能性を保つため今回の再集計から除外する。

## 実行順

```
.venv\Scripts\python.exe analysis\trend_research_20261117\01_data_quality.py
.venv\Scripts\python.exe analysis\trend_research_20261117\02_build_daily_panel.py
.venv\Scripts\python.exe analysis\trend_research_20261117\03_trend_analysis.py
.venv\Scripts\python.exe analysis\trend_research_20261117\04_intraday_analysis.py
.venv\Scripts\python.exe analysis\trend_research_20261117\05_walkforward.py
.venv\Scripts\python.exe analysis\trend_research_20261117\06_supplement.py
.venv\Scripts\python.exe analysis\trend_research_20261117\07_fidelity_check.py
```

## 指標定義の注意

- RSI は Cutler 方式（単純移動平均）で近似する。本番の Wilder 方式 RSI とは値が異なる（傾向分析用途のため許容。差は報告書に明記）。
- ADX 等 Wilder 平滑化が必須の指標は、同様に SMA 平滑で近似し `*_sma` と命名する。
- 特徴量は「エントリー日 t の朝時点で利用可能な情報」= 前営業日 t-1 までの確定日足（および t 日 9:00-9:29 の分足）のみから計算する。
- `01_data_quality.py` は日足・分足の重複キー、null、OHLC構造、非正価格・負の売買高/代金を確認し、分割等のイベント日には生値・調整後リターンを直接比較する。非イベント日の調整後50%超リターンも年別に監査する。
- `03_trend_analysis.py` は銘柄マスタの上場期間を日付時点で適用し、20営業日後の観測がない右打ち切り行を集計する。全コード感度分析はETF・REIT等を含み得るため、時点整合した証券種別マスタなしに純粋な普通株比較とは扱わない。
- `04_intraday_analysis.py` と `05_walkforward.py` は、未約定を評価対象外とする通常値に加え、選択された未約定分が全額損失になる極端な下方ストレスを集計する。これは実損失推定ではなく、欠損除外への感度確認である。04はV5bと現行相当の差を同日ペアで比較し、取引日クラスターブートストラップCIを出力する。
- `07_fidelity_check.py` は実運用スクリーニング監査順位・日付対応・選択後の除外条件該当数を集計する。実運用ファイルは2026-11-18までを対象とし、それより後の日付のファイルは件数を記録して分析対象から除外する。11-18のように実データはあるが固定分析締切後の結果ファイルは、休場日ファイルと分けて記録する。実運用成績は価格系列による感度分析で、往復0.21%コスト控除前後を併記する。区間推定は取引日を単位とするクラスターブートストラップ、約定可能性は9:35エントリー価格と15:25決済価格それぞれの実バー時刻で判定する。近似との一致率は共通の取引日集合で±1営業日シフトも比較し、休場日付の結果ファイルはデータ有無を列挙するが、ファイルだけで実運用実行や発注を断定しない。
