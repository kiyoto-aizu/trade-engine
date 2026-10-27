from src.domain.jquants_disk_space import (
    BACKFILL_MIN_FREE_BYTES,
    DiskVerdict,
    VolumeNeed,
    backfill_may_start,
    judge_volume,
    judge_volumes,
)


def test_judge_volume_boundaries():
    assert judge_volume(VolumeNeed(300, 100)) is DiskVerdict.OK
    assert judge_volume(VolumeNeed(299, 100)) is DiskVerdict.HOLD
    assert judge_volume(VolumeNeed(150, 100)) is DiskVerdict.HOLD
    assert judge_volume(VolumeNeed(149, 100)) is DiskVerdict.STOP


def test_judge_volumes_takes_worst():
    assert judge_volumes([VolumeNeed(1000, 100), VolumeNeed(200, 100)]) is DiskVerdict.HOLD
    assert judge_volumes([VolumeNeed(100, 100), VolumeNeed(1000, 100)]) is DiskVerdict.STOP
    assert judge_volumes([]) is DiskVerdict.OK


def test_backfill_floor():
    floor = BACKFILL_MIN_FREE_BYTES
    assert backfill_may_start(floor + 5, 5)
    assert not backfill_may_start(floor + 4, 5)
    assert not backfill_may_start(floor - 1, 0)
