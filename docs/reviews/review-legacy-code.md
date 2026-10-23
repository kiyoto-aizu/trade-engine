# trade-engine 旧実装のコードレビュー記録

対象: `trading_bot.py` / `component/get_api_5d_closes.py` / `config.py` を中心にしたレビュー。
このファイルは旧実装を対象にした過去のレビュー記録です。現行コードの状態を示すものではありません。コーディング規約は [coding-guidelines.md](../architecture/coding-guidelines.md) を参照してください。

---

## 1. 良い点
- `OrderHistoryManager` / `AccountManager` / `AiLimitManager` / `NotificationService` のようにクラスで責務を分けようとする意識は既にある。
- 注文の二重発防止（`has_ordered_today` / `is_recent_order`）や、買付余力チェックなど、安全側の考慮が入っている。
- `.env` によるシークレット管理を導入しようとしている（`config.py` の dotenv 読み込み）。

## 2. 重大な問題点（優先度：高）

| # | 箇所 | 問題 | リスク |
|---|---|---|---|
| 1 | `evaluate_symbol()` | 板情報取得失敗時に `random.uniform()` で価格を捏造し、そのままシグナル判定・発注ロジックに流している | **本番運用時にAPI障害＝ランダム価格で実発注**という致命的な事故につながる |
| 2 | `TradingBot` クラス | 初期化・ループ制御・シグナル判定・安全チェック・発注・レポート送信を1クラスに集約（God Object化） | 変更の影響範囲が読めない、テストが書けない |
| 3 | `config.py` | import時に `print()` が実行される（副作用）。`IS_DEMO` の切り替えだけで本番発注が可能になり、明示的な確認ステップがない | 誤って本番モードで起動するリスク |
| 4 | 発注方向 | `'1'`（売）/`'2'`（買）がマジックストリングとして各所に散在 | typoで売買が逆転しても気づけない |
| 5 | ファイル名と import 名の不一致 | `get_api_5d_closes.py` を `from component import get_5d_closes` として import | 別の開発者・AIが読んだときに混乱、リファクタ時の事故要因 |
| 6 | 例外処理 | `except Exception: pass` 相当の握りつぶしが複数箇所（`OrderHistoryManager.load`、`get_yahoo_5d_closes` 等） | 障害が起きても気づけずサイレントに壊れた状態で稼働し続ける |

## 3. 中程度の問題点
- ドメインロジック（買い/売り判定）とインフラ処理（API呼び出し、通知）が同じメソッド内に混在している。
- ロギングが `print()` ベースで、レベル分け・永続化（監査ログ）がない。
- リトライ・バックオフの仕組みがない（Yahoo Finance / kabu API / LINE通知いずれも一発失敗で終わり）。
- `AiLimitManager` という名前だが、中身は単純な5日移動平均で「AI」ではない。命名が実態と乖離している。

## 4. 現状のディレクトリ構成の確認

実際の `src/` 構成（エクスプローラーより）:

```
src/
├── api/            request_handler.py
├── common/         storage.py
├── component/      get_api_5d_closes.py / get_board.py / get_positions.py /
│                   get_token.py / get_wallet.py / line_notify.py / send_order.py
├── filter_dynamic/ filter_dynamic.py
├── screening/      screening.py
├── main.py
└── trading_bot.py
```

提案しているレイヤー（domain / application / infrastructure）と照らした評価:

| 現状のフォルダ | 相当するレイヤー | コメント |
|---|---|---|
| `api/request_handler.py` | infrastructure（共通基盤） | 位置づけは自然 |
| `component/*` | infrastructure | 外部API連携の集約は良いが「component」という名前からは中身が読めない |
| `common/storage.py` | infrastructure/persistence | `OrderHistoryManager` の保存先として妥当な位置 |
| `config/config.py` | config | 妥当 |
| `filter_dynamic/filter_dynamic.py` | 恐らく domain | 名前からは「価格閾値判定」か「銘柄フィルタ」か判別できない |
| `screening/screening.py` | 恐らく application/domain | `filter_dynamic` との境界が外から見えない |
| `main.py` と `trading_bot.py` | ── | エントリーポイントが2つあるように見える。どちらが実際の起動点か要確認 |

**未確認のためコメントできない点**: `filter_dynamic.py` / `screening.py` / `main.py` / `get_token.py` / `storage.py` の中身を見ていないため、domain / application の境界線がどこにあるかは推測段階。中身を確認できれば、より正確なレイヤーマッピングを更新する。

## 5. 未確定・要ヒアリング事項
- 発注数量は買い注文が価格に応じた最大数量（`ORDER_UNIT`の整数倍）、売り注文が保有数量全量。固定100株ではない。
- kabu API（kabuステーション）のトークン取得・更新フローは `get_token.py` にあると思われるが未確認。
- 損失上限・キルスイッチのような「止める仕組み」は現状ゼロなので、優先度をどこに置くか。
- `main.py` と `trading_bot.py` の役割分担（どちらが正のエントリーポイントか）。
