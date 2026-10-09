import json
from types import SimpleNamespace

import pytest

from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.companion import CodexCompanionAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.external_mcp import ExternalMcpRuntime, parse_services
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.quest_drafting import IntentTurnRequest
from meta_research.writing_skill import CodexWritingSkillAdapter
from test_deepfetch_adapter import RecordingRunner, _request, _deepfetch_web_evidence_gate_output_schema
from test_external_mcp import service
from test_external_root_resident_mcp import _AcquisitionDelegate, _SequenceRunner, _fake_codex
from test_runtime_conditions_provider_prompts import _SignedRunner, _idea_call


def configured(tmp_path):
    runtime = ExternalMcpRuntime(tmp_path / "settings")
    runtime.configure_endpoint("http://127.0.0.1:18768")
    draft = service(tmp_path, research_instructions="Use chamber A for this experiment.")
    draft["connection"]["environment"]["EXTERNAL_MCP_AUDIT"] = str(tmp_path / "discovery.jsonl")
    runtime.save_config(services=parse_services([draft]), expected_revision=runtime.read_config().revision)
    return runtime


def changed_config(runtime, tmp_path):
    runtime.save_config(services=parse_services([service(tmp_path, allowed_root_kinds=[],
        research_instructions="Use chamber B for the next experiment.")]), expected_revision=runtime.read_config().revision)


def test_public_acquisition_preflight_before_quest_gets_external_context(tmp_path):
    external = configured(tmp_path)
    runner = _SequenceRunner([{"accepted": True, "human_request": None}])
    adapter = CodexAcquisitionRootAdapter(tmp_path / "acquisition", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "fake-acquisition")), process_runner=runner)
    adapter.bind_external_mcp(external)
    result = adapter.preflight(SimpleNamespace(session_ref="acquisition-before-quest", config_hash="9" * 64, generation=1))
    assert result.status == "ready"
    assert "META_RESEARCH_EXTERNAL_MCP_TOKEN" in runner.environments[0]
    assert "Use chamber A for this experiment." in runner.prompts[0]
    assert (tmp_path / "discovery.jsonl").read_text().splitlines() == ['"tools/list"']
    assert not (tmp_path / "calls.jsonl").exists()


def test_public_companion_before_quest_replays_frozen_context(tmp_path):
    external = configured(tmp_path)
    runner = _SequenceRunner([{"reply": "Clarify the goal."}])
    workspace = tmp_path / "companion"
    executable = str(_fake_codex(tmp_path / "fake-companion"))
    adapter = CodexCompanionAdapter(workspace, executable=executable, process_runner=runner)
    adapter.bind_external_mcp(external)
    request = IntentTurnRequest(initialization_id="initialization:before-quest", draft_revision=1, draft_hash="b" * 64,
        draft={}, message="What should we research?", native_session_ref=None, job_ref="companion:before-quest",
        creation_context_kind="quest_initialization", creation_context_ref=None, context_generation=None)
    first = adapter.reply(request)
    assert first.reply == "Clarify the goal."
    assert "Use chamber A for this experiment." in runner.prompts[0]
    assert "META_RESEARCH_EXTERNAL_MCP_TOKEN" in runner.environments[0]
    changed_config(external, tmp_path)
    restored = CodexCompanionAdapter(workspace, executable=executable, process_runner=runner)
    restored.bind_external_mcp(external)
    assert restored.reply(request) == first
    assert len(runner.prompts) == 1
    assert (tmp_path / "discovery.jsonl").read_text().splitlines() == ['"tools/list"']
    assert not (tmp_path / "calls.jsonl").exists()


def test_public_writing_draft_keeps_source_permissions_with_external_context(tmp_path):
    from test_writing_skill_adapter import _SequenceRunner as WritingRunner, _request as writing_request, _draft_output

    external = configured(tmp_path)
    runner = WritingRunner([_draft_output()])
    adapter = CodexWritingSkillAdapter(tmp_path / "writing", executable=str(_fake_codex(tmp_path / "fake-writing")), process_runner=runner)
    adapter.bind_external_mcp(external)
    draft = adapter.generate_draft(writing_request(adapter))
    assert "rare morphology remains visible" in draft.markdown
    assert "Use chamber A for this experiment." in runner.calls[0][1]
    assert "META_RESEARCH_EXTERNAL_MCP_TOKEN" in runner.environments[0]
    assert 'default_permissions="writing_snapshot"' in runner.calls[0][0]
    assert (tmp_path / "discovery.jsonl").read_text().splitlines() == ['"tools/list"']
    assert not (tmp_path / "calls.jsonl").exists()


def test_unbound_standalone_deepfetch_preserves_native_mcp_configuration(tmp_path):
    runner = RecordingRunner({"status": "web_evidence_ready"})
    adapter = CodexDeepFetchAdapter(tmp_path / "standalone", process_runner=runner)
    adapter._invoke_with_access(_request(), "web_evidence_gate=v1\nRead the evidence.",
        output_schema=_deepfetch_web_evidence_gate_output_schema(), timeout_seconds=None, access=None)
    argv = runner.calls[0][0]
    assert "--strict-config" in argv
    assert "mcp_servers={}" not in argv
    assert not any("meta_research_external" in argument for argument in argv)
    assert 'shell_environment_policy.inherit="none"' not in argv


def test_cancelled_job_reconciliation_accepts_and_verifies_frozen_external_binding(tmp_path):
    external = configured(tmp_path)
    workspace = tmp_path / "provider"
    runner = _SignedRunner()
    adapter = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    adapter.bind_external_mcp(external)
    first = _idea_call(adapter)
    directory = workspace / "provider-operations" / canonical_hash({"job_ref": "job:initial"}) / "primary"
    signed_before = (directory / "invocation.json").read_bytes()
    binding = json.loads(signed_before)["payload"]["external_mcp"]
    changed_config(external, tmp_path)
    restored = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    restored.bind_external_mcp(external)
    assert restored.reconcile_cancelled_job("job:initial") is True
    assert _idea_call(restored) == first
    assert (directory / "invocation.json").read_bytes() == signed_before
    assert len(runner.calls) == 1
    assert (tmp_path / "discovery.jsonl").read_text().splitlines() == ['"tools/list"']
    snapshot_path = tmp_path / "settings/external-mcp/operations" / (binding["operation_key"] + ".json")
    snapshot_path.unlink()
    assert restored.reconcile_cancelled_job("job:initial") is False
    assert len(runner.calls) == 1


def test_sealed_transport_failure_replay_accepts_frozen_external_binding(tmp_path):
    external = configured(tmp_path)
    workspace = tmp_path / "provider"
    runner = _SignedRunner()
    runner.output = ["invalid object result"]
    adapter = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    adapter.bind_external_mcp(external)
    with pytest.raises(IdeaSkillUnavailable) as initial:
        _idea_call(adapter)
    assert initial.value.code == "idea_primary_result_contract_invalid"
    assert initial.value.rejected_detail_code == "codex_output_invalid"
    assert initial.value.recovery_checkpoint is not None
    directory = workspace / "provider-operations" / canonical_hash({"job_ref": "job:initial"}) / "primary"
    signed_before = (directory / "invocation.json").read_bytes()
    changed_config(external, tmp_path)
    restored = CodexIdeaSkillAdapter(workspace, process_runner=runner)
    restored.bind_external_mcp(external)
    with pytest.raises(IdeaSkillUnavailable) as replay:
        _idea_call(restored)
    assert replay.value.code == initial.value.code
    assert replay.value.rejected_detail_code == initial.value.rejected_detail_code
    assert replay.value.recovery_checkpoint == initial.value.recovery_checkpoint
    assert replay.value.rejected_candidate == initial.value.rejected_candidate
    assert len(runner.calls) == 1
    assert (directory / "invocation.json").read_bytes() == signed_before
    assert (tmp_path / "discovery.jsonl").read_text().splitlines() == ['"tools/list"']
