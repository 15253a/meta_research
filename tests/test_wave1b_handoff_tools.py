"""Regression tests for Wave 1B (D1, D8b, D2, D3/D5, D9, T1/RG1, T2, RG2/WS1)."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.baseline_identity import (
    resolve_baseline_method_identity,
    verify_baseline_method_identity,
)
from meta_research.bundle_target_contract import (
    formal_target_candidate_from_dict,
    formal_target_candidate_to_dict,
    normalized_completion_contract_from_dict,
)
from meta_research.context_presentation import (
    CONTEXT_VIEW_MAX_BYTES,
    stage_context_json,
    stage_context_view,
    stage_position_prompt,
)
from meta_research.external_mcp import (
    ExternalMcpOperationSnapshot,
    compose_external_mcp_prompt,
    split_external_mcp_prompt,
)
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.plan_contract import (
    PlanContractError,
    validate_current_plan_evidence_accounting,
    validate_plan_document,
)
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from meta_research.research_content import discover_questions
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from meta_research.stage_context_access import read_stage_context, stage_context_operations
from meta_research.target_execution_contract import main as check_result_main
from meta_research.tool_navigation import common_operations, operation_summary


# ---------------------------------------------------------------------------
# D1: Reasoning audit-closure compression without mutating context_pack_hash
# ---------------------------------------------------------------------------


def test_d1_reasoning_view_compresses_closures_within_budget_preserving_hash():
    huge_receipt = {
        "receipt_ref": "rcpt:1",
        "payload_hash": "a" * 64,
        "issuer": "research_graph",
        "kind": "target_commit",
        "signature_chain": "sig-" * 2000,
    }
    targets = [
        {
            "target_commit_ref": f"tc:{i}",
            "receipt": dict(huge_receipt),
            "target_spec": {"execution_contract": {"blob": "x" * 6000}},
            "result": {"summary": f"Target {i} finding."},
        }
        for i in range(15)
    ]
    pack = {
        "schema_ref": "meta-research/reasoning-context-pack/v1",
        "cycle_ref": "cycle:1",
        "plan_evidence_input": {"kind": "accepted", "summary": "plan science"},
        "upstream_stage_closure": [
            {
                "stage": "bundle",
                "commit_ref": "commit:bundle-1",
                "receipt": dict(huge_receipt),
                "closure": {
                    "notes": "Bundle handoff note: RSP/EDA verified.",
                    "audit_blob": "b" * 10000,
                },
            }
        ],
        "accepted_target_commit_closures": targets,
        "current_target_evidence_closure": [
            {"evidence_ref": f"ev:{i}", "receipt": dict(huge_receipt), "blob": "z" * 4000}
            for i in range(15)
        ],
    }
    before_hash = canonical_hash(pack)
    before_snapshot = deepcopy(pack)

    view = stage_context_view(
        "reasoning", pack, context_pack_ref="ctx:reasoning", context_pack_hash=before_hash
    )

    assert canonical_hash(pack) == before_hash
    assert pack == before_snapshot
    assert view["source_context_pack_hash"] == before_hash
    optional_payload = {
        **view,
        "sections": {
            k: v
            for k, v in view["sections"].items()
            if k not in {"plan_evidence_input", "prior_accepted_bindings", "typed_skip_basis_refs_by_stage", "cycle_position"}
        },
    }
    assert len(canonical_json(optional_payload).encode("utf-8")) <= CONTEXT_VIEW_MAX_BYTES
    assert view["section_delivery"]["accepted_target_commit_closures"] in {"preview", "omitted"}
    assert view["section_delivery"]["upstream_stage_closure"] in {"preview", "omitted"}
    assert view["section_delivery"]["current_target_evidence_closure"] in {"preview", "omitted"}
    assert view["section_delivery"]["plan_evidence_input"] == "complete"
    assert "current_handoff_notes" in view


# ---------------------------------------------------------------------------
# D8b: Creation understanding -> Idea, omit empty MCP block, deduplicate Goal
# ---------------------------------------------------------------------------


def test_d8b_idea_receives_creation_understanding_and_deduplicated_goal(tmp_path):
    import test_idea_skill_contract as idea_fixtures

    goal_text = "基于 100 Hz 多通道生理信号完成四次刺激事件特征提取与条件比较。"
    understanding = {
        "verified_facts": [
            {
                "ref": "stmt:1",
                "statement": "CSV 含 15000 样本与 ECG/RSP/EDA/Photosensor 四通道。",
                "sources": [{"material_key": "mat:1", "location": "README.md:1-10"}],
            }
        ],
    }
    runner = idea_fixtures._SequenceRunner([{"outcome": idea_fixtures._idea_set()}])
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(idea_fixtures._fake_codex_executable(tmp_path / "codex", read_all_input=True)),
        process_runner=runner,
    )
    base_req = idea_fixtures._request(runtime_binding=adapter.runtime_binding())
    context_pack = {
        **base_req.context_pack,
        "cycle_ref": "cycle:1",
        "cycle_position": {
            "cycle_ref": "cycle:1",
            "round": 1,
            "cycle_focus": goal_text,
            "focus_source": {"kind": "confirmed_creation"},
        },
        "goal_revision_ref": "quest_goal_revision:1",
        "quest_goal_revision": {
            "kind": "QuestGoalRevision",
            "quest_ref": "quest:1",
            "goal_revision_ref": "quest_goal_revision:1",
            "goal": {"goal": goal_text, "completion_criteria": "4 rows no NaN"},
        },
    }
    pack_hash = canonical_hash(context_pack)
    req = replace(
        base_req,
        context_pack=context_pack,
        context_pack_hash=pack_hash,
        creation_understanding=understanding,
    )
    adapter.generate_draft(req)
    prompt = runner.calls[0][1]

    # 1. Creation understanding prose routed directly into Idea required_material without changing context_pack_hash.
    view_line = next(line for line in prompt.splitlines() if line.startswith("provider_context_view="))
    view = json.loads(view_line.split("=", 1)[1])
    assert view["source_context_pack_hash"] == pack_hash
    assert canonical_hash(context_pack) == pack_hash
    assert view["required_material"]["creation_understanding"] == understanding

    # 2. Goal text appears exactly ONCE on initial turn.
    assert prompt.count(goal_text) == 1
    assert "当前 cycle focus 同本次冻结 Quest Goal" in prompt


def test_d8b_empty_external_mcp_omitted_on_initial_turn():
    snapshot = ExternalMcpOperationSnapshot(
        operation_key="a" * 64,
        root_kind="idea",
        configuration_revision="b" * 64,
        task_prompt="Original work",
        services=(),
        snapshot_hash="c" * 64,
        recovery_guidance="recovery",
    )
    composed = compose_external_mcp_prompt("Original work", snapshot, resume=False)
    assert composed == "Original work"
    rendered, remainder = split_external_mcp_prompt(composed)
    assert rendered == ""
    assert remainder == "Original work"


# ---------------------------------------------------------------------------
# D2: Plan.evidence_accounting optional (no missing_target_refs rejection)
# ---------------------------------------------------------------------------


def test_d2_plan_evidence_accounting_optional_and_preserves_entry_checks():
    import test_plan_skill_contract as plan_contract_fixtures

    doc_omitted = plan_contract_fixtures._plan_document()
    doc_omitted.pop("evidence_accounting", None)
    validate_plan_document(
        doc_omitted,
        question_ref="question_1",
        idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1",
        context_pack_hash="6" * 64,
        accepted_idea_set=plan_contract_fixtures._IDEA_SET,
        evidence_by_ref={"evidence_1": plan_contract_fixtures._EVIDENCE_REF},
        evidence_reference_revision=1,
    )

    doc_empty = plan_contract_fixtures._plan_document()
    doc_empty["evidence_accounting"] = []
    # Empty or partial evidence_accounting must NOT raise plan_evidence_accounting_incomplete.
    validate_plan_document(
        doc_empty,
        question_ref="question_1",
        idea_set_ref="idea_set_1",
        context_pack_ref="plan_context_1",
        context_pack_hash="6" * 64,
        accepted_idea_set=plan_contract_fixtures._IDEA_SET,
        evidence_by_ref={"evidence_1": plan_contract_fixtures._EVIDENCE_REF},
        evidence_reference_revision=1,
    )
    validate_current_plan_evidence_accounting(
        doc_empty,
        current_catalog=[plan_contract_fixtures._EVIDENCE_REF],
        selected_catalog=[],
    )

    # Explicitly invalid entry in provided evidence_accounting MUST still be rejected.
    doc_bad = plan_contract_fixtures._plan_document()
    doc_bad["evidence_accounting"] = [
        {
            "evidence_ref": "evidence_unknown",
            "target_commit_ref": "tc_unknown",
            "status": "not_used",
            "reason": "Bogus unknown entry.",
        }
    ]
    with pytest.raises(PlanContractError, match="plan_evidence_accounting_invalid"):
        validate_current_plan_evidence_accounting(
            doc_bad,
            current_catalog=[plan_contract_fixtures._EVIDENCE_REF],
            selected_catalog=[],
        )


def test_d2_plan_adapter_allows_null_or_omitted_evidence_accounting(tmp_path):
    import test_plan_skill_adapter as plan_fixtures

    plan = deepcopy(plan_fixtures._plan())
    plan["evidence_accounting"] = None
    plan["selected_evidence_bindings"] = []
    plan["answer_contract"].pop("answer_contract_hash", None)
    runner = plan_fixtures._SequenceRunner([{"plan": plan}])
    adapter = CodexPlanSkillAdapter(
        tmp_path / "plan-provider",
        executable=str(plan_fixtures._fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    request = plan_fixtures._request(runtime_binding=adapter.runtime_binding())
    draft = adapter.generate_draft(request)
    assert "evidence_accounting" not in draft.draft


# ---------------------------------------------------------------------------
# D3 / D5: Optional Bundle implementation_revision_ref & relaxed draft baseline
# ---------------------------------------------------------------------------


def test_d3_bundle_candidate_allows_omitted_or_null_implementation_revision_ref():
    import test_bundle_skill_adapter as bundle_fixtures

    plan = bundle_fixtures._plan_document()
    target_plan = bundle_fixtures._target_plan(plan, "a" * 64)
    completion = normalized_completion_contract_from_dict(
        target_plan["completion_contract"], plan_document=plan
    )
    raw = deepcopy(target_plan["initial_strategy_update"]["candidates"][0])
    raw["candidate"].pop("implementation_revision_ref")
    parsed_omitted = formal_target_candidate_from_dict(raw, completion_contract=completion)
    assert parsed_omitted.candidate.implementation_revision_ref is None

    raw_null = deepcopy(target_plan["initial_strategy_update"]["candidates"][0])
    raw_null["candidate"]["implementation_revision_ref"] = None
    parsed_null = formal_target_candidate_from_dict(raw_null, completion_contract=completion)
    assert parsed_null.candidate.implementation_revision_ref is None

    roundtripped = formal_target_candidate_to_dict(parsed_null, completion_contract=completion)
    assert roundtripped["candidate"]["implementation_revision_ref"] is None


def test_d5_bundle_draft_baseline_relaxed_and_strict_errors_report_exact_key_path(tmp_path):
    from test_public_bundle_stage import _bundle_runtime, _confirm_direct_quest

    runtime = _bundle_runtime(tmp_path / "d5-baseline")
    try:
        quest = _confirm_direct_quest(runtime)
        quest_ref = str(quest["quest_ref"])
        base_forward = {
            "method_key": "biosignals/neurokit-event",
            "method_version": "1",
            "method_contract": {"inputs": "100hz csv", "outputs": "4-event means"},
        }
        with runtime._database.write() as connection:
            ref1, created1 = resolve_baseline_method_identity(
                connection,
                forward={**base_forward, "draft_extra_hint": "sandbox hint"},
                quest_ref=quest_ref,
                accepted_at=1.0,
                draft=True,
            )
            assert created1 is True

            # Bundle draft with differing method_contract prose reuses registered baseline
            # without mutating immutable stored contract (Target registers actual method later).
            ref2, created2 = resolve_baseline_method_identity(
                connection,
                forward={
                    "method_key": "biosignals/neurokit-event",
                    "method_version": "1",
                    "method_contract": {"inputs": "100hz csv", "outputs": "revised draft text"},
                    "extra_draft_field": True,
                },
                quest_ref=quest_ref,
                accepted_at=2.0,
                draft=True,
            )
            assert ref2 == ref1
            assert created2 is False

            # Strict mode (Target finalization) STILL rejects extra keys and content conflict,
            # AND reports exact failing key path in error.details!
            with pytest.raises(OwnerConflict) as env_err:
                resolve_baseline_method_identity(
                    connection,
                    forward={**base_forward, "unexpected_param": 123},
                    quest_ref=quest_ref,
                    accepted_at=3.0,
                    draft=False,
                )
            assert env_err.value.code == "baseline_method_envelope_invalid"
            assert env_err.value.details is not None
            assert "unexpected_param" in str(env_err.value.details.get("path", ""))

            with pytest.raises(OwnerConflict) as conflict_err:
                resolve_baseline_method_identity(
                    connection,
                    forward={
                        **base_forward,
                        "method_contract": {"inputs": "different", "outputs": "different"},
                    },
                    quest_ref=quest_ref,
                    accepted_at=4.0,
                    draft=False,
                )
            assert conflict_err.value.code == "baseline_method_version_content_conflict"
            assert conflict_err.value.details is not None
            assert "method_contract" in str(conflict_err.value.details.get("path", ""))
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# D9: --check-result validates ALL formal_runs & Reasoning SKILL closure order
# ---------------------------------------------------------------------------


def test_d9_check_result_validates_second_formal_run(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "outputs" / "result.json"
    draft.parent.mkdir(parents=True)
    document = {
        "metrics": {"effect": 1.0},
        "formal_runs": [
            {"run_key": "run-1", "evaluations": [{"attempt_key": "eval-1", "metrics": {"effect": 1.0}}]},
            {"run_key": "run-2", "evaluations": [{"attempt_key": "eval-2", "metrics": {"wrong_key": 2.0}}]},
        ],
    }
    draft.write_text(canonical_json(document), encoding="utf-8")
    protocol = {
        "protocol_version_ref": "proto:1",
        "required_metric_keys": ["effect"],
        "optional_metric_keys": [],
    }
    assert check_result_main(["--check-result", canonical_json(protocol)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "metric_key_mismatch"
    assert out["run_key"] == "run-2"
    assert out["attempt_key"] == "eval-2"
    assert out["missing_metric_keys"] == ["effect"]
    assert out["unexpected_metric_keys"] == ["wrong_key"]


def test_d9_reasoning_skill_direct_candidate_completion_without_prior_human_request():
    skill_path = (
        Path(__file__).resolve().parent.parent
        / "src/meta_research/skills/reasoning_stage/SKILL.md"
    )
    text_body = skill_path.read_text(encoding="utf-8")
    assert "CandidateCompletion" in text_body
    assert "human_request.open" in text_body
    assert "直接提交" in text_body


# ---------------------------------------------------------------------------
# T1 / RG1 + T2 (assets.* split): Live ProductionRuntime gateway assertions
# ---------------------------------------------------------------------------


def test_t1_rg1_and_t2_live_production_gateway_summaries_catalog_and_assets(tmp_path):
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "live-gateway"),
        power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_power_probe=False,
        startup_harness_diagnostics=False,
    )
    try:
        gateway = runtime.harnesses._gateway
        all_ops = list(gateway._operations.values())
        assert len(all_ops) >= 95

        # 1. Zero bad summaries & zero Companion-only pollution across ALL registered operations.
        for op in all_ops:
            summary = str(operation_summary(op)["summary"])
            assert summary.strip(), f"Empty summary for {op.semantic_operation_id}"
            assert len(summary) <= 160, f"Overlong summary ({len(summary)}): {op.semantic_operation_id} -> {summary!r}"
            assert not summary.startswith("Companion may"), f"Companion pollution: {op.semantic_operation_id} -> {summary!r}"
            assert "Companion may" not in summary, f"Companion pollution: {op.semantic_operation_id} -> {summary!r}"

        # 2. T2: Distinct descriptions across all 7 research_memory.assets.* operations.
        asset_descs = {
            op.semantic_operation_id: op.description
            for op in all_ops
            if op.semantic_operation_id.startswith("research_memory.assets.")
        }
        assert len(asset_descs) == 7
        assert len(set(asset_descs.values())) == 7, "assets.* operations still share copy-pasted descriptions"

        # 3. RG1: All 6 RG entries visible in initial common_operations & overview.
        rg_six = {
            "research_graph.questions.page",
            "research_graph.baselines.page",
            "research_graph.datasets.page",
            "research_graph.environments.page",
            "research_graph.literature.page",
            "human_request.read",
        }
        hidden_plumbing = {
            "advancement_engine.bundle_stage_run.observe",
            "agent_runtime.bundle_run_binding.observe",
            "research_graph.target_launch_request.read",
            "agent_runtime.target_frontier.read",
            "agent_runtime.bundle_inbox.read",
            "agent_runtime.target_work.request",
            "advancement_engine.bundle_exhaustion.submit",
            "research_memory.implementation_content.accept",
            "research_memory.implementation_content.read",
            "advancement_engine.snapshot.read",
            "agent_runtime.snapshot.read",
            "human_collaboration.snapshot.read",
            "research_graph.snapshot.read",
            "research_memory.snapshot.read",
            "agent_runtime.host_compute.observe",
            "research_graph.quest_receipt.verify",
        }

        for role in ("idea", "plan", "bundle", "target", "reasoning", "companion"):
            conn, _ = gateway.issue_channel(
                run_ref=f"run-{role}",
                attempt_ref=f"att-{role}",
                root_session_ref=f"sess-{role}",
                fence_ref=f"fence-{role}",
                capability_binding_hash="b" * 64,
                operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[role],
                root_kind=role,
                phase="primary",
            )
            try:
                _, init_resp = gateway.dispatch(
                    conn.token, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
                )
                first_packet = json.loads(init_resp["result"]["instructions"].splitlines()[-1])
                common_ids = {item["operation_id"] for item in first_packet["common_operations"]}
                assert rg_six <= common_ids, f"{role} missing RG entries in common_operations: {rg_six - common_ids}"

                # Default catalog operations: 0 uncallable, 0 reconcile, 0 hidden plumbing.
                _, page_resp = gateway.dispatch(
                    conn.token,
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "tools.catalog.page",
                            "arguments": {"view": "operations", "limit": 100},
                        },
                    },
                )
                assert page_resp["result"]["isError"] is False
                items = page_resp["result"]["structuredContent"]["items"]
                ids = {item["operation_id"] for item in items}
                assert all(item["callable_in_current_session"] is True for item in items), role
                assert all(item["access_mode"] != "reconcile" for item in items), role
                assert not (ids & hidden_plumbing), (role, ids & hidden_plumbing)
                assert rg_six <= ids
                assert len(items) < len(ROOT_AGENT_SEMANTIC_OPERATION_IDS[role]) - 20, (
                    role,
                    len(items),
                    len(ROOT_AGENT_SEMANTIC_OPERATION_IDS[role]),
                )

                # Explicit view="all_contracts" still exposes full registered contracts (including reconcile & cross-role).
                _, all_resp = gateway.dispatch(
                    conn.token,
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "tools.catalog.page",
                            "arguments": {"view": "all_contracts", "limit": 100},
                        },
                    },
                )
                assert all_resp["result"]["isError"] is False
                all_items = all_resp["result"]["structuredContent"]["items"]
                assert any(item["access_mode"] == "reconcile" for item in all_items)
                print(
                    f"GATEWAY_METRIC role={role} default_visible={len(items)} "
                    f"all_contracts={len(all_items)} common_ops={len(common_ids)}"
                )
            finally:
                gateway.revoke_channel(conn.token)

        # 4. T2 schema defaults check on live ProductionRuntime gateway.
        schemas = {op.semantic_operation_id: op.input_schema for op in all_ops}
        assert set(schemas["research_memory.reasoning_evidence.read"]["properties"]) >= {"offset", "limit", "max_bytes"}
        assert schemas["research_graph.plan_evidence.page"].get("required", []) == []
        assert schemas["research_graph.plan_evidence.page"]["properties"]["offset"]["default"] == 0
        assert schemas["research_graph.plan_evidence.page"]["properties"]["limit"]["default"] == 32
        assert schemas["research_memory.plan_evidence.read"]["properties"]["offset"]["default"] == 0
        assert schemas["research_memory.plan_evidence.read"]["properties"]["limit"]["default"] == 8192
        assert set(schemas["research_memory.stage_context.read"]["required"]) == {"context_pack_ref", "source"}
        assert "offset" not in schemas["research_memory.stage_context.read"]["required"]
        assert "limit" not in schemas["research_memory.stage_context.read"]["required"]
        assert "path" not in schemas["research_memory.stage_context.read"]["required"]
        assert set(schemas["research_memory.research_notes.read"].get("required", [])) == {"source"}
        assert "offset" not in schemas["research_memory.research_notes.read"].get("required", [])
        assert "limit" not in schemas["research_memory.research_notes.read"].get("required", [])
        assert schemas["agent_runtime.bundle_inbox.read"].get("required", []) == []
        print(f"GATEWAY_SUMMARY_TOTAL total_ops={len(all_ops)} bad_summaries=0 companion_pollution=0")
    finally:
        runtime.close()


# ---------------------------------------------------------------------------
# T2: Bounded reasoning_evidence.read & safe default offset/limit on readers
# ---------------------------------------------------------------------------


def test_t2_reasoning_evidence_bounded_and_readers_have_safe_defaults(tmp_path):
    from meta_research.semantic_mcp import SemanticMcpGateway
    from meta_research.semantic_owner_gateway import _reasoning_operations, _plan_evidence_operations

    huge_closures = [
        {"target_commit_ref": f"tc:{i}", "payload": "x" * 3000}
        for i in range(25)
    ]
    pack = {
        "question_literature_input": {"kind": "none", "records": [{"title": "p" * 1000} for _ in range(20)]},
        "plan_evidence_input": {"kind": "accepted", "notes": "n" * 20000},
        "accepted_target_commit_closures": huge_closures,
    }
    request = SimpleNamespace(
        request_ref="req:r1",
        cycle_ref="cycle:1",
        context_pack_ref="ctx:r1",
        context_pack_hash=canonical_hash(pack),
        context_pack=pack,
        accepted_question=SimpleNamespace(quest_ref="quest:1", question_ref="q:1", content_ref="c:1", content_hash="h:1"),
    )
    run = SimpleNamespace(
        run_ref="run:1", attempt_ref="att:1", root_session_ref="sess:1",
        fence_ref="fence:1", runtime_binding_hash="b" * 64,
    )
    ae = SimpleNamespace(
        query_reasoning_stage_request=Mock(return_value=request),
        query_plan_stage_request=Mock(return_value=request),
        query_idea_stage_request=Mock(return_value=request),
    )
    ar = SimpleNamespace(
        verify_root_agent_runtime_scope=Mock(),
        verify_reasoning_runtime_scope=Mock(),
        query_managed_run=Mock(side_effect=lambda ref: {
            "run_kind": "reasoning_stage" if ref == "run:1" else "plan_stage",
            "cycle_ref": "cycle:1",
            "attempt_ref": "att:1",
            "root_session_ref": "sess:1",
            "fence_ref": "fence:1",
        }),
        query_reasoning_stage_run=Mock(return_value=run),
        query_plan_stage_run=Mock(return_value=SimpleNamespace(**{**vars(run), "run_ref": "run:p1"})),
        query_idea_stage_run=Mock(return_value=run),
    )
    rg = SimpleNamespace(
        query_plan_evidence_page=Mock(return_value=({"offset": 0, "limit": 32, "next_offset": None}, ())),
    )
    rm = SimpleNamespace()
    ops = (
        *_reasoning_operations(advancement_engine=ae, agent_runtime=ar),
        *_plan_evidence_operations(advancement_engine=ae, agent_runtime=ar, research_graph=rg, research_memory=rm),
        *stage_context_operations(advancement_engine=ae, agent_runtime=ar, research_graph=rg, research_memory=rm),
    )
    gateway = SemanticMcpGateway(ops)

    # 1. reasoning_evidence.read with default {} is bounded <= 16KB and paginated.
    conn_r, _ = gateway.issue_channel(
        run_ref="run:1", attempt_ref="att:1", root_session_ref="sess:1", fence_ref="fence:1",
        capability_binding_hash="b" * 64, operation_ids=gateway.operation_ids,
        root_kind="reasoning", phase="primary",
    )
    _, resp = gateway.dispatch(
        conn_r.token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "research_memory.reasoning_evidence.read", "arguments": {},
        }},
    )
    assert resp["result"]["isError"] is False, resp
    sc = resp["result"]["structuredContent"]
    assert len(canonical_json(sc).encode("utf-8")) <= 16384 + 2048
    assert sc["total_target_commits"] == 25
    assert sc["next_offset"] is not None
    assert sc["next_offset"] > 0
    # Continuation page advances cleanly with offset.
    _, resp_p2 = gateway.dispatch(
        conn_r.token,
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
            "name": "research_memory.reasoning_evidence.read",
            "arguments": {"offset": sc["next_offset"], "limit": 5, "max_bytes": 16384},
        }},
    )
    assert resp_p2["result"]["isError"] is False, resp_p2
    sc2 = resp_p2["result"]["structuredContent"]
    assert sc2["offset"] == sc["next_offset"]
    assert len(canonical_json(sc2).encode("utf-8")) <= 16384 + 2048

    # 2. stage_context.read omitting path/offset/limit works with safe defaults, while context_pack_ref remains required.
    _, sc_resp = gateway.dispatch(
        conn_r.token,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "research_memory.stage_context.read",
            "arguments": {"context_pack_ref": "ctx:r1", "source": "context_pack"},
        }},
    )
    assert sc_resp["result"]["isError"] is False, sc_resp
    assert sc_resp["result"]["structuredContent"]["offset"] == 0

    # Missing context_pack_ref STILL fails closed.
    _, sc_bad = gateway.dispatch(
        conn_r.token,
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "research_memory.stage_context.read",
            "arguments": {"source": "context_pack"},
        }},
    )
    assert sc_bad["result"]["isError"] is True

    # 3. plan_evidence.page omitting offset/limit succeeds with safe defaults.
    plan_pack = {**pack, "evidence_catalog": []}
    request_p = SimpleNamespace(
        request_ref="req:p1", cycle_ref="cycle:1", context_pack_ref="ctx:p1",
        context_pack_hash=canonical_hash(plan_pack), context_pack=plan_pack,
        accepted_question=request.accepted_question,
    )
    ae.query_plan_stage_request.return_value = request_p
    conn_p, _ = gateway.issue_channel(
        run_ref="run:p1", attempt_ref="att:1", root_session_ref="sess:1", fence_ref="fence:1",
        capability_binding_hash="b" * 64, operation_ids=gateway.operation_ids,
        root_kind="plan", phase="primary",
    )
    _, pe_resp = gateway.dispatch(
        conn_p.token,
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "research_graph.plan_evidence.page", "arguments": {},
        }},
    )
    assert pe_resp["result"]["isError"] is False, pe_resp
    rg.query_plan_evidence_page.assert_called_once_with(
        quest_ref="quest:1", question_ref="q:1", offset=0, limit=32
    )


# ---------------------------------------------------------------------------
# RG2 / WS1: Enriched questions.page + active questions; Workspace kind, purpose, limit=8
# ---------------------------------------------------------------------------


def test_rg2_questions_page_and_active_list_expose_tree_and_latest_outcome(tmp_path):
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_research_datasets import _quest, _runtime

    runtime = _runtime(tmp_path / "rg2-questions")
    try:
        root_q = _quest(runtime, "root-question")
        human = runtime.owners.human_collaboration
        graph = runtime.owners.research_graph
        memory = runtime.owners.research_memory

        _confirm_waived_manual_question(
            human, quest_ref=root_q.quest_ref, parent_question_ref=root_q.question_ref, key_prefix="child-a"
        )
        for _ in range(8):
            if not human.reconcile_once():
                break

        page = discover_questions(graph, memory, quest_ref=root_q.quest_ref, limit=12)
        assert len(page["items"]) == 2
        by_ref = {item["question_ref"]: item for item in page["items"]}
        assert by_ref[root_q.question_ref]["parent_question_ref"] is None
        assert by_ref[root_q.question_ref]["title"]
        assert "latest_outcome_one_line" in by_ref[root_q.question_ref]
        child_ref = next(ref for ref in by_ref if ref != root_q.question_ref)
        assert by_ref[child_ref]["parent_question_ref"] == root_q.question_ref
        assert by_ref[child_ref]["title"]

        active = graph.query_active_question_page(quest_ref=root_q.quest_ref, offset=0, limit=12)
        assert set(active["items"]) == {root_q.question_ref, child_ref}
        assert "questions" in active
        active_by_ref = {item["question_ref"]: item for item in active["questions"]}
        assert active_by_ref[root_q.question_ref]["parent_question_ref"] is None
        assert active_by_ref[child_ref]["parent_question_ref"] == root_q.question_ref
        assert active_by_ref[root_q.question_ref]["title"]
        assert "latest_outcome_one_line" in active_by_ref[root_q.question_ref]

        # When an accepted ScientificOutcome exists, latest_outcome_one_line exposes its 1-line conclusion.
        import sqlite3

        outcome_ref = "outcome:rg2-test"
        with sqlite3.connect(str(runtime._database.path)) as raw_conn:
            raw_conn.execute(
                "INSERT INTO rg_reasoning_outcome_decisions ("
                "decision_ref, request_ref, submission_ref, run_ref, attempt_ref, fence_ref, "
                "reasoning_content_ref, reasoning_content_receipt_ref, reasoning_content_receipt_hash, "
                "payload_hash, scientific_outcome_ref, outcome_hash, scientific_disposition, "
                "transition_kind, transition_ref, transition_json, transition_hash, "
                "reviewed_draft_hash, review_hash, decision, outcome_ref, "
                "feedback_json, feedback_hash, receipt_ref, receipt_hash, decided_at"
                ") VALUES ("
                "'dec:1', 'req:1', 'sub:1', 'run:1', 'att:1', 'fence:1', "
                "'rc:1', 'rcr:1', 'a', 'b', ?, 'c', 'sufficient', "
                "'continue_question', 'tr:1', ?, 'd', "
                "'e', 'f', 'accepted', ?, "
                "'[]', 'g', 'rec:1', 'h', 100.0"
                ")",
                (
                    outcome_ref,
                    canonical_json(
                        {
                            "source_quest_ref": root_q.quest_ref,
                            "source_question_ref": root_q.question_ref,
                        }
                    ),
                    outcome_ref,
                ),
            )
            raw_conn.commit()
        graph._receipt_verifier._verified_reasoning_history_source = lambda row: {
            "transition": {
                "source_quest_ref": root_q.quest_ref,
                "source_question_ref": root_q.question_ref,
            },
            "scientific_outcome": {
                "quest_ref": root_q.quest_ref,
                "question_ref": root_q.question_ref,
                "disposition": "affirmed",
                "claim": "四次刺激事件均引起显著皮电峰值上升。详细分析见正文。",
            },
        }
        page_with_outcome = discover_questions(graph, memory, quest_ref=root_q.quest_ref, limit=12)
        by_ref_out = {item["question_ref"]: item for item in page_with_outcome["items"]}
        assert by_ref_out[root_q.question_ref]["latest_outcome_ref"] == outcome_ref
        assert by_ref_out[root_q.question_ref]["latest_outcome_disposition"] == "affirmed"
        assert "四次刺激事件均引起显著皮电峰值上升" in str(by_ref_out[root_q.question_ref]["latest_outcome_one_line"])
        active_with_outcome = graph.query_active_question_page(quest_ref=root_q.quest_ref, offset=0, limit=12)
        active_out_by_ref = {item["question_ref"]: item for item in active_with_outcome["questions"]}
        assert active_out_by_ref[root_q.question_ref]["latest_outcome_ref"] == outcome_ref
        assert "四次刺激事件均引起显著皮电峰值上升" in str(active_out_by_ref[root_q.question_ref]["latest_outcome_one_line"])

        # Also verify insufficient_evidence (where claim is None and missing_evidence carries the reason).
        from meta_research.owners.research_graph import _scientific_outcome_one_line

        insuff_line = _scientific_outcome_one_line(
            {
                "disposition": "insufficient_evidence",
                "claim": None,
                "missing_evidence": ["缺少对照组基线原始采样记录。"],
            }
        )
        assert insuff_line == "[insufficient_evidence] 缺少对照组基线原始采样记录"
    finally:
        runtime.close()


def test_ws1_workspace_kind_purpose_and_mounted_materials_limit_8(tmp_path):
    from meta_research.root_workspace import RootWorkspaces, WorkspaceLocation
    from sqlalchemy import create_engine

    stage_dir = tmp_path / "stage_idea"
    target_dir = tmp_path / "target_t1"
    stage_dir.mkdir()
    target_dir.mkdir()

    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rg_quests (quest_ref TEXT, initialization_id TEXT)"))
        conn.execute(text("INSERT INTO rg_quests VALUES ('quest:1', 'init:1')"))
        conn.execute(text("CREATE TABLE rg_targets (target_ref TEXT, target_key TEXT, spec_json TEXT)"))
        conn.execute(
            text("INSERT INTO rg_targets VALUES (:ref, :key, :spec)"),
            {
                "ref": "target:1",
                "key": "biosignal-run",
                "spec": canonical_json({
                    "semantic_inputs": [
                        {"experiment_key": "exp-1", "goal": "提取四次刺激事件心率与皮电峰值。随后做条件对比汇总。"}
                    ]
                }),
            },
        )
        conn.execute(text("CREATE TABLE ar_deepfetch_runs (run_ref TEXT, request_ref TEXT)"))
        conn.execute(text("CREATE TABLE hc_deepfetch_requests (request_ref TEXT, initialization_id TEXT)"))
        conn.execute(
            text(
                "CREATE TABLE hc_work_material_submissions ("
                "submission_ref TEXT, state TEXT, anchor_kind TEXT, anchor_ref TEXT, "
                "receiver_json TEXT, command_json TEXT, created_at REAL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE hc_work_material_references ("
                "reference_ref TEXT, submission_ref TEXT, ordinal INTEGER, selection_json TEXT)"
            )
        )
        for i in range(10):
            conn.execute(
                text(
                    "INSERT INTO hc_work_material_submissions VALUES "
                    "(:sub, 'ready', 'manual', 'quest:1', :recv, :cmd, :ts)"
                ),
                {
                    "sub": f"sub:{i}",
                    "recv": canonical_json({"quest_ref": "quest:1"}),
                    "cmd": canonical_json({"description": f"Material #{i}"}),
                    "ts": float(i),
                },
            )
            conn.execute(
                text(
                    "INSERT INTO hc_work_material_references VALUES (:ref, :sub, 0, :sel)"
                ),
                {
                    "ref": f"material:{i:02d}",
                    "sub": f"sub:{i}",
                    "sel": canonical_json({"absolute_path": f"/data/file_{i:02d}.csv", "kind": "file"}),
                },
            )

    from contextlib import contextmanager

    class _DB:
        @contextmanager
        def read(self):
            with engine.connect() as c:
                yield c

    db = _DB()
    stage_loc = WorkspaceLocation(
        workspace_ref="ws:stage", work_ref="idea_run:1", root_kind="idea",
        quest_ref="quest:1", cycle_ref="cycle:1", target_ref=None,
        root_session_ref="sess:1", request_ref="req:1", initialization_id=None,
        context_generation=None, directory=stage_dir.resolve(),
    )
    target_loc = WorkspaceLocation(
        workspace_ref="ws:target", work_ref="target_run:1", root_kind="target",
        quest_ref="quest:1", cycle_ref="cycle:1", target_ref="target:1",
        root_session_ref="sess:2", request_ref="req:2", initialization_id=None,
        context_generation=None, directory=target_dir.resolve(),
    )
    ws = object.__new__(RootWorkspaces)
    ws._hc = SimpleNamespace(_database=db)
    ws._rg = SimpleNamespace(_database=db)
    ws._ae = SimpleNamespace(
        query_cycle_position=lambda cycle_ref: {
            "cycle_ref": cycle_ref,
            "round": 1,
            "cycle_focus": "验证事件相关生理特征提取。附加细节。",
            "focus_source": {"kind": "initial"},
        }
    )
    ws._visible_locations = lambda current: (stage_loc, target_loc)

    prompt = ws.native_directory_prompt(stage_loc)
    entries, _ = json.JSONDecoder().raw_decode(prompt.split("Work directories: ", 1)[1])
    by_ws = {entry["workspace_ref"]: entry for entry in entries}
    assert by_ws["ws:stage"]["workspace_kind"] == "stage_workspace"
    assert by_ws["ws:target"]["workspace_kind"] == "target_workspace"
    assert "提取四次刺激事件心率与皮电峰值" in by_ws["ws:target"]["purpose"]
    assert "验证事件相关生理特征提取" in by_ws["ws:stage"]["purpose"]

    # Mounted server materials default paginated at limit=8 with next_offset=8!
    marker = "Mounted server materials (persistent read-only references, directly accessible on host or via research_workspace.materials.*): "
    mounted_line = next(line for line in prompt.splitlines() if line.startswith(marker))
    mounted_items = json.loads(mounted_line.split(marker, 1)[1])
    assert len(mounted_items) == 8
    page_marker = "Mounted server materials page: "
    page_line = next(line for line in prompt.splitlines() if line.startswith(page_marker))
    page_info = json.loads(page_line.split(page_marker, 1)[1])
    assert page_info["total_count"] == 10
    assert page_info["next_offset"] == 8
    assert page_info["reader"]["arguments"] == {"offset": 0, "limit": 50}

    # Full access via offset pagination still returns the remaining 2 items!
    page2 = ws._mounted_material_page(stage_loc, offset=8, limit=8)
    assert len(page2["items"]) == 2
    assert page2["next_offset"] is None
    engine.dispose()


# ---------------------------------------------------------------------------
# Review Fixes: Blockers #1-#3, Medium #4, Minors #5-#7
# ---------------------------------------------------------------------------


def test_d8b_deepfetch_reconciles_empty_external_mcp_and_rejects_missing_nonempty(tmp_path):
    """Blocker #1: DeepFetch durable reconciliation succeeds when 0 external MCP services exist,
    and still fails closed when >=1 external MCP services are configured but header is missing."""
    from dataclasses import replace

    from meta_research.deepfetch import (
        CodexDeepFetchAdapter,
        DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
        DeepFetchUnavailable,
        _deepfetch_output_schema,
    )
    from meta_research.external_mcp import ExternalMcpRuntime, compose_external_mcp_prompt, parse_services
    from meta_research.provider_supervisor import (
        SUPERVISOR_EXIT_SCHEMA,
        SUPERVISOR_REQUEST_SCHEMA,
        supervisor_host_identity,
        write_exit_receipt,
    )
    from meta_research.root_capabilities import root_capability_profile
    import test_external_mcp as ext_mcp_fixtures
    import test_part3_stability_workers_guidance_supervisor as df_fixtures
    PROVIDER_RESULT_MAX_BYTES = df_fixtures.PROVIDER_RESULT_MAX_BYTES
    ensure_transport_key = df_fixtures.ensure_transport_key
    write_transport_envelope = df_fixtures.write_transport_envelope

    def _run_reconcile(case_dir: Path, *, with_service: bool, include_header: bool):
        workspace = case_dir / "df-ws"
        acquisition = df_fixtures.RecordingAcquisitionClient(case_dir / "acq")
        ext_runtime = ExternalMcpRuntime(case_dir / "ext-mcp")
        ext_runtime.configure_endpoint("http://127.0.0.1:9999")
        if with_service:
            svc = ext_mcp_fixtures.service(case_dir / "ext-svc")
            ext_runtime.save_config(
                services=parse_services([svc]),
                expected_revision=ext_runtime.read_config().revision,
            )
        adapter = df_fixtures._bind_acquisition(
            CodexDeepFetchAdapter(
                workspace,
                model_ref="gpt-test",
                process_runner=df_fixtures.SequencedPrototypeRunner([df_fixtures.PROTOTYPE_EMPTY_FINAL]),
            ),
            acquisition,
        )
        adapter.bind_external_mcp(ext_runtime)
        job_ref = "deepfetch-run:ext-mcp-reconcile"
        req = replace(
            df_fixtures._base_deepfetch_request(),
            job_ref=job_ref,
            runtime_binding=adapter.runtime_binding(),
            native_session_ref="native-df-ext-1",
            reconcile_only=True,
        )
        binding_hash = canonical_hash(adapter.runtime_binding().as_dict())
        provider_job_ref = f"{job_ref}:v4-turn:0"
        identity_leaf = canonical_hash({"job_ref": provider_job_ref})
        op_root = workspace / "provider-operations" / f"{binding_hash}-{identity_leaf}"
        seg0 = op_root / "deepfetch-initial"
        seg0.mkdir(parents=True)
        _kp, key = ensure_transport_key(workspace)

        run_root = adapter._protocol_run_root(adapter._provider_execution_key(job_ref), binding_hash)
        public_root = run_root / "public"
        public_root.mkdir(parents=True, exist_ok=True)
        base_prompt = f"public_output_root={public_root}\ngoal=test\n"
        snapshot = ext_runtime.operation_snapshot(
            operation_identity=f"{provider_job_ref}:initial",
            root_kind="deepfetch",
            task_prompt=base_prompt,
        )
        prompt_text = (
            compose_external_mcp_prompt(base_prompt, snapshot)
            if include_header
            else base_prompt
        )
        schema_text = canonical_json(_deepfetch_output_schema())
        profile = root_capability_profile("deepfetch")
        inv = {
            "schema_ref": "meta-research/deepfetch-provider-operation/v3",
            "job_ref": provider_job_ref,
            "segment_name": "initial",
            "request_ref": req.request_ref,
            "correlation_ref": req.correlation_ref,
            "draft_hash": req.draft_hash,
            "scope_hash": req.scope_hash,
            "runtime_binding_hash": binding_hash,
            "native_session_ref": None,
            "prompt_hash": canonical_hash(prompt_text),
            "output_schema_hash": canonical_hash(_deepfetch_output_schema()),
            "model_ref": req.runtime_binding.model_ref,
            "root_capability_profile": profile.as_dict(),
            "root_capability_profile_hash": profile.digest,
            "external_mcp": snapshot.binding(),
        }
        (seg0 / "prompt.txt").write_text(prompt_text, encoding="utf-8")
        (seg0 / "output-schema.json").write_text(schema_text, encoding="utf-8")
        (seg0 / "last-message.json").write_text(json.dumps(df_fixtures.PROTOTYPE_EMPTY_FINAL), encoding="utf-8")
        (seg0 / "stdout.jsonl").write_text(
            "\n".join([
                json.dumps({"type": "thread.started", "thread_id": "native-df-ext-1"}),
                json.dumps({"type": "turn.completed"}),
            ]) + "\n",
            encoding="utf-8",
        )
        write_transport_envelope(seg0 / "invocation.json", inv, key)
        argv = adapter._durable_argv(
            job_ref=provider_job_ref,
            runtime_binding_hash=binding_hash,
            schema_path=seg0 / "output-schema.json",
            result_path=seg0 / "last-message.json",
            native_session_ref=None,
        )
        write_transport_envelope(
            seg0 / "supervisor-request.json",
            {
                "schema_ref": SUPERVISOR_REQUEST_SCHEMA,
                "invocation_hash": canonical_hash(inv),
                "argv": argv,
                "prompt_path": str(seg0 / "prompt.txt"),
                "schema_path": str(seg0 / "output-schema.json"),
                "stdout_path": str(seg0 / "stdout.jsonl"),
                "result_path": str(seg0 / "last-message.json"),
                "lock_path": str(seg0 / "supervisor.lock"),
                "ready_path": str(seg0 / "supervisor-ready.json"),
                "started_path": str(seg0 / "provider-started.json"),
                "receipt_path": str(seg0 / "supervisor-exit.json"),
                "stop_path": str(seg0 / "supervisor-stop.json"),
                "timeout_seconds": None,
                "stream_max_bytes": DEEPFETCH_PROVIDER_STREAM_MAX_BYTES,
                "result_max_bytes": PROVIDER_RESULT_MAX_BYTES,
            },
            key,
        )
        write_exit_receipt(
            seg0 / "supervisor-exit.json",
            key=key,
            invocation_hash=canonical_hash(inv),
            prompt_path=seg0 / "prompt.txt",
            schema_path=seg0 / "output-schema.json",
            stdout_path=seg0 / "stdout.jsonl",
            result_path=seg0 / "last-message.json",
            stderr_path=None,
            returncode=0,
            input_bytes=(seg0 / "prompt.txt").stat().st_size,
            process_tree={
                "schema_ref": "meta-research/provider-process-tree/v1",
                "state": "closed",
                "mechanism": "linux-subreaper-echild",
                **supervisor_host_identity(),
            },
            termination_reason="completed",
            schema_ref=SUPERVISOR_EXIT_SCHEMA,
        )
        return adapter._read_existing_turn(
            req,
            public_root=public_root,
            provider_job_ref=provider_job_ref,
            runtime_binding_hash=binding_hash,
            expected_native_session_ref=None,
            enforce_native_session_ref=False,
            expected_web_gate=False,
            resident_phase="turn-0",
        )

    # 1. 0 external MCP services configured -> compose omitted empty header -> reconciliation succeeds cleanly!
    result_doc, observed_native, _, _ = _run_reconcile(
        tmp_path / "empty-ext", with_service=False, include_header=True
    )
    assert observed_native == "native-df-ext-1"
    assert result_doc["completion"] == "honest_empty"

    # 2. >=1 external MCP services configured -> missing header STILL fails closed with external_mcp_prompt_missing!
    with pytest.raises(DeepFetchUnavailable) as exc_info:
        _run_reconcile(tmp_path / "missing-header", with_service=True, include_header=False)
    assert isinstance(exc_info.value.__cause__, ValueError)
    assert str(exc_info.value.__cause__) == "external_mcp_prompt_missing"


def test_ws1_resume_runtime_conditions_tracks_mounted_materials_page_changes():
    """Blocker #2: resume_runtime_conditions detects newly mounted materials even when >= 8 exist."""
    from meta_research.runtime_conditions import resume_runtime_conditions

    marker = (
        "Mounted server materials (persistent read-only references, directly accessible on host "
        "or via research_workspace.materials.*): "
    )
    page_marker = "Mounted server materials page: "

    def _mat(i: int) -> dict[str, object]:
        return {
            "reference_ref": f"material:{i:02d}",
            "absolute_path": f"/data/file_{i:02d}.csv",
            "kind": "file",
            "access": "read_only",
        }

    def _cond(items: list[dict[str, object]], total: int) -> str:
        text_lines = [marker + canonical_json(items)]
        if total > len(items):
            text_lines.append(
                page_marker
                + canonical_json({
                    "shown_count": len(items),
                    "total_count": total,
                    "offset": 0,
                    "limit": 8,
                    "next_offset": 8,
                    "reader": {
                        "operation": "research_workspace.materials.discover",
                        "arguments": {"offset": 0, "limit": 50},
                    },
                })
            )
        return "\n".join(text_lines)

    first_8 = [_mat(i) for i in range(8)]
    prompt_8 = _cond(first_8, total=8)
    prompt_9 = _cond(first_8, total=9)

    # (a) 8 -> 9 mounted materials emits delta with updated page metadata & reader!
    delta_8_to_9 = resume_runtime_conditions(prompt_9, (prompt_8,), ordered=True)
    assert delta_8_to_9.startswith("挂载材料授权更新=")
    payload_9 = json.loads(delta_8_to_9.split("=", 1)[1])
    assert payload_9["mounted_materials_page"]["total_count"] == 9
    assert payload_9["mounted_materials_page"]["reader"]["arguments"] == {"offset": 0, "limit": 50}

    # (b) 9 -> 9 unchanged (even after an unrelated time-only update) emits ""!
    time_only = '当前运行条件更新={"research_time":{"used_seconds":42}}'
    assert resume_runtime_conditions(prompt_9, (time_only, delta_8_to_9, prompt_8), ordered=True) == ""

    # (c) 10 -> 9 after removing an early material (material:00) emits removed_reference_refs + updated page!
    prompt_10 = _cond(first_8, total=10)
    after_remove = [_mat(i) for i in range(1, 9)]
    prompt_9_shifted = _cond(after_remove, total=9)
    delta_shifted = resume_runtime_conditions(prompt_9_shifted, (prompt_10,), ordered=True)
    assert delta_shifted.startswith("挂载材料授权更新=")
    payload_shifted = json.loads(delta_shifted.split("=", 1)[1])
    assert payload_shifted["removed_reference_refs"] == ["material:00"]
    assert payload_shifted["mounted_materials_page"]["total_count"] == 9

    # (d) 9 -> 8 emits page cleared to None, then 8 -> 8 emits ""!
    delta_9_to_8 = resume_runtime_conditions(prompt_8, (delta_8_to_9, prompt_8), ordered=True)
    assert delta_9_to_8.startswith("挂载材料授权更新=")
    assert json.loads(delta_9_to_8.split("=", 1)[1])["mounted_materials_page"] is None
    assert resume_runtime_conditions(prompt_8, (delta_9_to_8, delta_8_to_9), ordered=True) == ""


def test_ws1_discover_work_materials_reader_preserves_all_ordinals_with_workspace_scoped_materials(tmp_path):
    """Blocker #3: discover_work_materials orders by (created_at, ordinal, reference_ref) and
    following the emitted reader returns all 10 same-timestamp files + workspace-scoped material
    with zero skipped ordinals and zero duplicates."""
    from contextlib import contextmanager
    from sqlalchemy import create_engine

    from meta_research.root_workspace import RootWorkspaces, WorkspaceLocation
    from meta_research.work_materials import WorkMaterialsMixin

    stage_dir = tmp_path / "stage_idea"
    stage_dir.mkdir()
    engine = create_engine("sqlite:///:memory:")
    # Deliberately reverse-lexicographical reference_refs for ordinals 0..9 to prove ordinal order wins.
    ordered_refs = [f"ref_{chr(ord('z') - i)}_ord{i}" for i in range(10)]
    ws_scoped_ref = "ref_00_workspace_scoped"
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rg_quests (quest_ref TEXT, initialization_id TEXT)"))
        conn.execute(text("INSERT INTO rg_quests VALUES ('quest:1', 'init:1')"))
        conn.execute(text("CREATE TABLE rg_targets (target_ref TEXT, target_key TEXT, spec_json TEXT)"))
        conn.execute(text("CREATE TABLE ar_deepfetch_runs (run_ref TEXT, request_ref TEXT)"))
        conn.execute(text("CREATE TABLE hc_deepfetch_requests (request_ref TEXT, initialization_id TEXT)"))
        conn.execute(
            text(
                "CREATE TABLE hc_work_material_submissions ("
                "submission_ref TEXT, state TEXT, anchor_kind TEXT, anchor_ref TEXT, "
                "receiver_json TEXT, command_json TEXT, created_at REAL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE hc_work_material_references ("
                "reference_ref TEXT, submission_ref TEXT, ordinal INTEGER, selection_json TEXT)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE hc_work_material_roots ("
                "submission_ref TEXT, workspace_ref TEXT)"
            )
        )
        # Earlier workspace-scoped material (not quest-receiver anchored, so shifts discover offset).
        conn.execute(
            text(
                "INSERT INTO hc_work_material_submissions VALUES "
                "('sub:ws', 'ready', 'manual', 'ws:stage', '{}', :cmd, 5.0)"
            ),
            {"cmd": canonical_json({"description": "Workspace-only scratch"})},
        )
        conn.execute(
            text("INSERT INTO hc_work_material_roots VALUES ('sub:ws', 'ws:stage')")
        )
        conn.execute(
            text("INSERT INTO hc_work_material_references VALUES (:ref, 'sub:ws', 0, :sel)"),
            {"ref": ws_scoped_ref, "sel": canonical_json({"absolute_path": "/data/ws.csv", "kind": "file"})},
        )
        # Single submission with 10 files sharing identical created_at=10.0 and ordinals 0..9.
        conn.execute(
            text(
                "INSERT INTO hc_work_material_submissions VALUES "
                "('sub:batch10', 'ready', 'manual', 'quest:1', :recv, :cmd, 10.0)"
            ),
            {
                "recv": canonical_json({"quest_ref": "quest:1"}),
                "cmd": canonical_json({"description": "Ten mounted files"}),
            },
        )
        for ordinal, ref in enumerate(ordered_refs):
            conn.execute(
                text("INSERT INTO hc_work_material_references VALUES (:ref, 'sub:batch10', :ord, :sel)"),
                {
                    "ref": ref,
                    "ord": ordinal,
                    "sel": canonical_json({"absolute_path": f"/data/batch_{ordinal}.csv", "kind": "file"}),
                },
            )

    class _DB:
        @contextmanager
        def read(self):
            with engine.connect() as c:
                yield c

    db = _DB()
    stage_loc = WorkspaceLocation(
        workspace_ref="ws:stage", work_ref="idea_run:1", root_kind="idea",
        quest_ref="quest:1", cycle_ref="cycle:1", target_ref=None,
        root_session_ref="sess:1", request_ref="req:1", initialization_id=None,
        context_generation=None, directory=stage_dir.resolve(),
    )
    ws = object.__new__(RootWorkspaces)
    ws._hc = SimpleNamespace(_database=db)
    ws._rg = SimpleNamespace(_database=db)
    ws._ae = SimpleNamespace(query_cycle_position=lambda _c: None)
    ws._visible_locations = lambda current: (stage_loc,)

    prompt = ws.native_directory_prompt(stage_loc)
    page_marker = "Mounted server materials page: "
    page_info = json.loads(
        next(line for line in prompt.splitlines() if line.startswith(page_marker)).split(page_marker, 1)[1]
    )
    reader_args = page_info["reader"]["arguments"]
    assert reader_args == {"offset": 0, "limit": 50}

    class _Owner(WorkMaterialsMixin):
        def __init__(self):
            self._database = db

        def _material_visible_workspaces(self, context):
            return {"ws:stage"}

        def _quest_directory_references(self, context):
            return set(ordered_refs)

        def _material_scope(self, context):
            return {}

        def query_work_material(self, reference_ref):
            return {"reference_ref": reference_ref}

    owner = _Owner()
    discovered = owner.discover_work_materials(
        context=SimpleNamespace(phase="primary"),
        **reader_args,
    )
    returned_refs = [item["reference_ref"] for item in discovered["references"]]
    assert len(returned_refs) == len(set(returned_refs)) == 11
    assert returned_refs[0] == ws_scoped_ref
    assert returned_refs[1:] == ordered_refs
    engine.dispose()


def test_d5_rolling_batch_baseline_rejection_uses_batch_local_index_and_surfaces_path(tmp_path):
    """Medium #4: Rolling batch rejection reports batch-local $.candidates[0] (not global ordinal)
    and surfaces contract_failure_detail_path in repair_hint."""
    from meta_research.owners.research_graph import _insert_target_with_measurement_authority
    from meta_research.sealed_provider_operations import _provider_terminal_contract_checkpoint
    from meta_research.owners.agent_runtime import _validated_stage_provider_hard_ceiling

    # 1. Batch-local index is used when candidate_index=0 even if target.ordinal=3.
    import test_bundle_skill_adapter as bundle_fixtures

    plan = bundle_fixtures._plan_document()
    target_plan = bundle_fixtures._target_plan(plan, "a" * 64)
    completion = normalized_completion_contract_from_dict(
        target_plan["completion_contract"], plan_document=plan
    )
    raw_cand = deepcopy(target_plan["initial_strategy_update"]["candidates"][0])
    raw_cand["measurement_contract"]["baseline_forward_contract"] = {"method_key": "legacy-only"}
    formal_candidate = formal_target_candidate_from_dict(raw_cand, completion_contract=completion)
    target_spec = formal_target_candidate_to_dict(formal_candidate, completion_contract=completion)
    target = SimpleNamespace(
        target_ref="target:3",
        graph_ref="graph:1",
        target_key=formal_candidate.candidate.local_label,
        ordinal=3,
        spec=target_spec,
        spec_hash=canonical_hash(target_spec),
        dependency_refs=(),
        receipt=SimpleNamespace(receipt_ref="rec:1", payload_hash="a" * 64),
    )
    from sqlalchemy import create_engine
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE rm_asset_versions (version_ref TEXT)"))
        conn.execute(text(
            "CREATE TABLE rg_targets (target_ref TEXT, graph_ref TEXT, target_key TEXT, ordinal INT, "
            "spec_json TEXT, spec_hash TEXT, dependency_refs_json TEXT, dependency_refs_hash TEXT, "
            "receipt_ref TEXT, receipt_hash TEXT, accepted_at REAL, append_ref TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE rg_target_spec_acceptances (acceptance_ref TEXT, target_ref TEXT, graph_ref TEXT, "
            "spec_content_hash_ref TEXT, target_acceptance_receipt_ref TEXT, target_acceptance_receipt_hash TEXT, "
            "receipt_ref TEXT, receipt_hash TEXT, accepted_at REAL)"
        ))
        conn.execute(text(
            "CREATE TABLE rg_experiment_baselines (baseline_ref TEXT, quest_ref TEXT, "
            "forward_contract_json TEXT, forward_contract_hash TEXT, accepted_at REAL)"
        ))
        with pytest.raises(OwnerConflict) as exc_info:
            _insert_target_with_measurement_authority(
                conn,
                target=target,
                append_ref="append:1",
                quest_ref="quest:1",
                target_plan_hash="b" * 64,
                graph_generation=1,
                graph_acceptance_receipt=SimpleNamespace(receipt_ref="gr:1", payload_hash="c" * 64),
                rolling_append_source=None,
                stage_request_ref="req:1",
                plan_binding=SimpleNamespace(),
                completion_contract=SimpleNamespace(),
                formal_candidate=formal_candidate,
                accepted_at=1.0,
                candidate_index=0,
            )
    assert exc_info.value.code == "baseline_method_identity_required"
    assert exc_info.value.details is not None
    assert exc_info.value.details["path"].startswith("$.candidates[0].measurement_contract.")
    engine.dispose()

    # 2. Terminal contract failure checkpoint accepts and validates optional contract_failure_detail_path.
    detail_path = "$.candidates[0].measurement_contract.baseline_forward_contract"
    marker = {
        "termination_reason": "completed",
        "invocation_hash": "1" * 64,
        "prompt_hash": "2" * 64,
        "output_schema_hash": "3" * 64,
        "stdout_hash": "4" * 64,
        "result_file_hash": "5" * 64,
        "supervisor_receipt_hash": "6" * 64,
    }
    checkpoint = _provider_terminal_contract_checkpoint(
        marker,
        "bundle_review_result_contract_invalid",
        "baseline_method_identity_required",
        detail_path=detail_path,
    )
    assert checkpoint["contract_failure_detail_path"] == detail_path
    validated = _validated_stage_provider_hard_ceiling(
        unit_kind="bundle_review",
        failure_code="bundle_review_result_contract_invalid",
        provider_exit=checkpoint,
    )
    assert validated["contract_failure_detail_path"] == detail_path


def test_d3_omitted_implementation_revision_ref_flows_through_graph_finalizer_and_commit(tmp_path):
    """Minor #5: End-to-end integration test with Bundle implementation_revision_ref=None
    flowing through accept_target_graph -> TargetRunFinalizer -> accept_target_commit_from_root_completion."""
    import test_target_root_finalizer as finalizer_fixtures

    class _NullImplBundleSkill(finalizer_fixtures._CurrentBindingBundleSkill):
        def _target_plan(self, request):
            document = super()._target_plan(request)
            for item in document["initial_strategy_update"]["candidates"]:
                item["candidate"]["implementation_revision_ref"] = None
            return document

    runtime = finalizer_fixtures._current_bundle_runtime(
        tmp_path / "d3-e2e-null-impl",
        bundle_skill=_NullImplBundleSkill(),
    )
    try:
        runtime, lifecycle, memory, authority, handle, workspace, evidence = (
            finalizer_fixtures._root_finalizer_fixture(tmp_path / "unused", runtime=runtime)
        )
        # Confirm Bundle candidate was accepted into RG with implementation_revision_ref=None.
        with runtime._database.read() as conn:
            spec_json = conn.execute(
                text("SELECT spec_json FROM rg_targets WHERE target_ref = :ref"),
                {"ref": handle.target_ref},
            ).scalar_one()
        assert json.loads(spec_json)["candidate"]["implementation_revision_ref"] is None

        finalizer = finalizer_fixtures.TargetRunFinalizer(
            lifecycle=lifecycle,
            memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=finalizer_fixtures._EvidenceReader(evidence),
            measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph,
        )
        completed = finalizer.finalize(handle=handle, evidence=evidence)
        assert completed.status == "completed"
        assert completed.target_commit_ref is not None
        manifest = memory.query(completed.manifest_ref)
        assert manifest is not None
        assert manifest.implementation_revision_ref.startswith("target_impl_")
        commits = runtime.owners.research_graph.query_target_commits(authority.graph_ref)
        assert any(c.commit_ref == completed.target_commit_ref for c in commits)
    finally:
        runtime.close()


def test_d9_check_result_aligns_status_with_matching_failure_and_checks_failed_eval_metrics(
    tmp_path, monkeypatch, capsys
):
    """Minor #6: Top-level run_key/attempt_key align with chosen status (metric_key_mismatch),
    and explicit metrics dict on a status='failed' evaluation is still validated."""
    import meta_research.target_execution_contract as tec

    monkeypatch.chdir(tmp_path)
    draft = tmp_path / "outputs" / "result.json"
    draft.parent.mkdir(parents=True)
    # Run-1 (status='failed') has an invalid metric value; Run-2 has metric_key_mismatch.
    orig_valid = tec.valid_target_metric_value
    monkeypatch.setattr(
        tec,
        "valid_target_metric_value",
        lambda v: False if v == "__invalid_sentinel__" else orig_valid(v),
    )
    document = {
        "metrics": {"effect": 1.0},
        "formal_runs": [
            {
                "run_key": "run-1",
                "evaluations": [
                    {"attempt_key": "eval-1", "status": "failed", "metrics": {"effect": "__invalid_sentinel__"}}
                ],
            },
            {
                "run_key": "run-2",
                "evaluations": [
                    {"attempt_key": "eval-2", "metrics": {"wrong_key": 2.0}}
                ],
            },
        ],
    }
    draft.write_text(canonical_json(document), encoding="utf-8")
    protocol = {
        "protocol_version_ref": "proto:1",
        "required_metric_keys": ["effect"],
        "optional_metric_keys": [],
    }
    assert check_result_main(["--check-result", canonical_json(protocol)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "metric_key_mismatch"
    assert out["run_key"] == "run-2"
    assert out["attempt_key"] == "eval-2"
    assert out["missing_metric_keys"] == ["effect"]
    assert out["unexpected_metric_keys"] == ["wrong_key"]
    assert len(out["failing_evaluations"]) == 2
    by_run = {item["run_key"]: item for item in out["failing_evaluations"]}
    assert by_run["run-1"]["invalid_metric_values"] == ["effect"]
    assert by_run["run-2"]["missing_metric_keys"] == ["effect"]

    # Also verify alignment when run-1 (executed, first in failing_evaluations) has invalid value
    # and run-2 (executed, second) has metric_key_mismatch -> top-level status & run_key align to run-2.
    document["formal_runs"][0]["evaluations"][0]["status"] = "executed"
    draft.write_text(canonical_json(document), encoding="utf-8")
    assert check_result_main(["--check-result", canonical_json(protocol)]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert out2["status"] == "metric_key_mismatch"
    assert out2["run_key"] == "run-2"
    assert out2["attempt_key"] == "eval-2"
    assert out2["failing_evaluations"][0]["run_key"] == "run-1"
    assert out2["failing_evaluations"][1]["run_key"] == "run-2"


def test_rg2_batch_query_avoids_n_plus_one_outcome_scans(tmp_path):
    """Minor #7: query_active_question_page and discover_questions execute at most 1
    rg_reasoning_outcome_decisions scan regardless of question count."""
    from sqlalchemy import event
    from test_public_manual_question_lifecycle import _confirm_waived_manual_question
    from test_research_datasets import _quest, _runtime

    runtime = _runtime(tmp_path / "rg2-batch-sql")
    try:
        root_q = _quest(runtime, "root-batch")
        human = runtime.owners.human_collaboration
        graph = runtime.owners.research_graph
        memory = runtime.owners.research_memory
        for i in range(4):
            _confirm_waived_manual_question(
                human, quest_ref=root_q.quest_ref, parent_question_ref=root_q.question_ref, key_prefix=f"c-{i}"
            )
            for _ in range(8):
                if not human.reconcile_once():
                    break

        outcome_queries: list[str] = []

        def _capture(conn, cursor, statement, parameters, context, executemany):
            if "rg_reasoning_outcome_decisions" in statement:
                outcome_queries.append(statement)

        event.listen(runtime._database._engine, "before_cursor_execute", _capture)
        try:
            active = graph.query_active_question_page(quest_ref=root_q.quest_ref, offset=0, limit=12)
            assert len(active["questions"]) == 5
            assert len(outcome_queries) == 1

            outcome_queries.clear()
            page = discover_questions(graph, memory, quest_ref=root_q.quest_ref, limit=12)
            assert len(page["items"]) == 5
            assert len(outcome_queries) == 1
        finally:
            event.remove(runtime._database._engine, "before_cursor_execute", _capture)
    finally:
        runtime.close()
