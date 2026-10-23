# trade-engine コーディング規約・設計方針 第3版

更新日: 2026-10-04

このドキュメントは、trade-engine でコードを書く／レビューする際に従うべきルールをまとめたものです。
AIにコードを書かせる・レビューさせる際は、このファイルをコンテキストとして読み込ませてください。
（旧実装のレビュー記録は [review-legacy-code.md](../reviews/review-legacy-code.md) を参照してください）

---

## 1. レイヤー構成

```
trade-engine/
├── src/
│   ├── api/                        # request_handler.py: infrastructure専用HTTP共通処理
│   ├── application/                # usecase・分析/配分機能
│   │   ├── price_band_filtering_usecase.py # 価格帯別フィルタ実行制御
│   │   └── midday_filtering_usecase.py # 昼のYahoo分足推定フィルタ(450/900円帯)
│   ├── config/                     # config.py, task_schedule.py
│   ├── domain/                     # enums, models, rules, ATR/市場分析, minute_turnover_estimation(分足の売買代金推定)
│   ├── entrypoints/                # 現行CLI群（下記参照）
│   ├── executor/
│   ├── filter_dynamic/
│   ├── infrastructure/
│   │   ├── analysis/  ├── backtest/  ├── calendar/
│   │   ├── kabu/          # registration_aware_board_cache.py: 登録枠・並列取得を管理する板キャッシュ
│   │   ├── market_data/   # cached_volume_client.py: 出来高平均キャッシュ
│   │   ├── notification/  ├── paper/  └── persistence/
│   ├── sample/
│   ├── screening/
│   └── trading/
├── tests/                          # 現状は各test_*.pyを直下に配置
├── docs/
└── scripts/
```

`main.py`はリポジトリ直下にも`src/`直下にも存在しない。`tests/`も現状は平置きで、`tests/domain/`等のサブディレクトリはない。

### 現行entrypoints一覧

```text
backtest_v2_multi_day_check.py    backtest_v2_single_day_check.py
run_adx_analysis.py               run_atr_ratio_analysis.py
run_backtest.py                   run_daily_diary.py
run_daily_task_check.py           run_daily_task_plan.py
run_filtering.py                  run_filtering_override.py
run_midday_filtering.py           
run_market_regime.py              run_market_volatility_analysis.py
run_minute_backfill.py            run_monthly_analysis.py
run_screening.py                  run_strategy_review.py
run_trading.py                    run_vix_analysis.py
run_weekly_analysis.py            update_daily_bar_cache.py
```

### 1.1 各層の責務と依存方向
- **依存の向きは一方向**：`src.entrypoints → src.application → src.domain` / `src.application → src.infrastructure`。
- `domain` は外部依存を持たず、ビジネスルールと判定ロジックだけを表現する。
- `application` はユースケースの司令塔で、`domain` のロジックと `infrastructure` の入出力をつなぐ。
- `infrastructure` は外部API・DB・通知・永続化の具体実装のみを担当し、ロジックは持たない。
- `entrypoints` は起動時の「初期化」と「ユースケース呼び出し」だけを担当する。
- `src/api/request_handler.py`はHTTP共通ハンドラで、`infrastructure/kabu`および`infrastructure/market_data`から利用する。`application` / `domain`から直接利用しない。ハンドラ自身は`config`にのみ依存する。将来、`infrastructure`配下へ移す案は選択肢として残す。

```python
# src/domain/enums.py
from enum import Enum

class OrderSide(str, Enum):
    BUY = "2"
    SELL = "1"

# src/application/evaluate_symbol_usecase.py
class EvaluateSymbolUseCase:
    def __init__(self, board_repo, order_repo, safety_checker):
        self._board_repo = board_repo
        self._order_repo = order_repo
        self._safety_checker = safety_checker

    def execute(self, symbol: str, limits: PriceLimit) -> Optional[TradeSignal]:
        board = self._board_repo.get_current_board(symbol)
        if board is None or board.current_price is None:
            # データが取れない場合は評価をスキップする。価格を推測・捏造しない。
            return None
        ...
```

---

## 2. 命名規約

### 2.1 ファイル・モジュール
- ファイル名と `import` 時のモジュール名は必ず一致させる。
- 責務が「取得」なら `get_xxx.py`、「送信・実行」なら `send_xxx.py` / `place_xxx.py`、「判定」なら `decide_xxx.py` のように動詞プレフィックスで統一する。
- 中身から役割が読み取れない汎用的な名前（`component`, `executor`, `filter_dynamic` など）は避け、扱う対象・レイヤーが分かる名前にする。
- `entrypoints/` 以下は「起動トリガー」を表す名前にする。例: `run_trading.py`, `run_screening.py`。

### 2.2 クラス
- 1クラス1責務。役割を表す接尾辞で統一する。
  - `Repository`: 永続化・外部データ取得
  - `Client`: 外部API通信
  - `UseCase`: 業務フロー
  - `Service`: 横断的な処理
- 実態と乖離した名前（例: 移動平均計算なのに「Ai」と付ける）は避け、処理内容に即した名前にする。

### 2.3 変数・定数
- 意味のある値（発注方向など）は文字列直書きせず `Enum` にする。
- 定数は `UPPER_SNAKE_CASE`、意味単位でグループ化する。
- 真偽値フラグは `is_` / `has_` プレフィックスで統一する。

### 2.4 関数
- 「取得して判定して発注する」のような複合動詞を避け、1関数1動作にする。
- 副作用（ファイル書き込み・API呼び出し・print）を持つ関数と、純粋な計算関数を名前で区別できるようにする。
  - 計算のみ: `calculate_xxx`
  - 外部影響あり: `execute_xxx`, `send_xxx`, `persist_xxx`, `notify_xxx`

---

## 3. セキュリティ観点

### 3.1 秘密情報管理
- `.env` は必ず `.gitignore` に含める。公開リポジトリの場合はコミット履歴に過去のシークレットが残っていないかも確認する。
- 必須環境変数（APIパスワード、通知トークン等）は、値が空でもデフォルト値で起動を許容せず、欠けている場合は起動時に例外で停止する。
- ログ・例外メッセージにトークンやパスワードを含めない。

### 3.2 本番/デモ切り替えの安全化
- 環境切り替えは1つのフラグだけで実弾発注に到達しない設計にする。
- 起動引数や環境変数の二重チェックを要求する。
- 発注数量の上限・1日の最大発注回数・想定損失額の上限（キルスイッチ）を設定し、超えたら自動停止する。

### 3.3 外部API呼び出しの安全性
- 外部データ取得に失敗した場合、フォールバックとして推測・乱数生成した値を使わない。
- 非公式APIやレート制限のあるAPIを使う場合は、タイムアウト・リトライ間隔・サーキットブレーカーを設ける。
- 注文履歴データが破損している場合は、空初期化でサイレントに継続せず、明示的な警告または起動停止で気づける形にする。

### 3.4 監査ログ
- 「いつ・どの銘柄を・いくらで・何を根拠に」発注したかを、判定に使った数値とともにログに残す。
- 標準出力だけに頼らず、ログファイル／監査ログに記録する。

### 3.5 既知の乖離（このタスクでは解消しない）

以下は現行コードで確認した規約との乖離であり、この規約更新ではコードを変更しない。

| 現状 | 規約上の望ましい形 |
|---|---|
| `TradingUseCase._load_order_history()` / `_save_order_history()`が`ORDER_HISTORY_FILE`をUseCase内で直接読み書きする | 注文履歴の永続化をinfrastructureのRepositoryへ移す |
| `src/application/trading_usecase.py`は2,069行、`TradingUseCase.run()`は717行 | UseCaseを責務単位に分割し、ループ制御・判断・通知を分離する |
| `src/application/backtest_usecase.py`は1,320行 | シミュレーション処理を責務単位に分割する |

### 3.6 テスト配置の現状と将来方針

- 現状の`tests/`は平置きで、`tests/test_*.py`に配置する。`tests/domain/`・`tests/application/`等のサブディレクトリ分割はしていない。
- 将来の方針: テスト数・保守性の課題が明確になった段階で、責務別サブディレクトリへの移行を検討する。

---

## 4. テスト観点

### 4.1 基本方針
- `domain` 層（シグナル判定・安全性チェックのロジック）は外部APIから完全に切り離し、ネットワークなしでテストできる状態を保つ。
- `infrastructure` 層は repository/client のインターフェースを介して呼び出せるようにし、テスト時にモックへ差し替え可能にする。
- `pytest --cov=src --cov-branch --cov-fail-under=80` をCIおよび変更前の確認コマンドとし、全体カバレッジ80%未満の変更はマージしない。
- 現状値（2026-10-04実測）: branch coverage **82%**、全517テスト通過。下位モジュールは`entrypoints/run_backtest.py` 29%、`entrypoints/backtest_v2_multi_day_check.py` 34%、`entrypoints/update_daily_bar_cache.py` 48%、`infrastructure/persistence/backtest_input_repository.py` 59%、`infrastructure/market_data/yahoo_backtest_history_client.py` 62%。全体基準を満たしていても、これらを変更する場合は該当経路のテストを追加する。
- 外部API・通知・永続化は実ネットワークへ接続せず、成功応答、タイムアウト・HTTPエラー、欠損または不正な応答をモックした契約テストで検証する。

### 4.2 テストすべき観点

**シグナル判定ロジック**
- 境界値テスト（閾値ちょうど、わずかに上、わずかに下）
- 入力データが不足しているときにスキップされること
- 閾値計算（移動平均など）が正しいこと

**安全性チェック**
- 予算不足時に発注が拒否されること
- 同一銘柄・同一方向の当日注文がある場合に拒否されること
- 二重発注防止のロック時間の境界値
- 保有がない銘柄への売り注文が拒否されること

**永続化**
- 初回起動（ファイル不存在）時の挙動
- データ破損時の挙動（サイレントに空初期化しない）
- 複数回の記録が正しく積み上がること

**異常系**
- 外部APIがエラー・タイムアウトを返したときに、後続の注文ロジックへ進まないこと
- 複数の外部APIのうち一部が落ちても、他の処理に影響しないこと

### 4.3 テストの種類
- **ユニットテスト**: `domain` / `application` 層はモックのみで完結させる。
- **契約テスト**: `infrastructure` 層は外部APIのレスポンス形式に依存するため、サンプルレスポンスを使ったパース処理のテストを用意する。
- **結合テスト**: デモ環境を使って、起動〜1サイクル評価〜レポート送信までを通しで動かす。

---

## 5. 起動契機とエントリポイント（第3版）

- 起動契機が異なる場合、エントリポイントを分けるのは許容される。
- ただし、エントリポイントは原則として初期化・引数解析・依存構築・UseCase呼び出しにとどめる。
- 実ビジネスロジックは `application` / `domain` に置き、`entrypoints/` は依存関係の組み立てとユースケース呼び出しだけを行う。
- `src/config/task_schedule.py`は日付ごとの予定表を返すが、OSスケジューラやcronの代わりにプロセスを起動するものではない。

### 5.2 `entrypoints` の責務
- 設定読み込み
- ロギング初期化
- 依存オブジェクトの生成
- ユースケースの呼び出し
- CLIの`main()`と`if __name__ == '__main__'`を持つ場合がある。例外表の確定方針と期限に従う。

### 5.3 `entrypoints` を分けるべきケース
- スケジュール実行と手動実行で起動条件が異なる場合
- サービス起動とバッチ実行で初期化プロセスが異なる場合
- Webhook など、外部トリガーが専用起動フローを必要とする場合

### 現状の例外（行数実測 2026-10-04）

| ファイル | 行数 | 方針 | 薄くない処理・移設/終了時の対応 |
|---|---:|---|---|
| `backtest_v2_single_day_check.py` | 310行 | **期限つき例外** | 履歴入力・疑似時計準備、単日疑似実行、性能比較を行う検証CLI。ADR-0007 Phase 5で正式バックテストエンジンへ昇格する際にapplicationへ移す。 |
| `backtest_v2_multi_day_check.py` | 505行 | **期限つき例外** | 日付・データ充足探索、複数日疑似実行、比較集計、scratch出力を行う検証CLI。ADR-0007 Phase 5で正式バックテストエンジンへ昇格する際にapplicationへ移す。 |
| `run_backtest.py` | 199行 | **例外（廃止予定）** | 旧エンジンのCLI。ADR-0007 Phase 6の旧エンジン廃止まで現状維持し、applicationへの移設は行わない。 |

- 解消（2026-10-04）: `run_filtering.py`の板/出来高キャッシュをinfrastructureへ、価格帯別フィルタ実行制御を`PriceBandFilteringUseCase`へ移設した。`run_filtering_override.py`のentrypoint間importも解消し、両entrypointは`BoardRepository`・共通ロギング・`notify_daily`を直接利用する。
- 更新（2026-10-06）: 通常・価格帯別の対象収集と逐次評価は`PriceBandFilteringUseCase`、並列板取得・同一銘柄の要求統合・登録解除待ちは`RegistrationAwareBoardCache`が担当する。判定条件は変更しない。

`tests/`は現状平置きである。`tests/domain/`等への分割は将来の方針として検討する。

---

## 6. 移行の進め方

1. 危険なフォールバック処理（推測値での代替）を除去する
2. 設定読み込み時の副作用（print等）を除去し、必須環境変数のfail-fast化を行う
3. マジックストリングを `Enum` 化する
4. 既存の外部連携コードを `infrastructure` 配下に整理する
5. 判定・安全性チェックのロジックを `domain` 層に純粋関数として抽出する
6. `domain` / `application` にユニットテストを追加する
7. 司令塔クラスを `application` 層のユースケースに分割する

---

## 7. 変更履歴

- 第3版（2026-10-04）: 構成図を実ディレクトリ・entrypointsに更新し、存在しない`main.py`等を除去。
- 第3版（2026-10-04）: `src/api`の位置づけ、entrypoint例外表、既知の責務・永続化乖離を追加。
- 第3版（2026-10-04）: テスト配置・coverageの現状値と将来方針を追記。
- 第3版（2026-10-04追記）: entrypoint例外の移設方針・期限を確定し、ADR-0007 Phase 5/6と整合。
- 第3版（2026-10-09追記）: 昼フィルタ(`run_midday_filtering.py` → `application/midday_filtering_usecase.py` → `domain/minute_turnover_estimation.py`)を追加。診断スクリプトと共有する推定ロジックはdomainへ移設し、Yahoo取得失敗の区別は`infrastructure/market_data/get_intraday_bars.py`の`fetch_yahoo_intraday_bars`が担う。
