"""Keep the production launch acceptance matrix aligned with the root catalog.

The referenced tests exercise actual adapter entry points and observe provider
arguments/recovery results. This guard does not replace those behavioral tests:
it makes a new root or a removed/renamed acceptance test require a matrix update.
Entry labels describe reachable adapter operations, not every theoretical
combination of root kind and lifecycle event.
"""
from __future__ import annotations

import ast
from importlib import import_module
import inspect
from pathlib import Path

import pytest

from meta_research.root_capabilities import ROOT_AGENT_KINDS


PRODUCTION_LAUNCH_COVERAGE = {
    "idea": (
        "meta_research.idea_skill.CodexIdeaSkillAdapter",
        {
            "initial/primary": "test_idea_skill_contract.py::test_production_adapter_runs_packaged_skill_with_canonical_capabilities",
            "resume/review-and-continuation": "test_idea_skill_contract.py::test_system_mcp_changes_apply_to_review_and_continuation_in_same_native_session",
            "recovery/frozen-and-historical": "test_idea_skill_contract.py::test_system_mcp_prelaunch_recovery_keeps_frozen_argv_and_legacy_hash",
        },
    ),
    "plan": (
        "meta_research.plan_skill.CodexPlanSkillAdapter",
        {
            "initial/primary-and-resume/review": "test_plan_skill_adapter.py::test_production_adapter_uses_one_native_root_with_advisory_finalization",
            "successor/primary-and-review": "test_plan_skill_adapter.py::test_owner_feedback_is_present_in_both_successor_prompts",
            "recovery/review": "test_plan_skill_adapter.py::test_durable_plan_review_shape_failure_is_terminal_and_not_replayed",
        },
    ),
    "bundle": (
        "meta_research.bundle_skill.CodexBundleSkillAdapter",
        {
            "successor/primary": "test_bundle_skill_adapter.py::test_completion_rejection_feedback_reaches_bundle_primary_prompt",
            "initial/primary-and-resume/dispatch": "test_bundle_skill_adapter.py::test_production_adapter_freezes_skill_without_requiring_child_choreography",
            "recovery/dispatch": "test_bundle_skill_adapter.py::test_bundle_transport_limits_are_sealed_and_reused_on_durable_restart",
        },
    ),
    "reasoning": (
        "meta_research.reasoning_skill.CodexReasoningSkillAdapter",
        {
            "initial/primary-and-resume/review": "test_reasoning_skill_adapter.py::test_production_adapter_uses_one_session_and_scoped_resident_mcp",
            "resume/autonomous": "test_reasoning_skill_adapter.py::test_completion_rejection_feedback_reaches_reasoning_autonomous_resume",
            "recovery/autonomous": "test_reasoning_skill_adapter.py::test_restart_reconciles_cancelled_autonomous_resume_operation",
        },
    ),
    "writing": (
        "meta_research.writing_skill.CodexWritingSkillAdapter",
        {
            "initial/primary-and-resume/review": "test_writing_skill_adapter.py::test_type_specific_skill_resource_uses_the_same_resumable_session_seam",
            "recovery/review": "test_writing_skill_adapter.py::test_review_spool_replays_after_crash_rotates_only_attempt_and_fence",
        },
    ),
    "companion": (
        "meta_research.companion.CodexCompanionAdapter",
        {
            "initial/pre-quest": "test_external_root_resident_mcp.py::test_pre_quest_companion_turn_remains_without_resident_channel",
            "resume/reply": "test_quest_drafting_adapters.py::test_companion_guides_action_and_resumes_with_normal_tools",
            "resume/proposal-fork": "test_quest_drafting_adapters.py::test_companion_proposal_fork_returns_public_content_without_replacing_root_session",
        },
    ),
    "acquisition": (
        "meta_research.acquisition_root.CodexAcquisitionRootAdapter",
        {
            "initial/preflight": "test_external_root_resident_mcp.py::test_unmanaged_acquisition_turn_remains_without_resident_channel",
            "resume/batch": "test_external_root_resident_mcp.py::test_quest_bound_acquisition_batch_uses_exact_resident_operation_tree",
            "owner/batch": "test_external_root_resident_mcp.py::test_production_acquisition_owner_injects_scope_into_actual_root_adapter",
        },
    ),
    "deepfetch": (
        "meta_research.deepfetch.CodexDeepFetchAdapter",
        {
            "initial-and-resume/direct": "test_system_mcp_execution.py::test_deepfetch_direct_loads_external_tools_without_internal_channel",
            "initial-and-recovery/durable": "test_system_mcp_execution.py::test_deepfetch_recovery_segments_freeze_tools_but_next_turn_samples_latest",
            "recovery/historical": "test_system_mcp_execution.py::test_historical_deepfetch_pending_segments_keep_original_snapshot_absence",
        },
    ),
    "target": (
        "meta_research.harness_adapters.CodexHarnessAdapter",
        {
            "initial-and-resume/native-session": "test_system_mcp_execution.py::test_target_freezes_system_tools_per_operation_and_updates_same_session",
            "wake/native-session": "test_harness_target_root.py::test_target_root_lifecycle_chooses_first_then_resume_in_one_native_session",
            "recovery/historical": "test_system_mcp_execution.py::test_historical_target_spool_replays_original_argv_and_hash_after_registration",
            "recovery/credential-rotation": "test_system_mcp_execution.py::test_target_terminal_replay_does_not_depend_on_current_external_credentials",
        },
    ),
}


def test_production_launch_matrix_covers_every_authoritative_root() -> None:
    assert set(PRODUCTION_LAUNCH_COVERAGE) == set(ROOT_AGENT_KINDS), (
        "Update the real adapter launch coverage when the root catalog changes"
    )


@pytest.mark.parametrize("root_kind", sorted(PRODUCTION_LAUNCH_COVERAGE))
def test_production_launch_matrix_references_real_adapters_and_tests(root_kind: str) -> None:
    qualified_adapter, entries = PRODUCTION_LAUNCH_COVERAGE[root_kind]
    module_name, _, class_name = qualified_adapter.rpartition(".")
    adapter = getattr(import_module(module_name), class_name)
    assert inspect.isclass(adapter)
    assert "system_mcp_registry" in inspect.signature(adapter).parameters
    assert entries, f"{root_kind} needs a production launch contract"
    tests_root = Path(__file__).parent
    for entry_path, nodeid in entries.items():
        filename, test_name = nodeid.split("::")
        module = ast.parse((tests_root / filename).read_text(encoding="utf-8"))
        functions = {
            node.name for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert test_name.startswith("test_") and test_name in functions, (
            f"{root_kind} {entry_path} references missing acceptance test {nodeid}"
        )
