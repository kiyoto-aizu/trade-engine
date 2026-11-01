"""Pre-acquisition disk checks use the existing relative-capacity thresholds."""

from src.domain.jquants_disk_space import DEFAULT_RUN_BYTES, VolumeNeed, judge_volumes
from src.infrastructure.market_data.jquants_disk_space import measure_volume


class JQuantsDailyDiskCheck:
    def __init__(self, raw_directory, canonical_directory, *, volume_probe=measure_volume):
        self.raw_directory = raw_directory
        self.canonical_directory = canonical_directory
        self.volume_probe = volume_probe

    def __call__(self, dataset):
        try:
            raw, raw_device = self.volume_probe(self.raw_directory)
            canonical, canonical_device = self.volume_probe(self.canonical_directory)
            sizes = DEFAULT_RUN_BYTES[dataset]
            volumes = [VolumeNeed(min(raw, canonical), sizes["raw"] + sizes["canonical"])] if (
                raw_device == canonical_device
            ) else [VolumeNeed(raw, sizes["raw"]), VolumeNeed(canonical, sizes["canonical"])]
            return judge_volumes(volumes)
        except Exception:
            return None
