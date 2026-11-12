"""External snapshots must not preempt sealed Bundle operation reconciliation."""

import json

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.external_mcp import ExternalMcpError, ExternalMcpRuntime
from meta_research.idea_skill import IdeaSkillUnavailable
from test_bundle_input_drift_recovery import _adapter, _changed, _output, _request


@pytest.fixture(params=["dispatch", "batch"])
def sealed_external_bundle(tmp_path, request):
    kind = request.param
    external = ExternalMcpRuntime(tmp_path / "settings")
    external.configure_endpoint("http://127.0.0.1:18768")
    adapter, runner = _adapter(tmp_path, _output(kind), durable=True)
    adapter.bind_external_mcp(external)
    original_request = _request(adapter, kind)
    invoke = adapter.schedule_target if kind == "dispatch" else adapter.propose_target_batch
    result = invoke(original_request)
    operation = next((tmp_path / "provider/provider-operations").glob("*/*"))
    snapshot, = (tmp_path / "settings/external-mcp/operations").glob("*.json")
    yield kind, external, runner, invoke, original_request, result, operation, snapshot
    external.close()


def test_sealed_bundle_replay_uses_original_snapshot_despite_rendered_prompt_change(
    sealed_external_bundle, monkeypatch,
):
    kind, external, runner, invoke, request, first, operation, snapshot = sealed_external_bundle
    before = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    snapshot_bytes = snapshot.read_bytes()
    render = bundle_skill._rolling_position_prompt
    monkeypatch.setattr(bundle_skill, "_rolling_position_prompt",
                        lambda request: render(request) + "\nRefreshed display wording.\n")
    assert invoke(request) == first
    assert len(runner.calls) == 1
    assert {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()} == before
    assert snapshot.read_bytes() == snapshot_bytes
    assert not external._grants


def test_external_snapshot_restore_does_not_accept_changed_business_input(sealed_external_bundle):
    kind, external, runner, invoke, request, _first, operation, snapshot = sealed_external_bundle
    before = {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()}
    snapshot_bytes = snapshot.read_bytes()
    with pytest.raises(IdeaSkillUnavailable) as failure:
        invoke(_changed(request, kind))
    assert failure.value.code == "bundle_review_result_contract_invalid"
    assert failure.value.recovery_checkpoint["contract_failure_detail_code"] == "bundle_operation_inputs_changed"
    assert len(runner.calls) == 1
    assert {p.name: p.read_bytes() for p in operation.iterdir() if p.is_file()} == before
    assert snapshot.read_bytes() == snapshot_bytes
    assert not external._grants


@pytest.mark.parametrize("damage", ["pending", "invocation_seal", "schema"])
def test_snapshot_restore_does_not_authorize_unverified_provider_replay(
    sealed_external_bundle, damage,
):
    kind, external, runner, invoke, request, _first, operation, snapshot = sealed_external_bundle
    if damage == "pending":
        for name in ("exit.json", "completed.json", "supervisor-exit.json"):
            (operation / name).unlink()
        expected = "codex_operation_reconciliation_pending"
    elif damage == "invocation_seal":
        path = operation / "invocation.json"
        value = json.loads(path.read_text())
        value["seal"] = "f" * 64
        path.write_text(json.dumps(value))
        expected = "codex_operation_identity_conflict"
    else:
        path = operation / "output-schema.json"
        value = json.loads(path.read_text())
        value["description"] = "Altered after sealing"
        path.write_text(json.dumps(value))
        expected = "codex_operation_spool_invalid"
    snapshot_bytes = snapshot.read_bytes()
    with pytest.raises(IdeaSkillUnavailable) as failure:
        invoke(_changed(request, kind))
    assert failure.value.code == expected
    assert len(runner.calls) == 1
    assert snapshot.read_bytes() == snapshot_bytes
    assert not external._grants


def test_exact_snapshot_binding_is_required_to_ignore_new_rendered_prompt(tmp_path):
    external = ExternalMcpRuntime(tmp_path)
    original = external.operation_snapshot(operation_identity="original", root_kind="bundle", task_prompt="Original work")
    path = tmp_path / "external-mcp/operations" / (original.operation_key + ".json")
    before = path.read_bytes()
    read_config = external.read_config
    # No current configuration lookup or rediscovery is permitted on this path.
    def forbidden():
        raise AssertionError("Recovery must not rediscover or read current configuration")
    external.read_config = forbidden
    restored = external.operation_snapshot(operation_identity="original", root_kind="bundle",
        task_prompt="New display wording", binding=original.binding())
    assert restored == original and restored.task_prompt == "Original work"
    assert path.read_bytes() == before
    for arguments, code in (
        ({}, "external_mcp_snapshot_conflict"),
        ({"binding": {**original.binding(), "snapshot_hash": "f" * 64}}, "external_mcp_snapshot_conflict"),
        ({"binding": {**original.binding(), "operation_key": "f" * 64}}, "external_mcp_snapshot_conflict"),
        ({"binding": {**original.binding(), "extra": "invalid"}}, "external_mcp_snapshot_conflict"),
    ):
        with pytest.raises(ExternalMcpError, match=code):
            external.operation_snapshot(operation_identity="original", root_kind="bundle",
                task_prompt="New display wording", **arguments)
    for identity, kind in (("foreign", "bundle"), ("original", "idea")):
        with pytest.raises(ExternalMcpError, match="external_mcp_snapshot_missing"):
            external.operation_snapshot(operation_identity=identity, root_kind=kind,
                task_prompt="New display wording", binding=original.binding())
    assert path.read_bytes() == before
    reads = []
    def current_configuration():
        reads.append(True)
        return read_config()
    external.read_config = current_configuration
    fresh = external.operation_snapshot(operation_identity="next", root_kind="bundle",
        task_prompt="A new logical operation")
    assert reads == [True] and fresh.operation_key != original.operation_key
    assert fresh.task_prompt == "A new logical operation"
    assert path.read_bytes() == before
    damaged = json.loads(before)
    damaged["payload"]["task_prompt"] = "Unsigned replacement"
    path.write_text(json.dumps(damaged))
    with pytest.raises(ExternalMcpError, match="external_mcp_snapshot_invalid"):
        external.operation_snapshot(operation_identity="original", root_kind="bundle",
            task_prompt="New display wording", binding=original.binding())
    assert not external._grants
