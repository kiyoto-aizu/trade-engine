# 実装プロンプト：スクリーニング価格上限フィルタ ＋ 動的ポジションサイジング

対象リポジトリ: `trade-engine`（DDDレイヤー構成: domain / application / infrastructure / entrypoints）

## 背景・目的

現状、以下2つの問題が確認されている。

1. **スクリーニング/フィルタリングに株価フィルタが無い**
   `ScreeningUseCase`は売買代金ランキングと値上がり率ランキングのみで銘柄を選定しており、
   株価そのものによる絞り込みが存在しない。売買代金ランキングは構造的に値がさ株を拾いやすく、
   現在の運用資金（1銘柄あたり予算 約3万円、100株単位）では「そもそも1単元も買えない」
   候補が大半を占める状態が実測で確認されている（10銘柄中9銘柄が0単元）。

2. **日次の予算配分が「最初に予算配分できた上位TARGET_POSITIONS件」に固定される**
   `TradingUseCase.run()`は日次実行の最初に`_allocate_filtering_candidates()`を1回だけ呼び、
   フィルタリング結果（例: 10銘柄）のうち予算配分できた上位`TARGET_POSITIONS`件（例: 3件）
   だけをその日1日の監視対象に絞り込んでしまう。残りの銘柄は一切監視されず、
   また日中にポジションを決済して枠が空いても、新規に監視対象へ追加されることがない。
   これが取引機会の減少につながっている。

この2点を修正し、以下の設計に変更する。

- スクリーニング段階で、現在の予算設定から動的に算出した株価上限であらかじめ候補を絞る
- フィルタリング結果の全銘柄（例: 10件）を1日を通して監視し続ける
- 保有ポジション数が`TARGET_POSITIONS`未満の間だけ新規買いを許可し、保有数が上限に達したら
  それ以上は買わない
- ポジションを決済して枠が空いたら、監視中の他銘柄で新規シグナルが出た時点でそのまま買える
- 新規エントリー時の1銘柄あたり予算は、保有数に関わらず常に
  `min(現在の口座残高 / TARGET_POSITIONS, MAX_ORDER_AMOUNT_PER_TRADE)`
  で計算する（＝資金効率より分散投資の均等性を優先する）

---

## タスク①: スクリーニングへの動的株価上限フィルタ追加

### 1-1. `src/config/config.py`

- 新しい設定値`SCREENING_PRICE_MARGIN`を追加する（`.env`で上書き可能、デフォルト`"0.9"`）。
  役割: スクリーニング実行日と実際の発注日にはタイムラグがあるため、
  株価変動を見込んで予算枠に安全マージンを掛ける係数。
- 以下の計算を行う関数`get_screening_price_cap()`を追加する:

```python
def get_screening_price_cap() -> float:
    """
    スクリーニング時点で候補として残す株価の上限を動的に計算する。

    budget_per_position = min(OPERATING_CAPITAL / TARGET_POSITIONS, MAX_ORDER_AMOUNT_PER_TRADE)
    price_cap = (budget_per_position / ORDER_UNIT) * SCREENING_PRICE_MARGIN
    """
```

  `OPERATING_CAPITAL`, `TARGET_POSITIONS`, `MAX_ORDER_AMOUNT_PER_TRADE`, `ORDER_UNIT`は
  いずれも既存の設定値をそのまま使う。新しい設定値は`SCREENING_PRICE_MARGIN`のみ。

- `.env.example`にも`SCREENING_PRICE_MARGIN=0.9`をコメント付きで追加する
  （既存の`MAX_ORDER_AMOUNT_PER_TRADE`等の記載スタイルに合わせる）。

### 1-2. `src/domain/rules.py`

- 純粋なドメインルールとして、以下のような関数を追加する
  （既存の`exclude_by_regulation`と同じパターンで、除外件数を返す）:

```python
def filter_candidates_by_price(candidates, price_by_symbol, price_cap):
    """
    株価が price_cap を超える銘柄、および価格が取得できない銘柄を除外する。

    Args:
        candidates: 銘柄シンボルのリスト（順序を維持する）
        price_by_symbol: symbol -> 現在値(float) の辞書。価格不明な銘柄はキーが無いか値がNone
        price_cap: この値を超える株価の銘柄を除外する

    Returns:
        exclude_by_regulationと同様の構造を持つ結果オブジェクト
        (remaining: 残った銘柄リスト, excluded_by_price_count: 上限超過での除外件数,
         excluded_missing_price_count: 価格不明での除外件数)
    """
```

  - `exclude_by_regulation`が返している結果オブジェクトの型（`ExclusionResult`等、
    `src/domain/models.py`を確認）を参考に、同じ命名規則で新しいデータクラスを追加するか、
    既存の型を拡張して良いか判断して実装すること。
  - 価格が0以下、またはNoneの銘柄は「価格不明」として除外し、`excluded_missing_price_count`
    でカウントする（安全側に倒し、価格不明な銘柄は買わない）。

### 1-3. `src/application/screening_usecase.py`

- `execute()`内、`turnover_by_symbol` / `price_gain_by_symbol`を構築した直後、
  かつ規制情報取得のバッチループ（APIコールが発生する箇所）より**前**に、
  価格上限フィルタを適用する。順序を早める理由は、以後の規制情報取得API呼び出しの回数を
  無駄に増やさないため。

- 各銘柄の現在値は`turnover_by_symbol`と`price_gain_by_symbol`の`current_price`から取得する。
  どちらか一方にしか値が無い場合はそちらを採用し、両方Noneの場合は価格不明として扱う。

- フィルタ適用後の`remaining`（除外後の候補リスト）を、以後の規制チェック・
  `limit_candidates`・監査ログ（`ScreeningAuditEntry`）に反映する。
  `ScreeningAuditEntry`に価格上限で除外されたかどうかが分かるようにしたい場合は、
  既存フィールド構成を確認した上で、無理に新フィールドを増やさず
  `restriction_reason`的な既存の仕組みで表現できないか検討すること
  （フィールド追加が必要な場合は追加してよい）。

- `_notify_completion()`のLINE通知メッセージにも、価格上限による除外件数を1行追加する
  （既存の「規制」「地方取引所」の除外件数表示と同じ形式）。

- ログにも「価格上限（xxx円）により除外: N件」を`logger.info`で出力する。

### 1-4. テスト

- 新規または既存のテストファイルに、以下を検証するテストを追加する:
  - `get_screening_price_cap()`が現在の設定値から正しい値を計算すること
    （例: OPERATING_CAPITAL=100000, TARGET_POSITIONS=3, MAX_ORDER_AMOUNT_PER_TRADE=30000,
    ORDER_UNIT=100, SCREENING_PRICE_MARGIN=0.9 のとき 270 になること）
  - `filter_candidates_by_price()`が上限超過銘柄・価格不明銘柄を正しく除外すること
  - `ScreeningUseCase.execute()`が価格フィルタを適用した上で規制チェックに進むこと
    （価格フィルタで除外された銘柄については規制情報取得APIが呼ばれないことも
    可能であれば確認する）

---

## タスク②: 動的ポジションサイジング（保有上限を都度チェック）

### 2-1. `src/application/trading_usecase.py`

- `run()`内の以下の処理を削除する:
  - `_allocate_filtering_candidates()`の呼び出し
  - その結果で`symbols`を絞り込んでいる処理
    （`symbols = [symbol for symbol in symbols if symbol in allocated_quantities]`）
  - 関連して「資金制約により発注可能な銘柄がないため、取引を開始しません」的な
    早期リターン処理も、もう使われなくなるなら削除する
  - `_allocate_filtering_candidates()`メソッド自体も、他から呼ばれなくなるなら削除してよい
    （domain層の`allocate_budget()`関数(`src/application/allocate_budget.py`)自体は
    削除しないこと。既存の`tests/test_budget_allocation.py`が直接テストしており、
    将来的な参考・レポート用途にも使える可能性があるため）

- これにより、フィルタリング結果の全銘柄（`symbols`）がそのまま1日を通して
  監視対象になる。

- 保有ポジション数をカウントするヘルパーを追加する。すでにループ内で複数箇所、
  以下のようなパターンで「保有中か」を判定している箇所があるので、それを再利用可能な
  形に共通化すること:

```python
position.get('Side') == config.OrderSide.SELL.value and int(position.get('HoldQty', 0) or 0) > 0
```

  例:
```python
def _count_open_positions(self, positions) -> int:
    """信用売り建て決済待ち＝保有中とみなすポジション数を数える。"""
```

- BUYシグナル発生時（現在`signal.qty = allocated_quantities.get(symbol, calculate_buy_quantity(...))`
  となっている箇所）を、以下のロジックに置き換える:

  1. `wallet_amount, positions = self._load_account_state()`はすでに直前で呼ばれているので、
     それを使って現在の保有ポジション数を`_count_open_positions(positions)`で取得する
  2. その銘柄が**すでに保有中でない**場合に限り、保有数が`config.TARGET_POSITIONS`以上なら
     このBUYシグナルは見送る（`continue`）。ログに
     `"保有上限のため新規買いを見送ります: 銘柄=%s | 保有数=%s/%s"`のように出力する
  3. 見送りにならない場合、1銘柄あたり予算を次のように計算する:
     ```python
     budget_per_position = min(
         wallet_amount / config.TARGET_POSITIONS,
         config.MAX_ORDER_AMOUNT_PER_TRADE,
         self.api_soft_limit,
     )
     signal.qty = calculate_buy_quantity(signal.price, budget_per_position, config.ORDER_UNIT)
     ```
     （`api_soft_limit`を上限に含める点は、既存の
     `min(config.MAX_ORDER_AMOUNT_PER_TRADE, self.api_soft_limit)`という考え方を踏襲する）
  4. これ以降のATRボラティリティ調整（`adjust_quantity_for_volatility`）の呼び出しは
     変更しない。既存通り、上で計算した数量に対してさらに調整をかける。

- `top_symbols_path`を使う古い実行経路（`preflight_market_data`を渡さない呼び出し）も
  同じ新ロジックを通るようにする。この経路専用の分岐が残っている場合は、
  可能な限り一本化すること。

### 2-2. テスト（`tests/test_trading_bot.py`）

以下の既存テストへの影響を確認し、必要に応じて更新する:
- `_allocate_filtering_candidates`や`allocated_quantities`の挙動に依存しているテスト
- `preflight_market_data`ありのケースで「銘柄が3件に絞り込まれる」ことを前提にしているテスト

新規テストとして、以下を追加する:
1. 保有数が`TARGET_POSITIONS`未満のとき、新規BUYの数量が
   `min(wallet_amount/TARGET_POSITIONS, MAX_ORDER_AMOUNT_PER_TRADE)`から
   正しく計算されること
2. 保有数がすでに`TARGET_POSITIONS`に達しているとき、新規銘柄のBUYシグナルが出ても
   発注されない（見送りログが出る）こと。ただし**保有中の銘柄自身の追加シグナル**や
   SELL（決済）シグナルは影響を受けないこと
3. フィルタリング結果が10件ある場合、`run()`実行中は10件全てが監視対象になり続けること
   （日次の最初の時点で3件に絞り込まれないこと）
4. （可能であれば）1件を決済して保有数が減った後、次のループ周回で別の監視銘柄への
   新規エントリーが行われること。ループを複数回まわすモックの組み方は既存テストの
   スタイルに合わせること

---

## 共通の注意事項

- `docs/architecture/coding-guidelines.md`のレイヤー依存ルールに従うこと
  （domain層は純粋ロジックのみ、application層がdomain層を呼び出す、infrastructure層への
  依存をdomain層に持ち込まない）
- 既存の日本語コメント・ログメッセージのスタイル（変数名は英語、ログ文言は日本語）を踏襲すること
- 実装後、`pytest`をフルスイートで実行し、全て成功することを確認すること
- 変更点のまとめを最後に日本語で簡潔に報告すること（変更ファイル一覧、削除した関数、
  追加した設定値、テスト結果）
