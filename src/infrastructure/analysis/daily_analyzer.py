import json
import logging

import requests

from src.config import config

logger = logging.getLogger(__name__)

STRATEGY_REVIEW_SYSTEM_PROMPT = """あなたは日本株自動売買システムの戦略レビューの総括役です。
複数の仮説とその検証結果(支持/棄却/追加データ必要、それぞれの根拠)を踏まえて、
全体として何が言えるかを一段階上の視点でまとめてください。
以下を厳守すること:
1. 個々の仮説の結論を繰り返すのではなく、複数の結果を横断して見えるパターンや、
    より上位の構造的な結論があれば述べる。特にパターンが見えない場合は無理に作らない
2. 実行可能な次のアクション案を1〜2個提示する
    (「閾値をX円にすべき」のような具体的な数値変更の提案はしない)
3. 検証結果にない情報を根拠にしない。飛躍した推測をしない
4. 結論や優先度を上書きせず、投資判断やパラメータ変更を指示しない
出力はSlack通知に収まる数行の日本語にしてください。"""


def _build_period_review_prompt(period_label: str, period_summary: dict) -> str:
    review_summary = dict(period_summary)
    backtest = review_summary.get("backtest")
    period = review_summary.get("period")
    if isinstance(backtest, dict) and not (
        backtest.get("exact_period_run_available")
        and isinstance(period, dict)
        and period.get("status") == "complete"
    ):
        review_summary["backtest"] = {"exact_period_run_available": False}
    filter_events = review_summary.get("filter_decision_events")
    if isinstance(filter_events, dict):
        same_day = filter_events.get("same_day")
        review_summary["filter_decision_events"] = (
            {"same_day": same_day} if isinstance(same_day, dict) else {}
        )
    return (
        f"以下は自動売買システムの{period_label}集計です。数値を再計算せず、入力にある事実だけを根拠に、"
        "次の検証につながる短い日本語レビューを作成してください。全体を900字以内にしてください。"
        "見出しは必要なものだけを使い、各見出しの箇条書きは最大2件にしてください。"
        "「観測事実」は必ず使い、通知本文に表示済みの数値を再掲せず、結果から読み取れる所見だけを書いてください。"
        "period.statusがcomplete以外の場合だけ「期間・稼働状態」を使い、途中集計または未開始であることを明記してください。"
        "この場合、未到来のレポートやバックテストがないことを障害やスケジュール不備と推測しないでください。"
        "daily.operational_summaryにエラー、緊急停止、unavailable、not_evaluated、not_recordedがある場合だけ、"
        "同見出しで取引・戦略を評価できる状態だったかを事実として示してください。"
        "backtest.exact_period_run_availableがtrueかつperiod.statusがcompleteの場合だけ、対象期間のバックテスト結果を評価してください。"
        "対象期間一致のバックテストがない場合は「比較対象なし」の一言だけにし、別期間の損益・取引数は使わないでください。"
        "それ以外ではbacktest.runsの損益・取引数を合算せず、ペーパートレードとの乖離や成績比較を結論づけないでください。"
        "comparison.availableがtrueの場合だけ「期間比較」を使い、comparison.basisと両期間のreport_countを併記してください。"
        "判定イベントに触れる場合はsame_dayの当日基準だけを使い、複数営業日基準は使わないでください。"
        "概算損益を扱う場合は実取引損益ではないと明記してください。"
        "daily.total_profit_lossは日次レポートの記録値であり、内訳がない限り実現損益・未実現損益を断定しないでください。"
        "過学習はアウトオブサンプル結果やパラメータ比較などの根拠がない限り断定しないでください。"
        "仮説を書く場合は「仮説」と明示し、入力に直接根拠がある場合だけにしてください。"
        "未解決事項を判定できる具体的な観測値またはログがある場合だけ、「次回確認」を1件書いてください。"
        "ロジック変更、認証情報の修正、投資判断、売買指示は行わず、検証期間中の参考意見だと明記してください。\n\n"
        f"{period_label}集計:\n{json.dumps(review_summary, ensure_ascii=False, indent=2)}"
    )


class OpenAIDailyAnalyzer:
    """日次取引とバックテストの集計をLLMで評価するアダプター。"""

    def __init__(self, api_key: str, model: str, api_url: str, timeout: float = config.LLM_TIMEOUT_SECONDS):
        self.api_key = api_key
        self.model = model
        self.api_url = api_url
        self.timeout = timeout

    def analyze(self, daily_summary: dict) -> str | None:
        prompt = (
            "以下は自動売買の1日分の機械集計です。数値を再計算せず、入力にある事実だけを根拠に、"
            "翌日の検証につながる短い日本語レビューを作成してください。全体を600字以内にしてください。"
            "見出しは必要なものだけを使い、各見出しの箇条書きは最大2件にしてください。"
            "「本日の事実」は必ず使いますが、通知本文のA・Bに同じ数値が載るため、入力の数値の再掲はせず、所見(読み取れること・注意点)だけを書いてください。"
            "数値を出す場合は割合を%表記にそろえ(例: 0.1は10%)、mean_selected_rateのようなキー名や小数表記は使わないでください。"
            "no_trade_reasonがある場合は、売買0件の理由として所見に反映し、reasonが未記録の場合は理由が記録されていないことだけを示してください。"
            "注文数、損益、保有、market_conditions、log_errors、trend_checkから重要な点だけを扱ってください。"
            "market_conditions.assessment_statusがnot_evaluatedまたはunavailable、またはlog_errors.has_errorsがtrueの場合だけ、"
            "「運用・データ状態」を使い、取引・戦略を評価できる状態だったかを事実として示してください。"
            "trend_check.availableがfalseの場合は、答え合わせができなかった理由をそのまま示してください。"
            "skip_countsが空でない場合だけ、見送りの内訳を簡潔に示してください。"
            "当日の未解決事項を判定できる具体的な観測値またはログがある場合だけ、「次回確認」を1件書いてください。"
            "仮説を書く場合は「仮説」と明示し、入力に直接根拠がある場合だけにしてください。"
            "assessment_statusがnot_evaluatedは市場判定が未実行であり、市場データ取得失敗とは書かないでください。"
            "assessment_statusがunavailableでも、failure_reasonまたはエラー概要にない障害原因を断定しないでください。"
            "認証エラーと市場データ未取得など、複数事象の因果関係も入力に根拠がなければ断定しないでください。"
            "日次通知に載せるのは当日実績と当日の答え合わせだけです。複数営業日基準の結果は扱わないでください。"
            "見送り・損切り後の観測損益を扱う場合は、観測終了時刻までの概算であり実取引損益ではないと明記してください。"
            "ロジック変更、認証情報の修正、投資判断、売買指示は行わず、検証期間中の参考意見だと明記してください。\n\n"
            f"日次集計:\n{json.dumps(daily_summary, ensure_ascii=False, indent=2)}"
        )
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの日次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:1800]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("日次LLM分析に失敗しました: %s", exc)
            return None

    def analyze_backtest(self, backtest_summary: dict) -> str | None:
        prompt = (
            "以下は自動売買戦略のバックテスト集計です。与えられた数値だけを根拠に、"
            "日本語で短く評価してください。出力は「結果の評価」「注意点・不確実性」"
            "「次回の確認事項」の3見出し、各見出し2項目以内としてください。"
            "過学習、取引数不足、手数料・スリッページ、未実現損益の影響が判断できる場合は指摘し、"
            "ロジック変更や投資判断を命令しないでください。検証期間中の参考意見だと明記してください。\n\n"
            f"バックテスト集計:\n{json.dumps(backtest_summary, ensure_ascii=False, indent=2)}"
        )
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムのバックテストレビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:1800]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("バックテストLLM分析に失敗しました: %s", exc)
            return None

    def analyze_monthly(self, monthly_summary: dict) -> str | None:
        prompt = _build_period_review_prompt("月次", monthly_summary)
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの月次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:2400]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("月次LLM分析に失敗しました: %s", exc)
            return None

    def analyze_weekly(self, weekly_summary: dict) -> str | None:
        prompt = _build_period_review_prompt("週次", weekly_summary)
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "あなたは自動売買システムの週次レビュー担当です。"},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に本文がありません")
            return content.strip()[:2400]
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("週次LLM分析に失敗しました: %s", exc)
            return None

    def analyze_strategy_review(self, review_summary: dict) -> str | None:
        prompt = (
            "以下は複数の仮説と、それぞれの定量検証結果です。結果の個別要約ではなく、"
            "横断的な構造パターンと1〜2個の次アクション案を作成してください。"
            "未検証・検証不能の項目は判定済みとして扱わず、判定結果の上書きや数値変更案を出さないでください。\n\n"
            f"戦略レビュー結果(JSON):\n{json.dumps(review_summary, ensure_ascii=False, indent=2)}"
        )
        try:
            response = requests.post(
                self.api_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": STRATEGY_REVIEW_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            content = response.json().get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("LLM応答に総括本文がありません")
            return content.strip()
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            logger.warning("戦略レビュー総括に失敗しました: %s", exc)
            return None


def create_daily_analyzer() -> OpenAIDailyAnalyzer | None:
    if not config.LLM_DAILY_ANALYSIS_ENABLED or not config.LLM_API_KEY:
        return None
    return OpenAIDailyAnalyzer(
        api_key=config.LLM_API_KEY,
        model=config.LLM_MODEL,
        api_url=config.LLM_API_URL,
    )