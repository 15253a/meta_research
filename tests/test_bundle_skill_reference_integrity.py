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
        "SKILL.md",
        "references/contract.md",
        "references/owner-operations.md",
    )
    assert _bundle_skill_instruction_resources() == resources
    assert not any(name.startswith("scripts/") for name in resources)


def test_packaged_skill_retains_the_target_root_lifecycle_contract() -> None:
    skill = _text("SKILL.md")
    expected_sections = (
        "## 1. 锁定 Bundle 调用闭包",
        "## 2. 建立滚动策略",
        "## 3. Claim 独立 Target 根 Session",
        "## 4. 在根 Session 内完成 Target 循环",
        "## 5. 冻结最终交接",
        "## 6. 在冻结后请求 Owner 接纳",
        "## 7. 收口 Bundle",
    )
    assert all(section in skill for section in expected_sections)
    for invariant in (
        "per-Target single-flight",
        "结果驱动修改",
        "TargetCompletionHandoff",
        "stdout／stderr",
        "聚焦子智能体",
        "TargetCommit",
        "ExhaustionProposal",
        "逐 ExperimentKey",
    ):
        assert invariant in skill


def test_packaged_references_keep_root_ownership_and_owner_boundaries() -> None:
    contract = _text("references/contract.md")
    owner_operations = _text("references/owner-operations.md")
    assert len(contract.splitlines()) >= 280
    assert len(owner_operations.splitlines()) >= 174
    for section in (
        "## 滚动策略与 Target",
        "## Target 根 Session",
        "## Target daemon",
        "## 结果驱动循环",
        "## TargetCompletionHandoff",
        "## 交接后的 Owner 接纳",
        "## Web 执行观察",
    ):
        assert section in contract
    for invariant in (
        "claim_target_root_session",
        "reconcile_target_root_session",
        "forward_target_events",
        "query_target_root_completion_evidence",
        "根 Session 独占实现、训练、结果驱动修改和 completion handoff",
        "长训练观察子智能体只 tail 进程与日志并回报",
        "stdout Web projection 从不成为 Metric authority",
    ):
        assert invariant in owner_operations
