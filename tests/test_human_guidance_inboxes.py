from __future__ import annotations

from dataclasses import replace

import pytest

from meta_research.human_guidance import GuidanceRuntimeScope, StageGuidanceOperation
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_human_collaboration_web import (
    _runtime as _web_runtime, _confirm_direct_quest as _web_quest, _authenticated_client,
)
from test_public_idea_stage import _DeterministicIdeaSkill, _runtime, _confirm_direct_quest


@pytest.mark.parametrize("strength", [1, 2, 3, 4, 5, None])
def test_explicit_http_submission_preserves_original_text_and_hashed_strength(tmp_path, strength):
    runtime = _web_runtime(tmp_path / "runtime")
    try:
        quest = _web_quest(runtime)["quest_ref"]
        client, headers = _authenticated_client(runtime)
        body = {"scope_ref": "quest:" + quest, "text": "  保留原文\nContinue the audit.  "}
        if strength is not None:
            body["strength"] = strength
        proposal = runtime.owners.human_collaboration.record_agent_proposal("quest:" + quest, {
            "proposal_kind": "soft_constraint", "text": body["text"],
            "assistant_understanding": "保留原文要求并继续核验。", "applies_to": ["整个 Quest"],
            "semantic_scope": {"kind": "quest", "quest_ref": quest},
            "strength": strength or 3, "preserve_conditions": [], "work_materials": None,
        }, "reviewed-guidance")
        body.update(proposal_ref=proposal["proposal_ref"], expected_proposal_hash=proposal["proposal_hash"])
        headers["Idempotency-Key"] = "submit-one"
        response = client.post("/api/v1/human-collaboration/guidance", json=body, headers=headers)
        assert response.status_code == 201, response.text
        accepted = response.json()
        assert accepted["guidance"] == proposal["proposal"]
        binding, = runtime.owners.human_collaboration.query_active_guidance_bindings("quest:" + quest)
        assert binding["guidance_hash"] == canonical_hash(accepted["guidance"])
        assert client.post("/api/v1/human-collaboration/guidance", json=body, headers=headers).json() == accepted
        conflict = client.post("/api/v1/human-collaboration/guidance",
            json={**body, "text": "Changed"}, headers=headers)
        assert conflict.status_code == 409
        projected, = runtime.owners.human_collaboration.query_collaboration_projection(("quest:" + quest,))["soft_constraints"]
        assert projected["strength"] == (strength or 3)
        assert projected["deliveries"] == []
        client.close()
    finally:
        runtime.close()


@pytest.mark.parametrize("strength", [True, False, 0, 6, 3.0, "3", None])
def test_http_rejects_malformed_strengths(tmp_path, strength):
    runtime = _web_runtime(tmp_path / "runtime")
    try:
        quest = _web_quest(runtime)["quest_ref"]
        client, headers = _authenticated_client(runtime)
        headers["Idempotency-Key"] = "invalid-strength"
        response = client.post("/api/v1/human-collaboration/guidance",
            json={"scope_ref": "quest:" + quest, "text": "Keep the audit", "strength": strength}, headers=headers)
        assert response.status_code == 422
        response = client.post("/api/v1/human-collaboration/guidance",
            json={"scope_ref": "quest:" + quest, "text": " \n\t "}, headers=headers)
        assert response.status_code == 409
        client.close()
    finally:
        runtime.close()


class _InboxIdea(_DeterministicIdeaSkill):
    def __init__(self, on_primary, on_review):
        super().__init__()
        self.on_primary = on_primary
        self.on_review = on_review

    def operation(self, request, name):
        scope = GuidanceRuntimeScope("idea", request.run_ref, request.attempt_ref,
            request.root_session_ref, request.fence_ref, canonical_hash(request.runtime_binding.as_dict()))
        return StageGuidanceOperation(scope, request.job_ref, name)

    def generate_draft(self, request):
        self.on_primary(self.runtime.owners.human_collaboration, self.operation(request, "primary"))
        return super().generate_draft(request)

    def review_draft(self, request, draft):
        self.on_review(self.runtime.owners.human_collaboration, self.operation(request, "review"))
        return super().review_draft(request, draft)


def _run_idea(tmp_path, provider):
    runtime = _runtime(tmp_path / "runtime", provider)
    provider.runtime = runtime
    quest = _confirm_direct_quest(runtime)["quest_ref"]
    return runtime, quest


def _finish(runtime):
    for _ in range(6):
        runtime.idea_stage.process_once()


def test_empty_first_cut_survives_late_submit_and_new_operation_receives_it(tmp_path):
    observed = {}
    def primary(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        assert cut.deliveries == ()
        observed["empty"] = cut.binding.snapshot_ref
        hc.submit_human_guidance(quest_ref=cut.binding.quest_ref, original_text="Inspect device drift.", idempotency_key="late")
        recovered = hc.freeze_operation_guidance(operation)
        assert recovered.binding.snapshot_ref == observed["empty"]
        assert recovered.deliveries == ()
        with pytest.raises(OwnerConflict, match="guidance_provider_unit_unbound"):
            hc.freeze_operation_guidance(replace(operation, job_ref="foreign-job"))
    def review(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        assert delivery.needs_treatment is True
        read = hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
            delivery_ref=delivery.delivery_ref, effect_id="read")
        assert read["original_text"] == "Inspect device drift."
        assert read["strength"] == 3
        observed["review"] = cut.binding.snapshot_ref
    provider = _InboxIdea(primary, review)
    runtime, _ = _run_idea(tmp_path, provider)
    try:
        _finish(runtime)
        assert observed["review"] != observed["empty"]
    finally:
        runtime.close()


def test_exact_read_feedback_replay_and_continuing_constraints(tmp_path):
    observed = {}
    def primary(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        command = dict(scope=operation.scope, binding=cut.binding, delivery_ref=delivery.delivery_ref,
            effect_id="decision", understanding="The audit must isolate drift.",
            changes="Compare each device separately.", continuing_work="Continue morphology checks.",
            reasons="Device mixing hides failures.", disposition="applied")
        with pytest.raises(OwnerConflict, match="guidance_exact_read_required"):
            hc.feedback_operation_guidance(**command)
        page = hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
            delivery_ref=delivery.delivery_ref, effect_id="page", limit=8)
        assert page["full_read"] is False
        with pytest.raises(OwnerConflict, match="guidance_exact_read_required"):
            hc.feedback_operation_guidance(**command)
        read = hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
            delivery_ref=delivery.delivery_ref, effect_id="full")
        assert read["original_text"] == "Audit each device."
        assert read["full_read"] is True
        assert hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
            delivery_ref=delivery.delivery_ref, effect_id="full", reconcile=True) == read
        with pytest.raises(OwnerConflict, match="guidance_effect_conflict"):
            hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
                delivery_ref=delivery.delivery_ref, effect_id="full", limit=12)
        receipt = hc.feedback_operation_guidance(**command)
        assert receipt["changes"] == "Compare each device separately."
        assert hc.feedback_operation_guidance(**command) == receipt
        assert hc.feedback_operation_guidance(**command, reconcile=True) == receipt
        with pytest.raises(OwnerConflict, match="guidance_effect_conflict"):
            hc.feedback_operation_guidance(**{**command, "changes": "Changed"})
        with pytest.raises(OwnerConflict, match="guidance_already_treated"):
            hc.feedback_operation_guidance(**{**command, "effect_id": "another"})
        observed["receipt"] = receipt
    def review(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        assert delivery.needs_treatment is False
        page = hc.read_operation_guidance(scope=operation.scope, binding=cut.binding)
        assert page["deliveries"][0]["prior_treatment"] == observed["receipt"]
        assert page["deliveries"][0]["strength"] == 4
        observed["continuing"] = page
    provider = _InboxIdea(primary, review)
    runtime, quest = _run_idea(tmp_path, provider)
    try:
        guide = runtime.owners.human_collaboration.submit_human_guidance(
            quest_ref=quest, original_text="Audit each device.", strength=4, idempotency_key="audit")
        _finish(runtime)
        assert observed["continuing"]["summary_only"] is True
        projection = runtime.owners.human_collaboration.query_guidance_deliveries(guide["constraint_ref"])
        assert len(projection) == 2
        assert projection[0]["received_at"] is not None
        assert projection[0]["read_at"] is not None
        assert projection[1]["needs_treatment"] is False
        assert projection[1]["read_at"] is None
    finally:
        runtime.close()
