"""CP1.1 契约层自验：schema 元校验 + 正/负例 + policy / goal_brief 启动契约。

对应验收锚点：
- 四阶段产物 schema 是 M0 验收「逐项过 schema validator」的校验基准（第三部分 §7.1 M0 行）；
- policy.yaml 解析且过 schema = 运行前提 #4（§7.5）；
- goal_brief.md frontmatter 含合法 predicate_json = 运行前提 #2（缺失/非法 → 启动失败，§4.6.7）。
"""
import pytest
import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from conftest import (
    SCHEMAS_DIR, SYSTEM_ROOT,
    iter_fixture_cases, load_json, load_schema, make_validator,
)

STAGE_SCHEMAS = [
    # 四阶段最终产物 + policy（Gate 校验对象）
    "idea_set", "plan", "bundle_target",
    "answer", "tree_ops", "selection",
    "policy",
    # bundle 编译产物：机器执行契约（步⑧ CP8.1；plan 保持抽象，命令只在此，executor=orchestrator/manifest.py）
    "execution_manifest",
    # bundle 双评审裁决（步⑧ CP8.3；judge 产，JudgeProvider 落 runner_call+DECISION）
    "review_verdict",
    # sidecar（非 Gate 产物：schema 校验后经 interaction_request_create，不入研究库，§6.11）
    "resource_request",
    # interaction_query runner 候选回执：中介再按发布卡逐 path 交叉核值后落 interaction_reply
    "interaction_reply_candidate",
    # 过程产物（runner_call 级契约：生成草稿 / 判官 / 评审输出，驱动器消费）
    "idea_set_draft", "idea_audit", "plan_review",
]


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


def test_resource_request_metadata_bounds_match_runtime_quota():
    """请求 schema 自身限制 prompt 元数据；items 上限须与 policy 默认的 10 对齐。"""
    schema = load_schema("resource_request")
    props = schema["properties"]
    item_props = props["items"]["items"]["properties"]
    assert props["items"]["maxItems"] == 10
    assert item_props["expected_files"]["maxItems"] == 16
    assert item_props["attempted_paths"]["maxItems"] == 8

    validator = make_validator("resource_request")
    max_item = {
        "kind": "dataset",
        "desc": "D" * 1024,
        "expected_files": ["E" * 512] * 16,
        "attempted_paths": ["P" * 1024] * 8,
        "failure_reason": "F" * 1024,
        "dest_hint": "H" * 512,
    }
    validator.validate({"summary_md": "S" * 2048, "items": [max_item] * 10})

    invalid = [
        {"summary_md": "S" * 2049, "items": [max_item]},
        {"summary_md": "ok", "items": [max_item] * 11},
        {"summary_md": "ok", "items": [{**max_item, "expected_files": ["x"] * 17}]},
        {"summary_md": "ok", "items": [{**max_item, "attempted_paths": ["x"] * 9}]},
        {"summary_md": "ok", "items": [{**max_item, "desc": "D" * 1025}]},
        {"summary_md": "bad\x01control", "items": [max_item]},
        {"summary_md": "ok", "items": [{**max_item, "failure_reason": "bad\nline"}]},
    ]
    for request in invalid:
        with pytest.raises(ValidationError):
            validator.validate(request)


# ---------------------------------------------------------------------------
# 3. policy.yaml：解析 + 过 schema（运行前提 #4）
# ---------------------------------------------------------------------------

def test_policy_yaml_parses_and_validates():
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        policy = yaml.safe_load(f)
    make_validator("policy").validate(policy)


def test_policy_plan_review_semantic_rounds_are_capped_at_two():
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        policy = yaml.safe_load(f)
    policy["flow"]["retry"]["plan_review"] = 3
    with pytest.raises(ValidationError):
        make_validator("policy").validate(policy)


def test_policy_budget_price_required_and_positive_when_session_limit_enabled():
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        base = yaml.safe_load(f)
    validator = make_validator("policy")
    for budget in (
            {k: v for k, v in base["budget"].items() if k != "session_max"},
            {k: v for k, v in base["budget"].items() if k != "price_per_1k_tokens"},
            {**base["budget"], "price_per_1k_tokens": 0}):
        with pytest.raises(ValidationError):
            validator.validate({**base, "budget": budget})
    # 明确关闭 session 网时允许零价（仍可保留零成本审计行）。
    validator.validate({**base, "budget": {**base["budget"], "session_max": None,
                                             "price_per_1k_tokens": 0}})


def test_policy_defaults_match_appendix_c_spotchecks():
    """抽核与附录 C 的关键默认值一致（防手滑改默认；全量对齐由 schema 结构保证）。"""
    with open(SYSTEM_ROOT / "policies" / "policy.yaml", encoding="utf-8") as f:
        p = yaml.safe_load(f)
    assert p["acquisition"] == {"w1": 0.35, "w2": 0.25, "w3": 0.30, "c": 0.10}
    assert p["budget"] == {"B0": 5, "doubling_period_m": 8, "B_max": 40, "session_max": 100000,
                           "price_per_1k_tokens": 0.3}  # session_max: M6 CP7.1 全局安全网；price: 步⑩ CP10.2 记账汇率
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
# 4. goal_brief.md：frontmatter 契约（运行前提 #2）——唯一实现在 orchestrator.goalbrief，
#    解析与正负例测试见 tests/test_orchestrator.py（防两份规则漂移）。
