"""J-Quants canonical の運用契約（docs/detail_design/19）を守るための静的検査。"""
import ast
from pathlib import Path

import pyarrow as pa

from src.infrastructure.market_data.jquants_canonical_store import (
    SCHEMA_VERSION,
    canonical_schema,
)

SRC = Path(__file__).resolve().parents[1] / "src"

# 意図してschemaを変えるときは、SCHEMA_VERSIONとこの固定値を同時に更新する。
EXPECTED_SCHEMA_VERSION = "1"
_PRICES = [(name, pa.float64(), True) for name in ("O", "H", "L", "C", "Vo", "Va")]
EXPECTED_COLUMNS = {
    "daily": [("Code", pa.string(), False), ("Date", pa.date32(), False), *_PRICES,
              *[(name, pa.float64(), True)
                for name in ("AdjFactor", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")],
              ("UL", pa.int8(), True), ("LL", pa.int8(), True)],
    "minute": [("Code", pa.string(), False), ("Date", pa.date32(), False),
               ("Time", pa.timestamp("us", tz="Asia/Tokyo"), False), *_PRICES],
}

# J-Quants関連と混ぜてはいけない既存のデータ源・出力先。
FORBIDDEN_FOR_JQUANTS = ("yahoo", "yfinance", "backtest", "report_publication",
                         "parquet_minute_bar_repository")


def _is_jquants(path: Path) -> bool:
    return "jquants" in path.stem


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def test_canonical_schema_is_pinned():
    assert SCHEMA_VERSION == EXPECTED_SCHEMA_VERSION
    for dataset, expected in EXPECTED_COLUMNS.items():
        actual = [(field.name, field.type, field.nullable)
                  for field in canonical_schema(dataset)]
        assert actual == expected


def test_jquants_modules_do_not_import_other_data_sources():
    violations = []
    for path in SRC.rglob("*.py"):
        if _is_jquants(path):
            for module in _imported_modules(path):
                if any(word in module for word in FORBIDDEN_FOR_JQUANTS):
                    violations.append(f"{path.name} -> {module}")
    assert not violations


def test_other_modules_do_not_import_jquants():
    violations = []
    for path in SRC.rglob("*.py"):
        if not _is_jquants(path):
            for module in _imported_modules(path):
                if "jquants" in module:
                    violations.append(f"{path.name} -> {module}")
    assert not violations


def test_guard_detects_forbidden_import(tmp_path):
    sample = tmp_path / "jquants_sample.py"
    sample.write_text("from src.infrastructure.market_data.yahoo_finance_client import X\n",
                      encoding="utf-8")
    assert any("yahoo" in module for module in _imported_modules(sample))
