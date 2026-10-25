"""J-Quants canonical の運用契約（docs/detail_design/19）を守るための静的検査。"""
import ast
from pathlib import Path

import pyarrow as pa
import pytest

from src.infrastructure.market_data import jquants_canonical_store as store
from src.infrastructure.market_data.jquants_canonical_store import (
    SCHEMA_VERSION,
    TRANSFORMER_VERSION,
    canonical_schema,
)

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = (ROOT / "src", ROOT / "scripts")

_PRICES = [(name, pa.float64(), True) for name in ("O", "H", "L", "C", "Vo", "Va")]

# 版ごとの固定表。意図してschemaを変えるときは、SCHEMA_VERSIONを上げて新しい版の表を追加する。
# 旧版の表は消さない（消すと差分に出る）。同じ版の中身を変えてはいけない。
EXPECTED_COLUMNS_BY_VERSION = {
    "1": {
        "daily": [("Code", pa.string(), False), ("Date", pa.date32(), False), *_PRICES,
                  *[(name, pa.float64(), True)
                    for name in ("AdjFactor", "AdjO", "AdjH", "AdjL", "AdjC", "AdjVo")],
                  ("UL", pa.int8(), True), ("LL", pa.int8(), True)],
        "minute": [("Code", pa.string(), False), ("Date", pa.date32(), False),
                   ("Time", pa.timestamp("us", tz="Asia/Tokyo"), False), *_PRICES],
    },
}

# 変換ロジックを変えるときは、TRANSFORMER_VERSIONとこの値、下の変換結果を同時に更新する。
EXPECTED_TRANSFORMER_VERSION = "3"

# J-Quants関連と混ぜてはいけない既存のデータ源・出力先。
FORBIDDEN_FOR_JQUANTS = ("yahoo", "yfinance", "backtest", "report_publication",
                         "parquet_minute_bar_repository")
# jquants側が読んでよい、名前にjquantsを含まない共有モジュール（接頭辞）。
JQUANTS_MAY_IMPORT_SHARED = ("src.config",)
# 接続層を追加するときは、そのモジュールのパス（ROOTからの相対）をここへ1つだけ加える（レビュー対象）。
CONNECTION_LAYER_ALLOWED: set[str] = set()
DYNAMIC_IMPORT_NAMES = {"import_module", "__import__"}
MARKET_DATA_PACKAGE = ["src", "infrastructure", "market_data"]


def _is_jquants(path: Path) -> bool:
    return "jquants" in path.stem


def _source_files() -> list[Path]:
    return [path for directory in SCAN_DIRS for path in directory.rglob("*.py")]


def _package_of(path: Path) -> list[str]:
    try:
        return list(path.resolve().relative_to(ROOT).parent.parts)
    except ValueError:
        return []


def _imported_modules(path: Path, package: list[str] | None = None) -> set[str]:
    # 相対importは、ファイルの置き場所から絶対名に直してから判定する。
    package = _package_of(path) if package is None else package
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            anchor = package[:len(package) - (node.level - 1)] if node.level else []
            base = ".".join(anchor + (node.module.split(".") if node.module else []))
            modules.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return modules


def _dynamic_imports(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in DYNAMIC_IMPORT_NAMES:
                found.append(name)
    return found


def _is_forbidden(module: str) -> bool:
    return any(word in module for word in FORBIDDEN_FOR_JQUANTS)


def _is_allowed_shared(module: str) -> bool:
    if not module.startswith("src."):
        return True
    return "jquants" in module or module.startswith(JQUANTS_MAY_IMPORT_SHARED)


def _jquants_side_violations(path: Path, package: list[str] | None = None) -> list[str]:
    violations = [f"{path.name} -> {module}" for module in sorted(_imported_modules(path, package))
                  if _is_forbidden(module) or not _is_allowed_shared(module)]
    violations += [f"{path.name} -> {name}() dynamic import" for name in _dynamic_imports(path)]
    return violations


def _other_side_violations(path: Path, package: list[str] | None = None) -> list[str]:
    violations = [f"{path.name} -> {module}" for module in sorted(_imported_modules(path, package))
                  if "jquants" in module]
    violations += [f"{path.name} -> {name}() dynamic import" for name in _dynamic_imports(path)]
    return violations


def test_canonical_schema_is_pinned_per_version():
    assert SCHEMA_VERSION in EXPECTED_COLUMNS_BY_VERSION
    expected_by_dataset = EXPECTED_COLUMNS_BY_VERSION[SCHEMA_VERSION]
    for dataset, expected in expected_by_dataset.items():
        actual = [(field.name, field.type, field.nullable)
                  for field in canonical_schema(dataset)]
        assert actual == expected


def _datasets_in_implementation() -> set[str]:
    tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8-sig"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "canonical_schema":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Set):
                    return {item.value for item in inner.elts if isinstance(item, ast.Constant)}
    raise AssertionError("canonical_schema のdataset一覧が見つからない")


def test_dataset_list_matches_pinned_tables():
    for expected_by_dataset in EXPECTED_COLUMNS_BY_VERSION.values():
        assert set(expected_by_dataset) == _datasets_in_implementation()


def test_transformer_version_and_conversion_are_pinned():
    assert TRANSFORMER_VERSION == EXPECTED_TRANSFORMER_VERSION
    record = {"Code": "1234", "Date": "2000-01-01", "O": 1, "H": 3, "L": 1, "C": 2,
              "Vo": 10, "Va": 20, "AdjFactor": 1, "AdjO": 1, "AdjH": 3, "AdjL": 1,
              "AdjC": 2, "AdjVo": 10, "UL": "0", "LL": "1"}
    row = store._convert_row(record, "daily", "2000-01-01")
    assert (row["C"], row["AdjC"], row["Vo"], row["UL"], row["LL"]) == (2.0, 2.0, 10.0, 0, 1)
    minute = {"Code": "1234", "Date": "2000-01-01", "Time": "09:00",
              "O": 1, "H": 3, "L": 1, "C": 2, "Vo": 10, "Va": 20}
    minute_row = store._convert_row(minute, "minute", "2000-01-01")
    assert minute_row["Time"].strftime("%H:%M") == "09:00"
    assert minute_row["C"] == 2.0


def test_jquants_modules_only_import_allowed_modules():
    violations = []
    for path in _source_files():
        if _is_jquants(path):
            violations += _jquants_side_violations(path)
    assert not violations


def test_other_modules_do_not_import_jquants():
    violations = []
    for path in _source_files():
        relative = path.relative_to(ROOT).as_posix()
        if not _is_jquants(path) and relative not in CONNECTION_LAYER_ALLOWED:
            violations += _other_side_violations(path)
    assert not violations


def test_connection_layer_does_not_import_other_data_sources():
    for relative in CONNECTION_LAYER_ALLOWED:
        path = ROOT / relative
        modules = _imported_modules(path)
        assert not [m for m in modules if _is_forbidden(m)], relative
        assert not _dynamic_imports(path), relative


@pytest.mark.parametrize("source", [
    "from src.infrastructure.market_data.yahoo_finance_client import X\n",
    "from . import yahoo_helper\n",
    "from .get_daily_closes import X\n",
    "from .index_daily_bar_fallback import X\n",
    "from ..persistence import some_repository\n",
    "import importlib\nimportlib.import_module('x')\n",
    "from src.application.backtest_usecase import Y\n",
    "from src.infrastructure.persistence.some_repository import Z\n",
])
def test_guard_detects_jquants_side_violations(tmp_path, source):
    sample = tmp_path / "jquants_sample.py"
    sample.write_text(source, encoding="utf-8")
    assert _jquants_side_violations(sample, MARKET_DATA_PACKAGE)


def test_guard_allows_jquants_side_imports(tmp_path):
    sample = tmp_path / "jquants_sample.py"
    sample.write_text("from src.config import config\n"
                      "from src.infrastructure.market_data.jquants_raw_store import X\n"
                      "from src.infrastructure.market_data import jquants_raw_store\n"
                      "from .jquants_raw_store import Y\n"
                      "from . import jquants_raw_store as z\n",
                      encoding="utf-8")
    assert not _jquants_side_violations(sample, MARKET_DATA_PACKAGE)


@pytest.mark.parametrize("source", [
    "from src.infrastructure.market_data.jquants_canonical_store import X\n",
    "from src.infrastructure.market_data import jquants_raw_store\n",
    "from . import jquants_raw_store\n",
    "import importlib\nimportlib.import_module('x')\n",
])
def test_guard_detects_other_side_violations(tmp_path, source):
    sample = tmp_path / "backtest_sample.py"
    sample.write_text(source, encoding="utf-8")
    assert _other_side_violations(sample, MARKET_DATA_PACKAGE)
