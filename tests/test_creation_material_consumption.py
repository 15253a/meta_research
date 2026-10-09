import hashlib

from fastapi.testclient import TestClient
import pytest

from meta_research.creation_inputs import CreationAnchor, require_identity
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpGateway
from meta_research.work_material_contract import WORK_MATERIAL_OPERATION_IDS
from meta_research.work_materials import material_operations
from meta_research.web import create_app
from test_public_human_reply_delivery import _login
from test_public_work_materials import _source
from test_root_workspace import _make


def _call(gateway, connection, operation, **arguments):
    status, response = gateway.dispatch(connection.token, {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": operation, "arguments": arguments}})
    assert status == 200
    return response["result"]


def _value(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


def _open(client, runtime, source, key):
    headers = {**_login(client, runtime), "Idempotency-Key": key}
    response = client.post("/api/v1/quest-initializations", json={}, headers=headers)
    assert response.status_code == 201, response.text
    view = response.json()
    human = runtime.owners.human_collaboration
    reference = client.post(f"/api/v1/quest-initializations/{view['initialization_id']}/material-references",
        json={"receiver": human.material_receiver("creation", view["initialization_id"]),
              "selections": [_source(client, source)], "description": "Read only the small note."},
        headers={**headers, "Idempotency-Key": key + "-material"})
    assert reference.status_code == 201, reference.text
    draft = view["quest_draft"]
    anchor = CreationAnchor("quest_initialization", view["initialization_id"], None,
                            draft["revision"], draft["hash"])
    binding = runtime.root_workspaces.bind_initialization(anchor.ref, view["intent_session"]["ref"])
    return anchor, binding, reference.json()["references"][0]


def _channel(gateway, operation, **overrides):
    scope = {"run_ref": operation.operation_ref, "attempt_ref": operation.operation_ref,
        "root_session_ref": operation.binding.location.root_session_ref,
        "fence_ref": operation.fence_ref, "capability_binding_hash": operation.binding_hash,
        "operation_ids": WORK_MATERIAL_OPERATION_IDS, "root_kind": "companion", "phase": "creation_materials"}
    connection, _ = gateway.issue_channel(**{**scope, **overrides})
    return connection


def test_current_operation_reads_have_exact_witnesses_and_exclude_browser_and_prior_jobs(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    (source / "note.txt").write_text("The observed value is 17. Unread tail.")
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(4 * 1024**3)
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "operation-open")
            human = runtime.owners.human_collaboration
            path = f"/api/v1/work-materials/{reference['reference_ref']}"
            page = client.get(path + "/discover").json()
            note = next(item for item in page["entries"] if item["name"] == "note.txt")
            args = {"reference_ref": reference["reference_ref"], "path": "note.txt",
                    "observation_ref": note["observation"]["observation_ref"], "offset": 0, "max_bytes": 25}
            browser = client.get(path + "/read", params={key: value for key, value in args.items() if key != "reference_ref"})
            assert browser.status_code == 200
            inputs = human.creation_material_snapshot(anchor)
            gateway = SemanticMcpGateway(material_operations(human))
            prior = human.begin_creation_material_operation(inputs, binding, "understanding-prior")
            prior_connection = _channel(gateway, prior)
            assert _value(_call(gateway, prior_connection, WORK_MATERIAL_OPERATION_IDS[1], **args))["text"] == "The observed value is 17."
            prior_identity = prior.seal()
            assert len(prior_identity.consumed) == 1
            operation = human.begin_creation_material_operation(inputs, binding, "understanding-current")
            connection = _channel(gateway, operation)
            assert operation.identity().consumed == ()
            listed = _value(_call(gateway, connection, WORK_MATERIAL_OPERATION_IDS[0]))
            assert listed["references"][0]["read_state"] == "not_read"
            assert listed["references"][0]["read_ranges"] == []
            catalog_status, catalog = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert catalog_status == 200
            assert {tool["name"] for tool in catalog["result"]["tools"]} == set(WORK_MATERIAL_OPERATION_IDS)
            consumed = _value(_call(gateway, connection, WORK_MATERIAL_OPERATION_IDS[1], **args))
            assert consumed["text"] == "The observed value is 17."
            witness = consumed["read_witness"]
            assert witness["operation_ref"] == "understanding-current"
            assert witness["length"] == 25
            assert witness["chunk_sha256"] == hashlib.sha256(b"The observed value is 17.").hexdigest()
            denied = _channel(gateway, operation, fence_ref=prior.fence_ref)
            assert _call(gateway, denied, WORK_MATERIAL_OPERATION_IDS[1], **args)["isError"]
            identity = operation.seal()
            assert [item.witness_ref for item in identity.consumed] == [witness["witness_ref"]]
            assert _call(gateway, connection, WORK_MATERIAL_OPERATION_IDS[1], **args)["isError"]
            assert require_identity(human, identity).set_hash == inputs.set_hash
            assert (source / "large.bin").stat().st_blocks == 0
            assert list(runtime.data_root.objects.rglob("*.bin")) == []
            (source / "note.txt").write_text("The observed value is 99. Unread tail.")
            with pytest.raises(OwnerConflict, match="creation_input_stale"):
                require_identity(human, identity)
    finally:
        runtime.close()


def test_append_during_active_operation_rejects_old_set_and_submission_replay_is_stable(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("First note.")
    other = tmp_path / "added.txt"
    other.write_text("Added without revising the draft.")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "append-open")
            human = runtime.owners.human_collaboration
            inputs = human.creation_material_snapshot(anchor)
            operation = human.begin_creation_material_operation(inputs, binding, "active-understanding")
            gateway = SemanticMcpGateway(material_operations(human))
            connection = _channel(gateway, operation)
            headers = {**_login(client, runtime), "Idempotency-Key": "material-append"}
            command = {"receiver": human.material_receiver("creation", anchor.ref),
                "selections": [_source(client, other)], "description": "New input."}
            path = f"/api/v1/quest-initializations/{anchor.ref}/material-references"
            appended = client.post(path, json=command, headers=headers)
            assert appended.status_code == 201
            changed = human.creation_material_snapshot(anchor)
            assert changed.anchor == inputs.anchor
            assert changed.set_hash != inputs.set_hash
            assert client.post(path, json=command, headers=headers).json() == appended.json()
            assert human.creation_material_snapshot(anchor).set_hash == changed.set_hash
            assert _call(gateway, connection, WORK_MATERIAL_OPERATION_IDS[0])["isError"]
            with pytest.raises(OwnerConflict, match="creation_input_stale"):
                operation.seal()
            operation.fail()
    finally:
        runtime.close()
