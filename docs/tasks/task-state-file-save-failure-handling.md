# 改修タスク: 状態ファイルの破損・保存失敗対応

- 起票日: 2026-10-04
- 対象: `src/infrastructure/paper/paper_order_client.py`、`src/infrastructure/persistence/storage.py`、注文履歴読み書き、関連テスト
- ステータス: **実装済み・コミット済み（`79b001e`、2026-10-04）**。キルスイッチ基準・日次レポートの保存失敗は2026-10-12に対応済み
- 実施時期: **2026-10-04**
- 関連調査: 2026-10-02「売買部分サイレントスキップ横並び調査」。第1弾は取引判断理由を`DecisionJournalRepository.TRADING_REASON_CODES`等へ記録したが、状態ファイル読込/保存失敗の専用記録はなく、共通JSON層は一般ログだけを出す。

## 未対応範囲

2026-10-12に次の2点を対応済み。
- キルスイッチ基準資本: 保存失敗時はメモリ上の基準を使い続け、未保存として保持する。保存対象`kill_switch_baseline`として既存の連続失敗カウント・初回critical通知・閾値での新規買い停止(売り・決済は継続)に載せ、取引ループごとに再保存して成功で復帰する。
- 日次レポート: `mkdir`の`OSError`と`write_json`失敗を処理し、1回再試行後も失敗なら`report_text`をcriticalログに残しSlack通知する。取引結果通知は必ず送り、「日次レポート保存失敗」を添える。

残る未対応(今回は変更しない):
- 起動時のキルスイッチ基準ファイルは`read_json()`(非strict)で読むため、破損していても停止・破損コピー保存はされず、基準がその時点の資産で作り直されて上書きされる。他の状態ファイルのような`StateFileCorruptError`扱いにはなっていない。
- `run_daily_analysis`は日次レポート欠落日を警告ログ+「日次レポートなし」の理由で扱い落ちないが、レポートJSONが破損している場合は`json.loads`の例外で落ちる。
- `write_json`の戻り値を見ていない他の呼び出し元: 中=`price_band_trend_check._write_status`(状態欠落で再判定・重複通知の恐れ、mkdirの`OSError`も未処理)、`run_weekly_analysis`・`run_monthly_analysis`(出力JSON欠落、通知は詳細パスを案内するため実在しない恐れ)。小=`error_analyzer._persist`(重複抑止状態の欠落のみ)、`run_daily_diary`(context欠落)、`backtest_v2_multi_day_check`(検証用の出力)。大に該当するものはない。

## 背景

ペーパー口座の状態は`paper_account_state.json`の`cash`、`holdings`、`average_costs`、`next_order_id`、`realized_pnl`、`realized_pnl_date`等に保存される。読込失敗や保存失敗が注文処理の成否と一致せず、メモリ上の状態と永続状態が食い違う可能性がある。

## 現行コードで確認した挙動

| ケース | 実装・位置 | 現行挙動 |
|---|---|---|
| 状態ファイルなし | `PaperOrderClient.__post_init__()`（`src/infrastructure/paper/paper_order_client.py:31`）、`read_json()`（`src/infrastructure/persistence/storage.py:32`） | `FileNotFoundError`は`None`として返り、PaperOrderClientは初期値で続行する。これは初回起動として意図された経路。 |
| JSON構文破損 | `read_json()`（`storage.py:50`）→`__post_init__()` | `JSONDecodeError`をログに記録して`None`を返す。PaperOrderClientはdictでないとしてreturnし、空の初期状態で続行する。 |
| JSONのrootがdictでない | `PaperOrderClient.__post_init__()`（`paper_order_client.py:34`付近） | `read_json()`の返値をdict確認してreturnし、警告なしで初期状態を使う。 |
| dict内の型・値が不正 | `PaperOrderClient.__post_init__()`（`paper_order_client.py:37-52`） | `float()` / `int()` / `.items()`等の変換で例外になり得る。個々の不正フィールドの継続・停止動作は入力形状ごとの確認が必要。 |
| 状態ファイル保存時の`OSError` | `PaperOrderClient._save_state()`（`paper_order_client.py:56`）→`write_json()`（`storage.py:14,28`） | `write_json()`が例外をログに記録して握りつぶす。PaperOrderClientは呼出し元へ保存失敗を返さず、注文結果dictを成功として返し得る。ファイルは`open(..., 'w')`で直接上書きし、一時ファイル置換ではない。 |
| 注文履歴のJSON構文/読込エラー | `TradingUseCase._load_order_history()`（`src/application/trading_usecase.py:473`） | `OSError` / `JSONDecodeError`を`ValueError`へ変換してraiseし、`run()`開始を止める。 |
| 注文履歴保存時の`OSError` | `_register_order()`（`trading_usecase.py:597`）→`_save_order_history()`（同484）→同じ`write_json()` | 共通関数が`OSError`を握りつぶすため、注文履歴ファイルの保存失敗も呼出し元には通知されず、注文処理が成功扱いで進み得る。 |

既存`tests/test_paper_order_client.py`は通常の永続化・再起動を確認する。`tests/test_trading_bot.py::test_order_history_load_corrupt`は注文履歴の破損時に例外となることを確認する。一方、Paper口座状態の破損・読込I/O失敗・保存`OSError`の失敗伝播は確認できない。

同一プロセス内では`_register_order()`が`self.order_history`へappendしてから保存を呼び、`is_safe_to_order()`は同じリストを重複/時間ロック判定に使う。このため、保存失敗後もプロセスが生きている間は直前の注文を使ったメモリ上の重複判定が効く。一方、再起動後はディスクに残った古い履歴しか復元できず、その注文に対するガードは失われ得る。

## 必須対応

- 状態ファイルの破損・読込失敗・保存失敗を検出し、ログと運用通知等の定めた経路で識別可能にする。
- 永続化が失敗した状態を、注文・状態更新が正常に保存されたものとして成功扱いしない。残高・保有・平均取得価格・注文記録のメモリ状態とディスク状態の不整合を検出できるようにする。
- 初回起動の「ファイルなし」と、既存ファイルの破損・アクセス失敗を区別する。
- `order_history`の破損時は現状`ValueError`で停止する一方、paper状態のJSON構文破損は`read_json()`が`None`に変換して初期値で続行する。この非対称性を解消する方針を決め、同じ障害を意図した扱いにする。

## 決定事項

1. **起動時に既存状態ファイルの破損・読込失敗を検出したら停止する**: 初期値で黙って続行しない。注文履歴破損時の`ValueError`停止と同じ安全側に揃える。破損ファイルは別名で残してからSlack通知する。ファイル名は`.corrupt`付き等を候補とし、具体名は実装時に確定する。ファイル不存在は初回起動として扱い、破損と区別する。
2. **実行中の保存失敗はcritical通知後に処理を続行する**: 失敗を成功扱いしない状態/注文履歴として記録し、保存は次の状態保存機会に再試行する。1回目の失敗でcritical通知を出す。
3. **保存失敗が連続したら新規買いだけ停止する**: 売り・決済は止めず、15:20の持ち越し防止決済を妨げない。連続回数は3回を初期候補として実装時に決める。停止理由をログ・判断記録へ残す。
4. `write_json()`は状態ファイルと注文履歴の双方で使われ、現状`OSError`を握りつぶす。新仕様では両方の保存失敗を成功扱いしない。Paper口座状態はペーパー経路のみだが、注文履歴は本番/ペーパー共通である。

### 確認事項

- **同一プロセス内の重複判定**: 現行は保存失敗しても`_register_order()`でメモリ上の`self.order_history`へ先に追加し、`is_safe_to_order()`が同リストを参照するため、プロセス稼働中は重複判定が効く。再起動後は保存失敗した注文がファイルにないため、ガードは失われ得る。
- **ペーパー/本番の差**: `run_trading.py`はpaper時のみ`PaperOrderClient(state_path=...)`を渡す（`src/entrypoints/run_trading.py:76-80`）。liveでは`TradingUseCase._load_account_state()`がwallet/positions clientまたはkabu APIの`get_wallet_cash()` / `get_positions()`へフォールバックする（`src/application/trading_usecase.py:731-751`）ため、Paper口座状態ファイルの障害は直接影響しない。一方、注文履歴ファイルと共通`write_json()`の失敗は本番/ペーパー双方に影響する。新規買い停止等の保存失敗カウンタをどの保存対象・実行モードへ適用するかは実装時に確認する。

## 売買結果への影響

**変わりうる。** 破損状態を初期値扱いしていた運用を停止または隔離へ変えると、保有・買付余力の復元や新規注文が変わる。保存失敗を成功扱いしない場合も実行後の状態と約定数に影響し、**40件カウントの起点**に影響する。障害時の除外件数・対象期間を記録する。

## 完了条件

- 起動時に破損JSONを読むと起動が停止し、元ファイルが別名で残り、Slack通知が出る。ファイル不存在の初回起動は通常どおり通る。
- 状態/注文履歴保存時に`OSError`を模擬するとcritical通知が出て処理は継続し、保存は次の機会に再試行される。
- 連続失敗が実装で定めた回数に達すると新規買いは止まり、売り・15:20決済は実行される。
- JSON root不正、値/型不正、読込`OSError`、状態ファイルと注文履歴ファイルの各保存失敗を区別してテストする。
- 注文履歴の既存破損時挙動との整合性をテストする。
- 同一プロセス内の注文履歴メモリ判定と、再起動後に保存されなかった履歴がない状態をテストする。
- `state_path=None`のヒストリカル再生にファイル障害ポリシーが誤適用されないことを確認する。
- 実kabu注文を行うテストは作成しない。

## 関連資料

- [ADR-0007](../adr/0007-unify-trading-and-backtest.md)（PaperOrderClientのヒストリカル再利用）
- [本番EOD決済・約定価格記録](./task-live-eod-liquidation-and-fill-reconciliation.md)（本番移行前の別タスク）