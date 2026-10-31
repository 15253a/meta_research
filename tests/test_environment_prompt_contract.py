"""Exercise the shipped Root/Stage guidance and its available resource tools."""
from __future__ import annotations

import json
from pathlib import Path
import re
from importlib.resources import files

import pytest

from meta_research.bundle_skill import _bundle_skill_resources
from meta_research.idea_skill import _idea_skill_resources
from meta_research.plan_skill import _plan_skill_resources
from meta_research.reasoning_skill import _reasoning_skill_resources
from meta_research.root_capabilities import (
    ROOT_AGENT_KINDS,
    ROOT_CAPABILITY_ENTRY_PATHS,
    root_capability_profile,
)
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from meta_research.target_execution_contract import target_execution_skill_text


def _root_instructions(root_kind, entry_path="initial"):
    argv = root_capability_profile(root_kind).codex_arguments(entry_path=entry_path)
    encoded = next(value.split("=", 1)[1] for value in argv
                   if value.startswith("developer_instructions="))
    return json.loads(encoded)


def _root_reference(root_kind, name, entry_path="initial"):
    instructions = _root_instructions(root_kind, entry_path)
    assert f"references/{name}" in instructions
    match = re.search(r"^Shared reference root: (.+)$", instructions, re.MULTILINE)
    assert match, "reference pointers must identify the installed package root"
    root = Path(match.group(1))
    assert root.is_absolute()
    body = (root / "references" / name).read_text(encoding="utf-8")
    assert body.strip() and body not in instructions
    return body


@pytest.mark.parametrize("root_kind", ROOT_AGENT_KINDS)
@pytest.mark.parametrize("entry_path", ROOT_CAPABILITY_ENTRY_PATHS)
def test_every_root_launch_pairs_six_discovery_entries_with_actual_tools(root_kind, entry_path):
    if root_kind == "acquisition":
        # Acquisition receives a concrete download task, not Cycle discovery duties.
        instructions = _root_instructions(root_kind, entry_path)
        assert "references/research-navigation.md" not in instructions
        assert "references/cycle-work.md" not in instructions
        human = _root_reference(root_kind, "human-guidance.md", entry_path)
        _root_reference(root_kind, "materials-and-custody.md", entry_path)
        assert "human_request.open" in human
        assert "human_request.open" in ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
        return
    instructions = _root_reference(root_kind, "research-navigation.md", entry_path)
    available = ROOT_AGENT_SEMANTIC_OPERATION_IDS[root_kind]
    for operation in (
        "research_graph.questions.page",
        "research_graph.baselines.page",
        "research_graph.datasets.page",
        "research_graph.environments.page",
        "research_memory.literature.page",
        "human_request.read",
        "research_memory.content.read",
        "research_graph.environments.register",
        "research_graph.environments.reference",
    ):
        assert operation in instructions
        assert operation in available
    assert "六入口" in instructions


def test_root_guidance_separates_resource_knowledge_conditions_and_current_availability():
    instructions = _root_reference("target", "research-navigation.md")
    # These are research/authorization boundaries, not a prescribed paragraph layout.
    for boundary in (
        "按真实来源直接登记",
        "无数字材料时可为空",
        "现实资源无需补造构建 Target",
        "描述或所引用数字内容变化",
        "一次启动或选卡本身无需新建资源快照",
        "登记和用途记录不改变本次运行条件、使用权限或资源实际状态",
        "使用前按任务需要核实资源可用性",
        "说明文件 hash 只标识说明",
        "本 Target 新建或改动的资源先沿真实 Baseline／Variant／VariantRun 保存并正式接纳",
        "已有资源的直接登记记录其既有来源",
        "不能代替本次成果的正式交接",
        "未完成 Target 的中间产物不提前发布或消费",
    ):
        assert boundary in instructions


def test_cross_quest_guidance_is_an_explicit_environment_reference_not_global_discovery():
    instructions = _root_reference("reasoning", "research-navigation.md")
    for boundary in (
        "已知另一 Quest 的精确 environment_ref",
        "每个数字 binding 在目标 Quest 的正式接纳",
        "接纳引用后才从当前 Quest 的 page/read 读取",
        "不据此遍历其他 Quest 或相邻目录",
        "其他资产仍沿原 Owner 权限与接纳边界",
    ):
        assert boundary in instructions
    assert "另一 Quest 的资产不进入本次发现、读取或采用" not in instructions


def test_root_navigation_routes_operation_readers_before_reading_content():
    instructions = _root_reference("plan", "research-navigation.md")
    assert "reader 含 operation 时调用其指明的工具及参数" in instructions
    assert "共用正文 reader 的 source_ref／version_ref 才交给 research_memory.content.read" in instructions
    assert "摘要里的 reader 直接交给 research_memory.content.read" not in instructions


@pytest.mark.parametrize("load_resources", (
    _idea_skill_resources,
    _plan_skill_resources,
    _bundle_skill_resources,
    _reasoning_skill_resources,
))
def test_shipped_stage_resources_route_shared_entry_rules_and_preserve_independent_review(load_resources):
    resources = load_resources()
    assert "六入口" in resources["SKILL.md"]
    assert "research-navigation.md" in resources["references/research-guidance.md"]
    assert "research_graph.environments.page" in resources["references/research-navigation.md"]
    instructions = "\n".join(resources.values())
    assert "五入口" not in instructions
    assert "独立子智能体" in instructions
    assert "独立审阅由子 Agent 对实际草稿与必要依据" in resources["references/research-guidance.md"]
    assert "最终取舍由根负责" in resources["references/research-guidance.md"]


@pytest.mark.parametrize("load_resources", (_bundle_skill_resources, _reasoning_skill_resources))
def test_shipped_handoff_guidance_carries_both_resource_candidates_and_checks_admission(load_resources):
    instructions = "\n".join(load_resources().values())
    for term in ("dataset_candidates", "environment_candidates", "completion manifest",
                 "RM binding", "Target", "正式接纳", "读回", "同一整理核验环节"):
        assert term in instructions
    assert "现有设备" in instructions or "已有设备" in instructions


def test_target_candidate_pointer_reaches_the_packaged_formal_handoff():
    instructions = target_execution_skill_text()
    assert "六入口" in instructions
    assert "Dataset／Environment 候选" in instructions
    assert "references/formal-work.md" in instructions
    formal = files("meta_research").joinpath(
        "skills", "target-execution", "references", "formal-work.md"
    ).read_text(encoding="utf-8")
    for term in ("dataset_candidates", "environment_candidates", "artifact_path",
                 "Baseline → Variant → VariantRun", "接纳前只交候选",
                 "本 Target 新建或改动的成果先走这里的候选交接", "设备本体按真实来源表达"):
        assert term in formal
