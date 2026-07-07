"""测试公共设施：定位系统根目录、加载 schema / fixture。"""
import json
import sys
from pathlib import Path

import pytest

SYSTEM_ROOT = Path(__file__).resolve().parent.parent   # meta-research/
SCHEMAS_DIR = SYSTEM_ROOT / "schemas"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# 让测试可 import orchestrator 包
if str(SYSTEM_ROOT) not in sys.path:
    sys.path.insert(0, str(SYSTEM_ROOT))


@pytest.fixture(scope="session")
def system_root() -> Path:
    return SYSTEM_ROOT


def load_schema(name: str) -> dict:
    """按短名加载 schema（如 'plan' → schemas/plan.schema.json）。"""
    path = SCHEMAS_DIR / f"{name}.schema.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def iter_fixture_cases(kind: str):
    """遍历 fixtures/<kind>/<schema_name>/*.json → (schema_name, 文件路径)。

    kind ∈ {valid, invalid}。目录名 = schema 短名，一目录多用例。
    """
    base = FIXTURES_DIR / kind
    if not base.is_dir():
        return
    for schema_dir in sorted(base.iterdir()):
        if not schema_dir.is_dir():
            continue
        for case in sorted(schema_dir.glob("*.json")):
            yield schema_dir.name, case
