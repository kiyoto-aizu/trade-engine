from datetime import date

from src.application.minute_bar_backfill_usecase import MinuteBarBackfillUseCase
from src.domain.models import MinuteBar
from src.infrastructure.persistence.parquet_minute_bar_repository import ParquetMinuteBarRepository


def test_yahoo_bar_overwrites_poll_bar_for_same_minute(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    target_date = date(2026, 9, 10)

    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:31:00", price=100.0, cumulative_volume=5000, volume=None, source="poll",
    ))
    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:31:00", price=101.5, cumulative_volume=None, volume=1200, source="yahoo",
    ))

    bars = repository.load_bars(target_date, "7203")
    assert len(bars) == 1
    assert bars[0].source == "yahoo"
    assert bars[0].price == 101.5


def test_poll_bar_does_not_downgrade_existing_yahoo_bar(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    target_date = date(2026, 9, 10)

    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:31:00", price=101.5, cumulative_volume=None, volume=1200, source="yahoo",
    ))
    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:31:00", price=999.0, cumulative_volume=5000, volume=None, source="poll",
    ))

    bars = repository.load_bars(target_date, "7203")
    assert len(bars) == 1
    assert bars[0].source == "yahoo"
    assert bars[0].price == 101.5  # pollでは上書きされない


def test_backfill_usecase_splits_bars_by_date_and_saves_them(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)

    def fake_fetch(symbol: str, days: int) -> list[MinuteBar]:
        return [
            MinuteBar(time="2026-09-08T09:31:00", price=100.0, cumulative_volume=None, volume=500, source="yahoo"),
            MinuteBar(time="2026-09-09T09:31:00", price=102.0, cumulative_volume=None, volume=600, source="yahoo"),
        ]

    usecase = MinuteBarBackfillUseCase(fetch_intraday_bars=fake_fetch, repository=repository)
    imported_counts = usecase.run(["7203"], days=7)

    assert imported_counts == {"7203": 2}
    assert len(repository.load_bars(date(2026, 9, 8), "7203")) == 1
    assert len(repository.load_bars(date(2026, 9, 9), "7203")) == 1


def test_backfill_usecase_records_zero_when_fetch_returns_nothing(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    usecase = MinuteBarBackfillUseCase(fetch_intraday_bars=lambda symbol, days: [], repository=repository)

    imported_counts = usecase.run(["9999"], days=7)

    assert imported_counts == {"9999": 0}


def test_backfill_usecase_preserves_existing_minute_values_and_only_adds_new_times(tmp_path):
    repository = ParquetMinuteBarRepository(tmp_path)
    target_date = date(2026, 9, 10)
    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:31:00", price=100.0, cumulative_volume=5000, volume=None, source="poll",
    ))
    repository.append_bar(target_date, "7203", MinuteBar(
        time="2026-09-10T09:32:00", price=101.0, cumulative_volume=None, volume=100, source="yahoo",
    ))
    usecase = MinuteBarBackfillUseCase(
        fetch_intraday_bars=lambda _symbol, _days: [
            MinuteBar(
                time="2026-09-10T09:31:00", price=999.0, cumulative_volume=None, volume=900, source="yahoo",
            ),
            MinuteBar(
                time="2026-09-10T09:32:00", price=999.0, cumulative_volume=None, volume=900, source="yahoo",
            ),
            MinuteBar(
                time="2026-09-10T09:33:00", price=102.0, cumulative_volume=None, volume=200, source="yahoo",
            ),
        ],
        repository=repository,
    )

    imported_counts = usecase.run(["7203"], days=7)

    bars = repository.load_bars(target_date, "7203")
    assert imported_counts == {"7203": 1}
    assert [(bar.time, bar.price, bar.cumulative_volume, bar.volume, bar.source) for bar in bars] == [
        ("2026-09-10T09:31:00", 100.0, 5000, None, "poll"),
        ("2026-09-10T09:32:00", 101.0, None, 100, "yahoo"),
        ("2026-09-10T09:33:00", 102.0, None, 200, "yahoo"),
    ]
