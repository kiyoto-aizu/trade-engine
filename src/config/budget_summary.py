"""「使っていい金額」の設定・派生値・状態を、起動時にログへ一覧で出す(観測のみ)。

警告は出すだけで処理は止めない。関数内の例外は握ってログに残す。
"""
import json
import logging
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

DEVIATION_LIMIT = 0.20
MARGIN_MIN = 0.5
MARGIN_MAX = 1.0

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _yen(value: Any) -> str:
    return "不明" if value is None else f"{value:,.0f}"


def _read_json(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    """(内容, 状態メモ)。無い・壊れている場合は内容None。"""
    try:
        if not path.exists():
            return None, "ファイルなし"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None, "形式不正"
        return data, None
    except Exception as exc:
        return None, f"読込失敗({type(exc).__name__})"


def _deviates(value: Any, capital: float) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not capital:
        return False
    return abs(value - capital) / capital > DEVIATION_LIMIT


def build_budget_summary(
    cfg: Any,
    *,
    effective_api_limit: Optional[float] = None,
    paper_state_path: Optional[Path] = None,
    baseline_path: Optional[Path] = None,
) -> Tuple[List[str], List[str]]:
    """(情報行, 警告行)を返す。cfgは設定値を属性に持つオブジェクト。"""
    capital = cfg.OPERATING_CAPITAL
    max_order = cfg.MAX_ORDER_AMOUNT_PER_TRADE
    positions = cfg.TARGET_POSITIONS
    margin = cfg.SCREENING_PRICE_MARGIN
    api_setting = cfg.API_SOFT_LIMIT
    unit = cfg.ORDER_UNIT
    alternates = tuple(cfg.SCREENING_ALTERNATE_PRICE_CAPS)

    api_effective = effective_api_limit if effective_api_limit is not None else None
    api_for_budget = api_effective if api_effective is not None else api_setting
    api_note = "" if api_effective is not None else "(API取得前のため設定値で代用)"

    per_slot = min(capital / positions, max_order)
    budget = min(per_slot, api_for_budget)
    main_cap = per_slot / unit * margin
    main_required = main_cap * unit

    info: List[str] = []
    warnings: List[str] = []

    info.append(
        f"予算設定: 資金={_yen(capital)} | 注文上限={_yen(max_order)} | 枠数={positions} | "
        f"マージン={margin} | API上限(設定値)={_yen(api_setting)} | "
        f"API上限(実効値)={_yen(api_effective) if api_effective is not None else '未取得'}"
    )
    info.append(
        f"派生値: 1枠予算={_yen(budget)}{api_note} | 主帯の価格上限={main_cap:g}円"
    )

    band_parts = []
    for cap in (main_cap, *alternates):
        required = cap * unit
        ratio = required / budget if budget else float("inf")
        band_parts.append(f"{cap:g}円→100株必要額{_yen(required)}(1枠予算の{ratio:.2f}倍)")
    info.append("帯別: " + " / ".join(band_parts))

    state_parts = []
    paper_cash = None
    if paper_state_path is not None:
        state, note = _read_json(Path(paper_state_path))
        if state is None:
            state_parts.append(f"ペーパー現金=なし({note})")
        else:
            paper_cash = state.get("cash")
            state_parts.append(f"ペーパー現金={_yen(paper_cash)}")
    baseline_capital = None
    if baseline_path is not None:
        baseline, note = _read_json(Path(baseline_path))
        if baseline is None:
            state_parts.append(f"当日開始資産=なし({note})")
        else:
            baseline_capital = baseline.get("capital")
            state_parts.append(f"当日開始資産={_yen(baseline_capital)}(保存日={baseline.get('date')})")
    if state_parts:
        info.append("状態: " + " | ".join(state_parts))

    if _deviates(paper_cash, capital):
        warnings.append(f"ペーパー現金{_yen(paper_cash)}が設定資金{_yen(capital)}から±20%超ずれています。")
    if _deviates(baseline_capital, capital):
        warnings.append(f"当日開始資産{_yen(baseline_capital)}が設定資金{_yen(capital)}から±20%超ずれています。")
    if alternates and main_cap >= min(alternates):
        warnings.append(f"主帯の価格上限{main_cap:g}円が代替帯の最小値{min(alternates):g}円以上です。")
    if max_order > api_for_budget:
        warnings.append(f"注文上限{_yen(max_order)}がAPI上限{_yen(api_for_budget)}を超えています{api_note}。")
    if margin < MARGIN_MIN or margin > MARGIN_MAX:
        warnings.append(f"SCREENING_PRICE_MARGIN={margin}が{MARGIN_MIN}〜{MARGIN_MAX}の範囲外です。")
    if budget < main_required:
        warnings.append(f"1枠予算{_yen(budget)}が主帯の100株必要額{_yen(main_required)}に満たません。")
    return info, warnings


def log_budget_summary(
    logger: Optional[logging.Logger] = None,
    *,
    cfg: Any = None,
    token: Optional[str] = None,
    api_limit_fetcher: Optional[Callable[[str], Optional[float]]] = None,
) -> None:
    """サマリーと警告をログに出す。どんな例外も握り、呼び出し元を止めない。"""
    log = logger or logging.getLogger(__name__)
    try:
        if cfg is None:
            from src.config import config as cfg
        effective = None
        if getattr(cfg, "IS_DEMO", False):
            effective = cfg.API_SOFT_LIMIT
        elif token:
            try:
                fetcher = api_limit_fetcher
                if fetcher is None:
                    from src.infrastructure.kabu.get_apisoftlimit import get_api_soft_limit as fetcher
                effective = fetcher(token)
            except Exception as exc:
                log.warning("API上限の実効値を取得できませんでした: %s", type(exc).__name__)
        info, warnings = build_budget_summary(
            cfg,
            effective_api_limit=effective,
            paper_state_path=_PROJECT_ROOT / cfg.PAPER_ACCOUNT_STATE_FILE,
            baseline_path=_PROJECT_ROOT / cfg.KILL_SWITCH_BASELINE_FILE,
        )
        for line in info:
            log.info(line)
        for line in warnings:
            log.warning("予算設定の警告: %s", line)
    except Exception:
        log.warning("予算サマリーの作成に失敗しました(処理は継続します)。", exc_info=True)
