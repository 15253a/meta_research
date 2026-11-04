from copy import deepcopy
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path

import pytest

from meta_research import bundle_batch_access as access
from meta_research.bundle_batch_delivery import verified_batch_delivery_baseline
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import canonical_json
from meta_research.semantic_mcp import SemanticMcpError
from test_bundle_batch_access import _bind
from test_bundle_batch_delivery import _previous, _proposals
from test_bundle_exact_sources import committed_targets, _sources, _read
from test_bundle_input_drift_recovery import _adapter, _output


def test_host_probes_once_per_used_source_and_descriptors_still_read_all(committed_targets, monkeypatch):
    runtime, graph, context, operation, request = _sources(committed_targets)
    owner = runtime.owners
    stage = owner.advancement_engine.query_bundle_stage_request(graph.cycle_ref)
    request.formal_plan_ref = stage.accepted_formal_plan.formal_plan_ref
    request.plan_document = stage.accepted_formal_plan.plan_document
    calls = []
    original = access.read_stage_context

    def observe(*args, **kwargs):
        calls.append(args[-1])
        return original(*args, **kwargs)

    monkeypatch.setattr(access, "read_stage_context", observe)
    dependencies = dict(advancement_engine=owner.advancement_engine, agent_runtime=owner.agent_runtime,
                        research_graph=owner.research_graph, research_memory=owner.research_memory)
    plan = access.verified_batch_plan_reader(request, **dependencies)
    sources = access.verified_batch_source_readers(request, **dependencies)
    assert len(calls) == 2 + len(request.current_targets) + len(request.target_commits)
    assert all(call["offset"] == 0 and call["limit"] == 1 for call in calls)
    assert not any(call["source"] == "bundle_initial_target_plan" and not call["path"] for call in calls)
    assert "initial_target_plan" not in sources
    expected = [(plan, request.plan_document),
                (sources["completion_contract"], request.initial_target_plan["completion_contract"])]
    expected.extend((sources["current_targets"][item["target_ref"]], item) for item in request.current_targets)
    expected.extend((sources["target_commits"][item["commit_ref"]], item) for item in request.target_commits)
    for descriptor, document in expected:
        assert descriptor["arguments"]["limit"] == 16384
        assert _read(operation, context, descriptor["arguments"])[0] == canonical_json(document)


def test_probe_still_checks_whole_content_after_same_first_byte(committed_targets):
    runtime, _, _, _, request = _sources(committed_targets)
    old = request.current_targets[0]
    changed = deepcopy(old)
    changed["spec"]["notes"] = "changed beyond the first byte"
    assert canonical_json(old)[0] == canonical_json(changed)[0]
    request.current_targets = (changed, *request.current_targets[1:])
    owner = runtime.owners
    with pytest.raises(SemanticMcpError, match="stage_bundle_version_unbound|bundle_batch_source_binding_stale"):
        access.verified_batch_source_readers(request, advancement_engine=owner.advancement_engine,
            agent_runtime=owner.agent_runtime, research_graph=owner.research_graph,
            research_memory=owner.research_memory)


def test_full_mode_does_not_probe_unused_readers(tmp_path, monkeypatch):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    request, *_ = _bind(adapter)
    monkeypatch.setenv("META_RESEARCH_BUNDLE_BATCH_VIEW", "full")
    adapter.bind_batch_plan_reader(lambda _: pytest.fail("full mode used Plan reader"))
    adapter.bind_batch_source_readers(lambda _: pytest.fail("full mode used source readers"))
    adapter.propose_target_batch(request)
    assert len(runner.calls) == 1


def test_delivery_reads_business_at_most_twice_without_skipping_success_proof(tmp_path, monkeypatch):
    adapter, runner, request, operation = _previous(tmp_path)
    original = Path.open
    opens = []

    def observe(path, *args, **kwargs):
        if path.parent == operation:
            opens.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    baseline = verified_batch_delivery_baseline(adapter, replace(request, base_generation=1),
        verified_proposals=_proposals(request),
        verified_proposal_checkpoints={"proposal:accepted": request.inbox_checkpoint})
    assert baseline["current_targets"] == list(request.current_targets)
    assert 1 <= opens.count("business-input.json") <= 2
    assert {"supervisor-exit.json", "stdout.jsonl", "last-message.json", "completed.json"} <= set(opens)
    assert len(runner.calls) == 1


@pytest.mark.parametrize("body", ["{", "[]", '{ "unsealed": true }'])
def test_bad_business_is_rejected_before_completion_or_reconciliation(tmp_path, monkeypatch, body):
    import meta_research.bundle_batch_delivery as delivery
    adapter, _, request, operation = _previous(tmp_path)
    (operation / "business-input.json").write_text(body, encoding="utf8")
    monkeypatch.setattr(delivery, "_read_completed_operation", lambda *a, **kw: pytest.fail("invalid business reached completion"))
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
        verified_batch_delivery_baseline(adapter, replace(request, base_generation=1))


def test_foreign_identity_is_rejected_before_unknown_result_reconciliation(tmp_path, monkeypatch):
    import meta_research.bundle_batch_delivery as delivery
    adapter, _, request, operation = _previous(tmp_path)
    for name in ("exit.json", "completed.json", "supervisor-exit.json"):
        (operation / name).unlink()
    monkeypatch.setattr(delivery, "_read_completed_operation", lambda *a, **kw: pytest.fail("foreign identity reached reconciliation"))
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_identity_conflict"):
        verified_batch_delivery_baseline(adapter, replace(request, base_generation=1, native_session_ref="foreign"))


def test_source_probe_rejects_wrong_content_hash_from_authorized_reader(committed_targets, monkeypatch):
    runtime, _, _, _, request = _sources(committed_targets)
    original = access.read_stage_context

    def wrong_hash(*args, **kwargs):
        return {**original(*args, **kwargs), "content_hash": "0" * 64}

    monkeypatch.setattr(access, "read_stage_context", wrong_hash)
    owner = runtime.owners
    with pytest.raises(SemanticMcpError, match="^bundle_batch_source_binding_stale$"):
        access.verified_batch_source_readers(request, advancement_engine=owner.advancement_engine,
            agent_runtime=owner.agent_runtime, research_graph=owner.research_graph,
            research_memory=owner.research_memory)


def test_signed_invocation_without_business_hash_cannot_supply_delivery_basis(tmp_path, monkeypatch):
    import meta_research.bundle_batch_delivery as delivery
    adapter, _, request, operation = _previous(tmp_path)
    path = operation / "invocation.json"
    envelope = json.loads(path.read_text(encoding="utf8"))
    del envelope["payload"]["operation_input_hash"]
    _, key = adapter._transport_key()
    envelope["seal"] = hmac.new(key, canonical_json(envelope["payload"]).encode("utf8"), hashlib.sha256).hexdigest()
    path.write_text(canonical_json(envelope), encoding="utf8")
    monkeypatch.setattr(delivery, "_read_completed_operation", lambda *a, **kw: pytest.fail("unbound business reached completion"))
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
        verified_batch_delivery_baseline(adapter, replace(request, base_generation=1))
