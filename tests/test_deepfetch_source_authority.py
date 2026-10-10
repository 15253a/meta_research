from __future__ import annotations

import pytest

from meta_research.deepfetch_sources import source_action
from meta_research.owners.common import canonical_hash
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill,
    _ReadyAutonomousAcquisitionProvider,
    _reach_autonomous_checkpoint,
    _tick_autonomous,
)
from test_public_manual_question_lifecycle import (
    DeterministicAcquisitionProvider,
    DeterministicManualDeepFetchProvider,
    _build_runtime,
    _open_and_confirm_seed,
)
from test_public_plan_stage import _DeterministicDraftingAdapter, _DeterministicProbe
from test_public_reasoning_stage import _reasoning_runtime


class SourceAttemptProvider(DeterministicManualDeepFetchProvider):
    def execute(self, request):
        context = SemanticCallContext(
            request.run_ref, request.attempt_ref, request.root_session_ref,
            request.fence_ref, canonical_hash(request.runtime_binding.as_dict()),
            "deepfetch", "turn-0", "deepfetch_source_action",
        )
        scope = self.runtime.owners.agent_runtime.verify_root_agent_runtime_scope(
            root_kind=context.root_kind, run_ref=context.run_ref,
            attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash,
        )
        assert scope["literature_access_mode"] == "provided_only"
        assert scope["acquisition_session_ref"] == self.issued.acquisition_session_ref
        assert request.literature_access_mode == "provided_only"
        with pytest.raises(SemanticMcpError, match="^deepfetch_source_action_provided_only$"):
            source_action(self.runtime.owners.agent_runtime, self.runtime.search_sources,
                context, {"source_id": "forbidden-source", "operation": "api_search", "query": "evidence"})
        return super().execute(request)


def _provided_creation(runtime, tmp_path, kind):
    source = tmp_path / "provided.md"
    source.write_text("# Provided evidence\nExact accepted material.\n", encoding="utf-8")
    intake = runtime.owners.research_memory.submit_asset_intake(
        AssetIntakeRequest(source_kind="local_path", custody_mode="linked_local",
            display_name=source.name, media_type="text/markdown", source_locator=str(source.resolve())),
        idempotency_key="provided-asset",
    )
    assert intake.asset is not None
    human = runtime.owners.human_collaboration
    opened = human.create_quest({}, "provided-open")
    observed = human.observe_host_compute(opened["initialization_id"],
        ["GPU-manual-lifecycle"], "provided-compute")
    draft = dict(observed["quest_draft"]["value"])
    draft.update({
        "goal": "Determine what the accepted material establishes.",
        "completion_criteria": "A bounded conclusion supported by the accepted material.",
        "time_budget": "30d", "route": "deepfetch" if kind == "initialization" else "direct",
        "literature": {"mode": "provided_only", "library_entry_url": "", "scope_exclusions": "",
            "accepted_material_bindings": [intake.asset.as_binding().as_dict()]},
        "background_and_initial_direction": "Use the supplied evidence.",
    })
    saved = human.revise_quest_draft(opened["initialization_id"], draft,
        observed["quest_draft"]["hash"], "provided-draft", observed["quest_draft"]["revision"])
    if kind == "initialization":
        human.prepare_acquisition_session(saved["initialization_id"], saved["quest_draft"]["hash"],
            "provided-acquisition", saved["quest_draft"]["revision"])
    human.generate_question_proposal(saved["initialization_id"], saved["quest_draft"]["hash"],
        "provided-proposal", saved["quest_draft"]["revision"])
    if kind == "initialization":
        return human.query_next_deepfetch_request()
    assert human.process_drafting_once()
    proposed = human.query_quest_creation(saved["initialization_id"])
    confirmation = dict(
        quest_draft_revision=proposed["quest_draft"]["revision"],
        quest_draft_hash=proposed["quest_draft"]["hash"],
        proposal_ref=proposed["proposal"]["ref"], proposal_hash=proposed["proposal"]["hash"],
    )
    preview = human.preview_confirmation(proposed["initialization_id"],
        **confirmation, idempotency_key="provided-preview")["confirmation_preview"]
    human.confirm_quest(proposed["initialization_id"], **confirmation,
        preview_ref=preview["ref"], preview_hash=preview["hash"], idempotency_key="provided-confirm")
    for _step in range(8):
        if not human.reconcile_once():
            break
    completed = human.query_quest_creation(opened["initialization_id"])
    assert completed["status"] == "completed"
    seeded = _open_and_confirm_seed(human, quest_ref=completed["quest_ref"],
        parent_question_ref=completed["question_ref"], key_prefix="provided-manual", deepfetch_preference="use")
    queued = human.start_manual_creation_deepfetch(seeded["context_ref"],
        expected_seed_ref=seeded["seed"]["ref"], expected_seed_hash=seeded["seed"]["hash"],
        idempotency_key="provided-manual-start")
    return human.query_deepfetch_request(queued["research_path"]["deepfetch"]["request_ref"])


@pytest.mark.parametrize("kind", ("initialization", "manual"))
def test_real_owner_provided_only_source_action_stops_before_registry_act(tmp_path, monkeypatch, kind):
    provider = SourceAttemptProvider()
    runtime = _build_runtime(tmp_path / "runtime", deepfetch_provider=provider,
        acquisition_provider=DeterministicAcquisitionProvider())
    provider.runtime = runtime
    calls = []

    def forbidden_act(**arguments):
        calls.append(arguments)
        raise AssertionError("provided_only must reject before source execution")

    monkeypatch.setattr(runtime.search_sources, "act", forbidden_act)
    try:
        provider.issued = _provided_creation(runtime, tmp_path, kind)
        assert provider.issued is not None
        runtime.owners.agent_runtime.execute_deepfetch(provider.issued, provider)
        assert len(provider.requests) == 1
        assert calls == []
    finally:
        runtime.close()


def test_autonomous_acquisition_binding_comes_from_validated_issued_request(tmp_path):
    runtime = _reasoning_runtime(tmp_path / "autonomous",
        reasoning_skill=_AutonomousReasoningSkill(require_source_literature=False),
        acquisition_provider=_ReadyAutonomousAcquisitionProvider(),
        host_compute_probe=_DeterministicProbe(), proposal_drafter=_DeterministicDraftingAdapter())
    try:
        _, _, checkpoint = _reach_autonomous_checkpoint(runtime, direct_quest=True)
        started = runtime.autonomous_creation.start(
            reasoning_checkpoint_ref=checkpoint["checkpoint_ref"],
            source_scientific_outcome_ref=checkpoint["scientific_outcome"]["outcome_ref"],
            idempotency_key="authority-autonomous-start")
        _tick_autonomous(runtime, "agent_runtime")
        _tick_autonomous(runtime, "advancement_engine")
        issued = runtime.owners.advancement_engine.query_autonomous_deepfetch_request(started["context_ref"])
        verifier = runtime.owners.agent_runtime._deepfetch_request_verifier
        assert verifier.query_deepfetch_acquisition_binding(issued.request_ref) == {
            "initialization_id": issued.initialization_id,
            "acquisition_session_ref": issued.acquisition_session_ref,
            "acquisition_config_hash": issued.acquisition_config_hash,
            "acquisition_runtime_binding_hash": issued.acquisition_runtime_binding_hash,
        }
    finally:
        runtime.close()
