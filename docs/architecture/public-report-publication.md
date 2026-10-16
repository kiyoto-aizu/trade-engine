# 日次公開レポート

日次分析の最後に、公開専用の `kiyoto-aizu/trade-report` へ安全なサマリーだけをpushし、GitHub PagesがURLで応答することを確認してからSlackへURLを通知する。取引システム本体のリポジトリはprivateのままであり、日報そのものや履歴を公開先へコピーしない。

## 公開するデータ

公開JSONは `src/infrastructure/report_publication.py` の `public_report_payload()` が作る許可リストであり、元の日報をそのまま出力してはならない。

| 項目 | 用途 |
| --- | --- |
| `date`、`published_at` | 対象日と公開生成日時 |
| `trading_mode` | ペーパー/本番の区分 |
| `order_count` | 当日の約定件数 |
| `market_status`、`market_regime` | 市場データの可用性と市場レジーム |
| `candidate_count`、`evaluated_count`、`buy_signal_count` | 銘柄を特定しない集計件数 |
| `no_trade_reason` | 売買なしの理由コードのみ |
| `skip_counts` | 見送り理由別の集計件数 |
| `charts` | 公開する分足診断画像のファイル名一覧 |

分足診断画像は、2026-10-16 に所有者が「銘柄コード・見送り判定・価格推移・損益表示が公開されてよい」と承認した。公開対象は `intraday_trade_diagnostics.png`（全体図）と `intraday_trade_diagnostics_detail_<銘柄>.png`（詳細図）のpngだけで、ファイル名の形式を検証する。同じ場所にある `.json`・`.csv`・`.md` は内部の補完状況や詳細データを含むため公開しない。画像に口座情報・パス・ログ文言が写らないことは確認済みだが、図の表示内容を変更する場合は公開前に再確認する。

次の情報はJSON、HTML、コミットメッセージに入れてはならない: APIキー、Webhook URL、認証情報、口座残高、注文ID、保有数量、取得単価、損益額、注文/保有明細、ローカルパス、localhost URL、ログ、内部エラー文、元レポート本文、LLM出力。公開リポジトリは履歴を含めて誰でも取得できるため、誤ってpushした情報は削除コミットだけでは回収できない。

## 公開先の構成

公開先のルートは次の固定構成である。`index.html` は日次公開のたびに、このリポジトリの内容へ揃えて更新する（変更がなければコミットされない）。日次で内容が増えるのは `data/` 配下だけである。

```text
trade-report/
├─ index.html
└─ data/
   ├─ index.json
   └─ YYYY-MM-DD.json
```

分足診断画像は `charts/YYYY-MM-DD/` にも置く。`index.json` は日付の降順一覧、日別JSONは上表の許可項目だけを持つ。画面は月別カレンダーで日付を選び、概要・件数の棒グラフ・直近30日の推移グラフ・分足診断画像を表示する。グラフは公開済みの集計値だけから描画する。

## 専用トークンとローカル設定

GitHubでfine-grained personal access tokenを新規作成する。

1. GitHub右上のプロフィールから **Settings** を開く。
2. **Developer settings** → **Personal access tokens** → **Fine-grained tokens** → **Generate new token** の順に開く。
3. Resource ownerを `kiyoto-aizu`、Repository accessを **Only select repositories** にし、`trade-report` だけを選ぶ。
4. Repository permissionsの **Contents** を **Read and write** にする。MetadataのReadは自動的に必要となる。Pages設定をAPIで変更しないため、Pages権限は不要である。
5. 短い有効期限を指定して発行し、その値をprivateリポジトリ直下の `.env` にのみ保存する。`.env` はGit無視済みで、トークン値をチャット、Slack、ログ、コミットに出力しない。

`.env.example` を参考に、実際の `.env` に次を設定する。

```dotenv
TRADE_REPORT_PUBLISH_ENABLED=true
TRADE_REPORT_GITHUB_TOKEN=<fine-grained-token>
TRADE_REPORT_REPOSITORY=kiyoto-aizu/trade-report
TRADE_REPORT_PUBLIC_URL=https://kiyoto-aizu.github.io/trade-report/
TRADE_REPORT_PUBLISH_RETRY_COUNT=12
TRADE_REPORT_PUBLISH_RETRY_WAIT_SECONDS=10
```

実装は公開先を `kiyoto-aizu/trade-report` に固定で検証する。別リポジトリが設定されている場合はpushしない。

## GitHub Pages設定（かいが行う手順）

`https://github.com/kiyoto-aizu/trade-report` を開き、次の画面順で設定する。

1. リポジトリの **Settings** タブを開く。
2. 左サイドバーの **Pages** を開く。
3. **Build and deployment** の **Source** で **Deploy from a branch** を選ぶ。
4. **Branch** のドロップダウンで `main`、フォルダで `/(root)` を選ぶ。
5. **Save** を押す。
6. 初回のpush後、同じ画面の **Your site is live at** に `https://kiyoto-aizu.github.io/trade-report/` が表示されることを確認する。

設定済みかは、Pages画面でSourceが `Deploy from a branch`、Branchが `main / (root)` と表示されること、さらに上記URLをブラウザで開いてHTTP 200で画面が表示されることの両方で確認する。設定前はAPIによるPages設定取得が404になることがある。

## 日次の失敗時

日次分析は公開失敗で停止しない。push、Pagesの公開待機（既定12回、10秒間隔）、またはトークン検証が失敗しても、最後のSlack分析通知に `公開: 失敗（詳細は内部ログを確認）` の1行だけを追加する。成功時はPages URLを同じ通知の最後に追加するため、404になる前のURL通知を防ぐ。
