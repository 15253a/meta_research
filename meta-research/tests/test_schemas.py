"""CP1.1 契约层自验：schema 元校验 + 正/负例 + policy / goal_brief 启动契约。

对应验收锚点：
- 四阶段产物 schema 是 M0 验收「逐项过 schema validator」的校验基准（第三部分 §7.1 M0 行）；
- policy.yaml 解析且过 schema = 运行前提 #4（§7.5）；
- goal_brief.md frontmatter 含合法 predicate_json = 运行前提 #2（缺失/非法 → 启动失败，§4.6.7）。
"""
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from conftest import (
    FIXTURES_DIR, SCHEMAS_DIR, SYSTEM_ROOT,
    iter_fixture_cases, load_json, load_schema,
)

STAGE_SCHEMAS = [
    "idea_set", "plan", "bundle_target",
    "answer", "tree_ops", "selection",
    "resource_request", "policy",
]


def make_validator(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(name))


# ---------------------------------------------------------------------------
# 1. schema 自身合法（draft 2020-12 元校验）且清单齐全
# ---------------------------------------------------------------------------

def test_schema_inventory_complete():
    on_disk = {p.name[: -len(".schema.json")] for p in SCHEMAS_DIR.glob("*.schema.json")}
    assert on_disk == set(STAGE_SCHEMAS), f"schema 清单漂移: {on_disk ^ set(STAGE_SCHEMAS)}"


@pytest.mark.parametrize("name", STAGE_SCHEMAS)
def test_schema_is_valid_draft2020(name):
    Draft202012Validator.check_schema(load_schema(name))


# ---------------------------------------------------------------------------
# 2. 正例全过 / 负例全拒（每个 schema 至少各一）
# ---------------------------------------------------------------------------

VALID_CASES = list(iter_fixture_cases("valid"))
INVALID_CASES = list(iter_fixture_cases("invalid"))


def test_every_schema_has_fixture_coverage():
    valid_covered = {name for name, _ in VALID_CASES}
    invalid_covered = {name for name, _ in INVALID_CASES}
    need = set(STAGE_SCHEMAS) - {"policy"}   # policy 的正例 = policies/policy.yaml 本体（见下）
    assert need <= valid_covered, f"缺正例: {need - valid_covered}"
    assert set(STAGE_SCHEMAS) <= invalid_covered, f"缺负例: {set(STAGE_SCHEMAS) - invalid_covered}"


@pytest.mark.parametrize("name,case", VALID_CASES, ids=[f"{n}/{c.stem}" for n, c in VALID_CASES])
def test_valid_fixture_passes(name, case):
    make_validator(name).validate(load_json(case))


def iter_errors_deep(validator, instance):
    """展平校验错误（含 oneOf/anyOf 的 context 子错误），供负例钉扎匹配。"""
    stack = list(validator.iter_errors(instance))
    while stack:
        err = stack.pop()
        yield err
        stack.extend(err.context or [])


@pytest.mark.parametrize("name,case", INVALID_CASES, ids=[f"{n}/{c.stem}" for n, c in INVALID_CASES])
def test_invalid_fixture_rejected(name, case):
    """负例必须被拒，且失败原因命中同名 .expect 钉扎（防 fixture 因无关笔误失效而测试空转）。

    每个负例 fixture 必须配 <case>.expect：内容为应出现在错误 json_path 或 message
    里的子串（即"这个负例到底测的是哪条约束"）。
    """
    validator = make_validator(name)
    instance = load_json(case)
    with pytest.raises(ValidationError):
        validator.validate(instance)

    expect_file = case.with_suffix(".expect")
    assert expect_file.exists(), f"负例缺钉扎文件: {expect_file.name}"
    needle = expect_file.read_text(encoding="utf-8").strip()
    haystacks = [f"{e.json_path} {e.message}" for e in iter_errors_deep(validator, instance)]
    assert any(needle in h for h in haystacks), (
        f"负例 {case.name} 未因预期约束被拒（expect={needle!r}）；实际错误：{haystacks[:5]}"
    )


# ---------------------------------------------------------------------------
# 3. policy.yaml：解析 + 过 schema（运行前提 #4）
# ---------------------------------------------------------------------------

def test_policy_yaml_parses_and_validates():
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        policy = yaml.safe_load(f)
    make_validator("policy").validate(policy)


def test_policy_defaults_match_appendix_c_spotchecks():
    """抽核与附录 C 的关键默认值一致（防手滑改默认；全量对齐由 schema 结构保证）。"""
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        p = yaml.safe_load(f)
    assert p["acquisition"] == {"w1": 0.35, "w2": 0.25, "w3": 0.30, "c": 0.10}
    assert p["budget"] == {"B0": 5, "doubling_period_m": 8, "B_max": 40}
    assert p["flow"]["retry"] == {
        "artifact_parse": 2, "plan_review": 2,
        "bundle_code_review": 3, "bundle_result_review": 3,
    }
    assert p["idea"]["sd_threshold"] == 6
    assert p["tree_guard"] == {
        "max_decompose_depth": 4, "max_children_per_node": 6, "max_open_questions": 30,
    }
    assert p["interaction_request"]["max_items_per_request"] == 10


# ---------------------------------------------------------------------------
# 4. goal_brief.md：frontmatter 契约（运行前提 #2；缺失/非法 → 启动失败）
# ---------------------------------------------------------------------------

def parse_goal_brief(path: Path) -> dict:
    """解析研究目标书 frontmatter（§4.6.7 机械校验的最小实现；CP1.3 编排器复用同规则）。

    返回 frontmatter dict；不合契约即抛 ValueError（对应「启动失败并报错」）。
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("goal_brief 缺 YAML frontmatter（须以 --- 开头）")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("goal_brief frontmatter 未闭合（缺结尾 ---）")
    front = yaml.safe_load(text[4:end + 1])
    if not isinstance(front, dict) or "predicate_json" not in front:
        raise ValueError("goal_brief frontmatter 缺 predicate_json 字段")
    predicate = front["predicate_json"]
    if not isinstance(predicate, dict):
        raise ValueError("predicate_json 须为合法 JSON 对象")
    json.dumps(predicate)   # 可 JSON 序列化 = 合法 JSON（YAML 特有类型在此被拒）
    body = text[end + 5:].strip()
    if not body:
        raise ValueError("goal_brief 缺中文正文")
    return front


def test_goal_brief_frontmatter_valid():
    front = parse_goal_brief(SYSTEM_ROOT / "input" / "goal_brief.md")
    assert front["predicate_json"]["metric_id"] == "accuracy"


def test_goal_brief_negative_missing_predicate(tmp_path):
    bad = tmp_path / "goal_brief.md"
    bad.write_text("---\ntitle: 没有谓词\n---\n\n正文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="predicate_json"):
        parse_goal_brief(bad)


def test_goal_brief_negative_no_frontmatter(tmp_path):
    bad = tmp_path / "goal_brief.md"
    bad.write_text("# 只有正文\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_goal_brief(bad)
