from dataclasses import replace
from copy import deepcopy
import json
from types import SimpleNamespace as NS

import pytest

from meta_research.bundle_batch_access import verified_batch_plan_reader
from meta_research.bundle_skill import BundleSkillUnavailable
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError
from test_bundle_input_drift_recovery import _adapter, _request, _output


def _bind(adapter):
    request = _request(adapter, "batch")
    pack = {"accepted_formal_plan_binding": {"plan_document": request.plan_document}}
    request = replace(request, context_pack_hash=canonical_hash(pack))
    target_plan = deepcopy(request.initial_target_plan)
    target_plan["source_bindings"]["context_pack_hash"] = request.context_pack_hash
    request = replace(request, initial_target_plan=target_plan)
    stage = NS(request_ref=request.stage_request_ref, context_pack=pack,
               context_pack_ref=request.context_pack_ref, context_pack_hash=request.context_pack_hash,
               accepted_question=NS(question_ref="question", quest_ref="quest"),
               accepted_formal_plan=NS(formal_plan_ref=request.formal_plan_ref, plan_document=request.plan_document))
    run = NS(**{name: getattr(request, name) for name in
                ("run_ref", "attempt_ref", "root_session_ref", "fence_ref", "native_session_ref")},
             runtime_binding_hash=canonical_hash(request.runtime_binding.as_dict()))
    calls = []
    revoked = []
    def verify(**scope):
        calls.append(scope)
        if revoked:
            raise OwnerConflict("scope_revoked")
    ar = NS(verify_root_agent_runtime_scope=verify,
            query_managed_run=lambda _: {"run_kind": "bundle_stage", "cycle_ref": request.cycle_ref},
            query_bundle_stage_run=lambda _: run)
    ae = NS(query_bundle_stage_request=lambda _: stage)
    def reader(current):
        return verified_batch_plan_reader(current, advancement_engine=ae, agent_runtime=ar,
                                          research_graph=None, research_memory=None)
    adapter.bind_batch_plan_reader(reader)
    return request, reader, stage, run, calls, revoked


def test_new_batch_uses_authorized_plan_reference_and_keeps_signed_full_input(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request, _, _, _, calls, _ = _bind(adapter)
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    raw = (operation / "prompt.txt").read_text()
    view = json.loads(next(line.removeprefix("batch_context=") for line in raw.splitlines() if line.startswith("batch_context=")))
    assert "formal_plan" not in view
    assert view["formal_plan_reader"]["content_hash"] == canonical_hash(request.plan_document)
    signed_input = json.loads((operation / "business-input.json").read_text())
    assert signed_input["request"]["formal_plan"] == request.plan_document
    assert signed_input["request"]["target_commits"] == list(request.target_commits)
    assert calls and len(runner.calls) == 1
    original = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    adapter.bind_batch_plan_reader(lambda _: pytest.fail("sealed input must reconcile without re-render lookup"))
    adapter.propose_target_batch(request)
    assert len(runner.calls) == 1
    assert {name: (operation / name).read_bytes() for name in original} == original


def test_unknown_compact_operation_reconciles_without_new_reader_or_dispatch(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request, _, _, _, _, _ = _bind(adapter)
    adapter.propose_target_batch(request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    for name in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation / name).unlink(missing_ok=True)
    adapter.bind_batch_plan_reader(lambda _: pytest.fail("unknown operation must reconcile"))
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_reconciliation_pending"):
        adapter.propose_target_batch(request)
    assert len(runner.calls) == 1


def test_revocation_never_falls_back_to_full_plan(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("batch"))
    request, _, _, _, _, revoked = _bind(adapter)
    revoked.append(True)
    with pytest.raises(BundleSkillUnavailable, match="semantic_call_scope_stale"):
        adapter.propose_target_batch(request)
    assert not runner.calls


def test_authorized_missing_reader_keeps_body_and_wrong_native_is_rejected(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("batch"))
    request, reader, stage, run, calls, _ = _bind(adapter)
    stage.context_pack = {}
    assert reader(request) is None
    assert calls
    run.native_session_ref = "another-thread"
    with pytest.raises(SemanticMcpError, match="bundle_batch_plan_binding_stale"):
        reader(request)


def test_rollback_keeps_full_new_input_but_still_checks_current_scope(tmp_path, monkeypatch):
    adapter, runner = _adapter(tmp_path, _output("batch"))
    request, _, _, _, calls, _ = _bind(adapter)
    monkeypatch.setenv("META_RESEARCH_BUNDLE_BATCH_VIEW", "full")
    adapter.propose_target_batch(request)
    view = json.loads(next(line.removeprefix("batch_context=") for line in runner.calls[0][1].splitlines() if line.startswith("batch_context=")))
    assert view["formal_plan"] == request.plan_document
    assert calls


def test_plan_proof_requires_every_page_and_rejects_revocation_between_pages(tmp_path):
    adapter, _ = _adapter(tmp_path, _output("batch"))
    request, reader, stage, _, calls, revoked = _bind(adapter)
    plan = {**request.plan_document, "notes": "保留缺测与反例。" * 2000}
    stage.accepted_formal_plan.plan_document = plan
    stage.context_pack["accepted_formal_plan_binding"]["plan_document"] = plan
    stage.context_pack_hash = canonical_hash(stage.context_pack)
    request = replace(request, plan_document=plan, context_pack_hash=stage.context_pack_hash)
    assert reader(request)["plan_hash"] == canonical_hash(plan)
    assert len(calls) > 1
    calls.clear()
    original = stage.context_pack
    class RevokeOnRead(dict):
        def __getitem__(self, key):
            value = super().__getitem__(key)
            revoked.append(True)
            return value
    stage.context_pack = RevokeOnRead(original)
    with pytest.raises(SemanticMcpError, match="semantic_call_scope_stale"):
        reader(request)


def test_late_guidance_body_is_included_only_in_requested_batch_view(tmp_path):
    from meta_research.human_guidance import (FrozenGuidanceBinding, FrozenGuidanceCut,
        FrozenGuidanceDelivery, GuidanceOperationIdentity)
    adapter, _ = _adapter(tmp_path, _output("batch"))
    request = _request(adapter, "batch")
    binding = FrozenGuidanceBinding(GuidanceOperationIdentity("bundle", request.run_ref, "guidance-op"),
                                   "quest", "snapshot", "a" * 64)
    cut = FrozenGuidanceCut(binding, "provenance", (FrozenGuidanceDelivery(
        "delivery", json.dumps({"constraint_ref": "constraint", "revision": 2,
                               "text": "Late counterexample: keep the independent device check."}),
        True, None),))
    adapter.bind_human_guidance_authority(NS(freeze_operation_guidance=lambda _: cut))
    args = {key: getattr(request, key) for key in
            ("run_ref", "attempt_ref", "root_session_ref", "fence_ref", "job_ref")}
    args.update(runtime_binding_hash=canonical_hash(request.runtime_binding.as_dict()),
                operation_name="target-batch-1", prompt="new batch")
    rendered, actual = adapter._prepare_stage_guidance(**args, include_body=True)
    assert "Late counterexample: keep the independent device check." in rendered
    assert actual == binding
    ordinary, _ = adapter._prepare_stage_guidance(**args)
    assert "Late counterexample: keep the independent device check." not in ordinary
