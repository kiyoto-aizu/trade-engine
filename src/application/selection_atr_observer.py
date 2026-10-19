"""選定時点のATR比を銘柄単位で観測する(取得結果は銘柄ごとにキャッシュ)。失敗しても呼び出し元の処理を止めない。"""
import logging
from datetime import date

from src.config import config
from src.domain.selection_atr_observation import (
    REASON_FETCH_FAILED,
    SelectionAtrObservation,
    observe_selection_atr,
    unavailable,
)

logger = logging.getLogger(__name__)


class SelectionAtrObserver:
    def __init__(self, history_fetcher, *, period=None, caution_ratio=None, danger_ratio=None):
        """history_fetcher: (symbol, until: date) -> list[DatedDailyBar]"""
        self._fetch = history_fetcher
        self._period = config.ATR_PERIOD if period is None else period
        self._caution = config.ATR_CAUTION_RATIO if caution_ratio is None else caution_ratio
        self._danger = config.ATR_DANGER_RATIO if danger_ratio is None else danger_ratio
        self._cache: dict[tuple[str, date], list | None] = {}

    def observe(self, symbol: str, as_of: date) -> SelectionAtrObservation:
        key = (symbol, as_of)
        if key not in self._cache:
            try:
                self._cache[key] = list(self._fetch(symbol, as_of))
            except Exception:
                logger.warning("選定時点ATR比の日足取得に失敗しました: %s", symbol, exc_info=True)
                self._cache[key] = None
        bars = self._cache[key]
        if bars is None:
            return unavailable(as_of, REASON_FETCH_FAILED)
        return observe_selection_atr(bars, as_of, self._period, self._caution, self._danger)

    def observe_safely(self, symbol: str, as_of: date) -> SelectionAtrObservation:
        try:
            return self.observe(symbol, as_of)
        except Exception:
            logger.warning("選定時点ATR比の算出に失敗しました: %s", symbol, exc_info=True)
            return unavailable(as_of, REASON_FETCH_FAILED)
