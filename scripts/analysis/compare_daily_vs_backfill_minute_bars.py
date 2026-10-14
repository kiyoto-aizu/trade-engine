"""日次取得した当日分足と、後日の土曜バックフィル後の分足を比べる診断(読み取り専用。分足Parquetは変更しない)。

使い方:
  1) 日次取得の直後:   python scripts/analysis/compare_daily_vs_backfill_minute_bars.py snapshot --date 2026-10-13
  2) 土曜バックフィル後: python scripts/analysis/compare_daily_vs_backfill_minute_bars.py compare --date 2026-10-13
銘柄は当日のフィルタ結果(data/filtering/<日付>.json)。snapshotは data/analysis/minute_bar_snapshots/<日付>.json に保存する。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pyarrow.parquet as parquet

ROOT = Path(__file__).resolve().parents[2]
MINUTE_DIR = ROOT / "data" / "minute_bars_parquet"
SNAPSHOT_DIR = ROOT / "data" / "analysis" / "minute_bar_snapshots"


def load_symbols(day: str) -> list[str]:
    data = json.loads((ROOT / "data" / "filtering" / f"{day}.json").read_text(encoding="utf-8"))
    return [str(symbol) for symbol in data["symbols"]]


def load_bars(symbol: str, day: str) -> dict[str, list]:
    path = MINUTE_DIR / f"symbol={symbol}" / f"date={day}" / "data.parquet"
    if not path.is_file():
        return {}
    rows = parquet.read_table(path, columns=["time", "price", "volume"]).to_pylist()
    return {row["time"]: [row["price"], row["volume"]] for row in rows}


def snapshot(day: str) -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    data = {symbol: load_bars(symbol, day) for symbol in load_symbols(day)}
    (SNAPSHOT_DIR / f"{day}.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    for symbol, bars in data.items():
        print(f"{symbol}: {len(bars)}本")
    return 0


def compare(day: str) -> int:
    path = SNAPSHOT_DIR / f"{day}.json"
    if not path.is_file():
        print(f"スナップショットがありません: {path}")
        return 2
    before_all = json.loads(path.read_text(encoding="utf-8"))
    mismatch = False
    for symbol, before in before_all.items():
        after = load_bars(symbol, day)
        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        changed = sorted(t for t in set(before) & set(after) if before[t][0] != after[t][0])
        ok = not (added or removed or changed)
        mismatch = mismatch or not ok
        print(
            f"{symbol}: 日次{len(before)}本 / 現在{len(after)}本 / 追加{len(added)} 欠落{len(removed)} 価格差{len(changed)}"
            f" -> {'一致' if ok else '不一致'}"
        )
        for moment in changed[:3]:
            print(f"    {moment}: 日次{before[moment][0]} 現在{after[moment][0]}")
    return 1 if mismatch else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["snapshot", "compare"])
    parser.add_argument("--date", required=True)
    args = parser.parse_args(argv)
    return snapshot(args.date) if args.mode == "snapshot" else compare(args.date)


if __name__ == "__main__":
    sys.exit(main())
