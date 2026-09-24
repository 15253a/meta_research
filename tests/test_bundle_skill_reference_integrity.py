from __future__ import annotations

from importlib.resources import files

from meta_research.bundle_skill import (
    _bundle_skill_instruction_resources,
    _bundle_skill_resources,
)


def _text(relative: str) -> str:
    resource = files("meta_research.skills.bundle_stage")
    for part in relative.split("/"):
        resource = resource / part
    return resource.read_text(encoding="utf-8")


def test_runtime_hashes_and_prompts_with_the_same_prose_contract() -> None:
    resources = _bundle_skill_resources()

    assert tuple(resources) == (
        "research-guidance.md",
        "SKILL.md",
        "references/contract.md",
        "references/owner-operations.md",
    )
    assert _bundle_skill_instruction_resources() == resources
    assert not any(name.startswith("scripts/") for name in resources)


def test_packaged_skill_routes_target_execution_to_its_actual_contract() -> None:
    skill = _text("SKILL.md")
    # Main instructions preserve coordination and route execution detail to the
    # runtime's actual contract; document size and heading counts are not gates.
    for authority in ("TargetRun", "TargetCommit", "Owner"):
        assert authority in skill
    for contract in (
        "target-execution", "measurement_contract", "result_schema"
    ):
        assert contract in skill


def test_packaged_references_keep_root_ownership_and_owner_boundaries() -> None:
    contract = _text("references/contract.md")
    owner_operations = _text("references/owner-operations.md")
    # The prose may be reorganized; retain the current execution and acceptance
    # concepts rather than obsolete section names or root-exclusive child rules.
    for concept in (
        "TargetRun", "formal_runs", "TargetCommit", "MetricResult",
        "StageCommit", "checkpoint_policy", "VariantRun", "EvaluationAttempt",
    ):
        assert concept in contract
    for invariant in (
        "Target 根管实际实施与最终交接",
        "子智能体在明确任务及所授权限内",
        "未接纳 Target 中间产物仍留工作区",
        "独立审阅最终策略及交接",
        "正式结果仅来自 Owner 验证的接纳链",
    ):
        assert invariant in owner_operations
