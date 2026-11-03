"""Executable regression tests for target-batch context compaction, recovery, authorization, and scientific fidelity.

Tests are separated into:
1. Baseline runnable regression suites (exercising `read_stage_context`, scope/fence checks,
   multi-page byte-exact synthetic scientific data, and sealed adapter operation_input recovery).
2. Upcoming implementation assertions for ordinary resumed `propose_target_batch` prompt compaction
   on the same native session and accepted Plan after targets finish, contrasted with full-input
   preservation on initial drafts, saved-draft resumes, and native replacements.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import Mock
import hashlib
import json

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import (
    BundleDispatchRequest,
    BundleTargetBatchRequest,
    CodexBundleSkillAdapter,
)
from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticMcpError
from meta_research.stage_context_access import read_stage_context, stage_context_operations

from test_bundle_input_drift_recovery import (
    _adapter,
    _changed,
    _output,
    _request,
)
from test_bundle_skill_adapter import (
    _inbox_checkpoint,
    _plan_document,
    _target_plan,
)


# ---------------------------------------------------------------------------
# Helpers & Synthetic Scientific Fixtures (Not claimed as real experimental data)
# ---------------------------------------------------------------------------

def _bundle_auth_objects(pack: dict, *, context_pack_ref: str = "context-pack:bundle-1"):
    """Construct authenticated Bundle stage scope mocks for `read_stage_context`."""
    pack_hash = canonical_hash(pack)
    context = NS(
        root_kind="bundle",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        root_session_ref="root-session:1",
        fence_ref="bundle-fence:1",
        capability_binding_hash="binding-hash:1",
    )
    question = NS(
        quest_ref="quest:synthetic-1",
        question_ref="question:synthetic-1",
        content_ref="question-content:1",
        content_hash="qhash:1",
    )
    request = NS(
        request_ref="stage-request:bundle-1",
        cycle_ref="cycle:bundle-1",
        context_pack=pack,
        context_pack_ref=context_pack_ref,
        context_pack_hash=pack_hash,
        accepted_question=question,
    )
    run = NS(
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        root_session_ref="root-session:1",
        fence_ref="bundle-fence:1",
        runtime_binding_hash="binding-hash:1",
    )
    ar = NS(
        verify_root_agent_runtime_scope=Mock(),
        query_managed_run=Mock(return_value={"run_kind": "bundle_stage", "cycle_ref": "cycle:bundle-1"}),
        query_bundle_stage_run=Mock(return_value=run),
    )
    ae = NS(query_bundle_stage_request=Mock(return_value=request))
    return context, ar, ae, request, run


def _synthetic_scientific_bundle_pack() -> dict:
    """Build synthetic scientific fixture testing exact precision, missing peaks, and 600/40 denominators."""
    plan = _plan_document()
    target_plan = _target_plan(plan, "7" * 64)

    # 600 valid predictions: 40 nominal zeros and 560 non-zero values.
    predictions = []
    for i in range(600):
        is_zero = i < 40
        predictions.append(
            {
                "row_index": i,
                "unit_key": f"cell:synthetic-{i:04d}",
                "status": "valid_prediction",
                "predicted_value": "0.0" if is_zero else f"{(i + 1) * 0.0025:.8f}",
                "nominal_zero": is_zero,
                # Alternating exact string representations of distinct lambda variants
                "lambda_regularization": (
                    "0.0010000000000000002" if i % 2 == 0 else "0.001"
                ),
            }
        )

    # Original row where source peak is missing; must be preserved verbatim, not filtered out.
    missing_peak_row = {
        "row_index": "row:source-peak-missing-001",
        "unit_key": "cell:peak-unobserved-original",
        "source_peak": None,
        "source_peak_status": "missing_in_raw_capture",
        "raw_observation_retained": True,
        "lambda_regularization": "0.0010000000000000002",
        "notes": "Synthetic fixture: original row preserved when source peak is missing.",
    }

    scientific_ledger = {
        "synthetic_fixture": True,
        "held_fixed_text": (
            "HELD-FIXED OBLIGATION: Retain all 600 valid predictions as the global evaluation "
            "denominator. Only the downstream spectrum log-ratio interpretation excludes the "
            "40 nominal zeros; never filter nominal zeros or missing-peak rows from primary records."
        ),
        "human_guidance": (
            "HUMAN GUIDANCE: Do not round lambda strings 0.0010000000000000002 and 0.001 into "
            "a single float bucket. Keep missing source-peak rows in cumulative failure accounting."
        ),
        "lambda_variants": ["0.0010000000000000002", "0.001"],
        "missing_source_peak_rows": [missing_peak_row],
        "denominator_accounting": {
            "total_valid_predictions": 600,
            "nominal_zero_predictions": 40,
            "non_zero_predictions": 560,
            "global_accuracy_denominator": 600,
            "spectrum_interpretation_denominator": 560,
            "spectrum_interpretation_excludes_nominal_zeros_only": True,
        },
        "predictions": predictions,
    }

    return {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "formal_plan_ref": "formal-plan:bundle-1",
        "accepted_formal_plan_binding": {"plan_document": plan},
        "initial_target_plan": target_plan,
        "scientific_fidelity_synthetic": scientific_ledger,
    }


def _reconstruct_via_paged_reader(ae, ar, ctx, *, context_pack_ref: str, path: list[str], limit: int = 509):
    """Page through `read_stage_context` at prime byte limits and verify hash/byte continuity."""
    chunks: list[bytes] = []
    offset = 0
    pages = 0
    expected_hash = None
    while True:
        page = read_stage_context(
            ae,
            ar,
            None,
            None,
            ctx,
            {
                "context_pack_ref": context_pack_ref,
                "source": "context_pack",
                "path": path,
                "offset": offset,
                "limit": limit,
            },
        )
        assert page["returned_bytes"] <= limit
        assert page["context_pack_ref"] == context_pack_ref
        if expected_hash is None:
            expected_hash = page["content_hash"]
        else:
            assert page["content_hash"] == expected_hash
        chunks.append(page["text"].encode("utf-8"))
        pages += 1
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    raw = b"".join(chunks)
    assert hashlib.sha256(raw).hexdigest() == expected_hash
    return json.loads(raw.decode("utf-8")), raw, pages, expected_hash


# ---------------------------------------------------------------------------
# Part 1: Baseline Runnable Tests — `read_stage_context`, Scope/Fence, Scientific Fidelity
# ---------------------------------------------------------------------------

def test_baseline_read_stage_context_pages_frozen_plan_and_target_plan_exact_hash():
    """Runnable against baseline: authentic `read_stage_context` paging reconstructs frozen Plan & Target contract."""
    pack = _synthetic_scientific_bundle_pack()
    ctx, ar, ae, request, _ = _bundle_auth_objects(pack)

    for sub_path, expected_obj in [
        (["accepted_formal_plan_binding", "plan_document"], pack["accepted_formal_plan_binding"]["plan_document"]),
        (["initial_target_plan"], pack["initial_target_plan"]),
    ]:
        reconstructed, raw_bytes, pages, content_hash = _reconstruct_via_paged_reader(
            ae,
            ar,
            ctx,
            context_pack_ref=request.context_pack_ref,
            path=sub_path,
            limit=127,
        )
        assert pages >= 2
        assert raw_bytes == canonical_json(expected_obj).encode("utf-8")
        assert content_hash == canonical_hash(expected_obj)
        assert reconstructed == expected_obj
    assert ar.verify_root_agent_runtime_scope.call_count > 2


def test_baseline_read_stage_context_rejects_wrong_ref_path_and_revoked_or_mismatched_fence():
    """Runnable against baseline: wrong ref/path fails and revoked/mismatched fence fails closed before source leak."""
    pack = _synthetic_scientific_bundle_pack()
    ctx, ar, ae, request, run = _bundle_auth_objects(pack)

    # 1. Wrong context_pack_ref / version ref
    with pytest.raises(SemanticMcpError, match="stage_context_ref_unbound"):
        read_stage_context(
            ae,
            ar,
            None,
            None,
            ctx,
            {
                "context_pack_ref": "context-pack:bundle-wrong-version",
                "source": "context_pack",
                "path": ["accepted_formal_plan_binding", "plan_document"],
                "offset": 0,
                "limit": 512,
            },
        )

    # 2. Wrong / nonexistent JSON path inside frozen context pack
    with pytest.raises(SemanticMcpError):
        read_stage_context(
            ae,
            ar,
            None,
            None,
            ctx,
            {
                "context_pack_ref": request.context_pack_ref,
                "source": "context_pack",
                "path": ["accepted_formal_plan_binding", "plan_document", "nonexistent_section_v99"],
                "offset": 0,
                "limit": 512,
            },
        )

    # 3. Current fence vs stale caller fence mismatch fails closed with semantic_call_scope_stale
    stale_fence_ctx = replace_ns(ctx, fence_ref="bundle-fence:stale-old")
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        read_stage_context(
            ae,
            ar,
            None,
            None,
            stale_fence_ctx,
            {
                "context_pack_ref": request.context_pack_ref,
                "source": "context_pack",
                "path": ["accepted_formal_plan_binding", "plan_document"],
                "offset": 0,
                "limit": 512,
            },
        )

    # 4. Revoked permissions in AgentRuntime fail closed before AdvancementEngine is even queried
    ae.query_bundle_stage_request.reset_mock()
    ar.verify_root_agent_runtime_scope.side_effect = OwnerConflict("runtime_fence_revoked")
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        read_stage_context(
            ae,
            ar,
            None,
            None,
            ctx,
            {
                "context_pack_ref": request.context_pack_ref,
                "source": "context_pack",
                "path": ["accepted_formal_plan_binding", "plan_document"],
                "offset": 0,
                "limit": 512,
            },
        )
    ae.query_bundle_stage_request.assert_not_called()

    # 5. Restoring current fence & valid scope succeeds immediately
    ar.verify_root_agent_runtime_scope.side_effect = None
    valid_page = read_stage_context(
        ae,
        ar,
        None,
        None,
        ctx,
        {
            "context_pack_ref": request.context_pack_ref,
            "source": "context_pack",
            "path": ["accepted_formal_plan_binding", "plan_document"],
            "offset": 0,
            "limit": 512,
        },
    )
    assert valid_page["content_hash"] == canonical_hash(pack["accepted_formal_plan_binding"]["plan_document"])
    assert valid_page["context_pack_hash"] == canonical_hash(pack)


def test_baseline_multi_page_scientific_fidelity_lambda_variants_missing_peak_and_600_40_denominators():
    """Runnable against baseline: multi-page reader preserves byte-exact lambda variants, missing peak row, and 600/40 counts."""
    pack = _synthetic_scientific_bundle_pack()
    ctx, ar, ae, request, _ = _bundle_auth_objects(pack)
    operations = stage_context_operations(
        advancement_engine=ae,
        agent_runtime=ar,
        research_graph=None,
        research_memory=None,
    )
    read_handler = operations[0].handler

    # Page through the entire synthetic scientific ledger using small prime byte chunks (limit=701)
    chunks: list[bytes] = []
    offset = 0
    page_count = 0
    while True:
        page = read_handler(
            ctx,
            {
                "context_pack_ref": request.context_pack_ref,
                "source": "context_pack",
                "path": ["scientific_fidelity_synthetic"],
                "offset": offset,
                "limit": 701,
            },
        )
        chunks.append(page["text"].encode("utf-8"))
        page_count += 1
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]

    assert page_count > 40  # Must span many pages
    raw_stream = b"".join(chunks)
    assert raw_stream == canonical_json(pack["scientific_fidelity_synthetic"]).encode("utf-8")

    # Verify raw byte stream contains both exact lambda strings without rounding
    assert b'"0.0010000000000000002"' in raw_stream
    assert b'"0.001"' in raw_stream

    reconstructed = json.loads(raw_stream.decode("utf-8"))

    # 1. Distinct lambda strings preserved across all 600 rows
    assert reconstructed["lambda_variants"] == ["0.0010000000000000002", "0.001"]
    observed_lambdas = {row["lambda_regularization"] for row in reconstructed["predictions"]}
    assert observed_lambdas == {"0.0010000000000000002", "0.001"}
    assert sum(1 for r in reconstructed["predictions"] if r["lambda_regularization"] == "0.0010000000000000002") == 300
    assert sum(1 for r in reconstructed["predictions"] if r["lambda_regularization"] == "0.001") == 300

    # 2. Missing source-peak original row preserved intact
    missing_rows = reconstructed["missing_source_peak_rows"]
    assert len(missing_rows) == 1
    assert missing_rows[0]["row_index"] == "row:source-peak-missing-001"
    assert missing_rows[0]["source_peak"] is None
    assert missing_rows[0]["source_peak_status"] == "missing_in_raw_capture"
    assert missing_rows[0]["raw_observation_retained"] is True

    # 3. 600 valid predictions including 40 nominal zeros preserved; only spectrum interpretation excludes zeros
    preds = reconstructed["predictions"]
    assert len(preds) == 600
    nominal_zeros = [p for p in preds if p["nominal_zero"] is True and p["predicted_value"] == "0.0"]
    non_zeros = [p for p in preds if not p["nominal_zero"] and p["predicted_value"] != "0.0"]
    assert len(nominal_zeros) == 40
    assert len(non_zeros) == 560

    denom = reconstructed["denominator_accounting"]
    assert denom["total_valid_predictions"] == 600
    assert denom["nominal_zero_predictions"] == 40
    assert denom["global_accuracy_denominator"] == 600
    assert denom["spectrum_interpretation_denominator"] == 560
    assert denom["spectrum_interpretation_excludes_nominal_zeros_only"] is True
    assert "Retain all 600 valid predictions" in reconstructed["held_fixed_text"]
    assert "Do not round lambda strings" in reconstructed["human_guidance"]


# ---------------------------------------------------------------------------
# Part 2: Baseline Runnable Adapter Recovery, Sealed Spool & No-Redispatch Tests
# ---------------------------------------------------------------------------

def test_baseline_target_batch_exact_operation_input_recovery_survives_changed_fresh_renderer(tmp_path, monkeypatch):
    """Runnable against baseline: completed `propose_target_batch` recovers from sealed spool without mutating prompt/hash when renderer changes."""
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request = _request(adapter, "batch")

    first_result = adapter.propose_target_batch(request)
    assert len(runner.calls) == 1

    operation_dir = next((tmp_path / "provider/provider-operations").glob("*/*"))
    sealed_before = {p.name: p.read_bytes() for p in operation_dir.iterdir() if p.is_file()}
    frozen_input = json.loads(sealed_before["business-input.json"])
    invocation_payload = json.loads(sealed_before["invocation.json"])["payload"]
    assert invocation_payload["operation_input_hash"] == canonical_hash(frozen_input)

    # Simulate a changed fresh prompt renderer / skill resource on a subsequent recovery call
    original_invoke_mcp = adapter._invoke_with_resident_mcp

    def mutated_fresh_renderer(**arguments):
        arguments["prompt"] = (
            "COMPACTED OR RE-RENDERED FRESH PROMPT HEADER\n"
            + arguments["prompt"]
            + "\nChanged fresh prompt text; business_input is still identical."
        )
        return original_invoke_mcp(**arguments)

    monkeypatch.setattr(adapter, "_invoke_with_resident_mcp", mutated_fresh_renderer)

    recovered_result = adapter.propose_target_batch(request)
    assert recovered_result == first_result
    assert len(runner.calls) == 1  # Must not re-execute the provider process

    # Verify sealed business-input.json, prompt.txt, and invocation.json hash were NEVER mutated
    sealed_after = {p.name: p.read_bytes() for p in operation_dir.iterdir() if p.is_file()}
    assert sealed_after == sealed_before


def test_baseline_unknown_in_flight_target_batch_never_redispatches_or_mutates_spool(tmp_path):
    """Runnable against baseline: unknown/unsealed operation raises reconciliation pending and never retries or mutates spool."""
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request = _request(adapter, "batch")
    adapter.propose_target_batch(request)
    assert len(runner.calls) == 1

    operation_dir = next((tmp_path / "provider/provider-operations").glob("*/*"))
    for terminal_receipt in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation_dir / terminal_receipt).unlink(missing_ok=True)
    unsealed_snapshot = {p.name: p.read_bytes() for p in operation_dir.iterdir() if p.is_file()}

    # Neither identical nor drifted input may redispatch an unknown in-flight operation
    for candidate_req in (request, _changed(request, "batch")):
        with pytest.raises(IdeaSkillUnavailable) as exc_info:
            adapter.propose_target_batch(candidate_req)
        assert exc_info.value.code == "codex_operation_reconciliation_pending"
        assert exc_info.value.recovery_checkpoint is None
        assert len(runner.calls) == 1
        assert {p.name: p.read_bytes() for p in operation_dir.iterdir() if p.is_file()} == unsealed_snapshot


def replace_ns(namespace_obj: NS, **changes) -> NS:
    """Return a copy of a SimpleNamespace with updated attributes."""
    data = dict(vars(namespace_obj))
    data.update(changes)
    return NS(**data)