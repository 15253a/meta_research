"""Distinct Recorder/creation interfaces; these are not real-model probes."""
import json
import os

from fastapi.testclient import TestClient
import pytest

from meta_research.owners.common import OwnerConflict
from meta_research.protected_creation_runtime import NativeCreationCall
from meta_research.recorder_sources import READ_SOURCE
from meta_research.semantic_mcp import TOOL_DISCOVERY_OPERATION_IDS
from meta_research.web import create_app
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from test_creation_material_consumption import _call, _open, _value
from test_public_plan_stage import _confirm_direct_quest, _owner_revisions
from test_root_workspace import _make


def test_recorder_owner_diagnostic_reaches_both_consumers_and_same_frozen_job_continues(tmp_path, monkeypatch):
    runtime = _make(tmp_path / "runtime")
    try:
        quest = _confirm_direct_quest(runtime)
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:18770")
        channels = runtime.recorder_channels
        reader = channels.reader
        nodes = reader.read(quest["quest_ref"])
        node = next(item for item in nodes if item["kind"] == "question")
        source_ref = node["sources"][0]["ref"]
        basis = {"quest_ref": quest["quest_ref"], "nodes": [node]}
        expected = reader.read_source(quest["quest_ref"], node, source_ref)
        original_read = reader.read_source
        before = _owner_revisions(runtime)

        def unavailable(quest_ref, frozen_node, ref):
            assert (quest_ref, frozen_node, ref) == (quest["quest_ref"], node, source_ref)
            # Only the transient Owner failure is injected. The later read uses
            # the real accepted source and unchanged frozen binding.
            raise OwnerConflict("content_temporarily_unavailable", {
                "source_ref": ref, "source_hash": frozen_node["source_hash"],
                "message": "The retained source is temporarily unavailable.",
                "correction": "Restore this source read path; keep the original job and exact source binding.",
            })

        with channels.grant("recorder-diagnostic-job", basis, "original-recorder-native") as connection:
            monkeypatch.setattr(reader, "read_source", unavailable)
            arguments = {"node_key": node["node_key"], "source_ref": source_ref}
            rejected = _call(channels.gateway, connection, READ_SOURCE, **arguments)
            assert rejected["isError"]
            failure = rejected["structuredContent"]
            assert failure["code"] == "content_temporarily_unavailable"
            assert failure["details"]["source_ref"] == source_ref
            assert failure["details"]["source_hash"] == node["source_hash"]
            assert "original job" in failure["details"]["correction"]
            assert json.loads(rejected["content"][0]["text"]) == failure

            monkeypatch.setattr(reader, "read_source", original_read)
            page = _value(_call(channels.gateway, connection, READ_SOURCE, **arguments))
            serialized = page["text"]
            while page["next_offset"] is not None:
                page = _value(_call(channels.gateway, connection, READ_SOURCE,
                    **arguments, offset=page["next_offset"]))
                serialized += page["text"]
            assert json.loads(serialized) == expected
            assert page["source_hash"] == node["source_hash"]
            assert channels.is_current(connection.token)
            assert _owner_revisions(runtime) == before
        status, _ = channels.gateway.dispatch(connection.token,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert status == 401
    finally:
        runtime.close()


@pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
                    reason="requires the actual protected Linux creation runtime")
def test_creation_grant_corrects_copy_limit_and_reconciles_without_overwriting_work(tmp_path):
    source = tmp_path / "original.txt"
    original = b"Original research observation: 16.\n"
    source.write_bytes(original)
    runtime = _make(tmp_path / "runtime")
    work = None
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "diagnostic-creation")
            roots = runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            work = roots.protected_creation(binding,
                runtime.owners.human_collaboration.creation_material_snapshot(anchor),
                operation_ref="creation-diagnostic-operation")
            assert work.run(NativeCreationCall(("/bin/bash", "-c", "echo ready"), "", 30, {}, (), ())).returncode == 0
            connection = work.channel()
            gateway = runtime.harnesses._gateway
            status, catalog = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert status == 200
            assert {tool["name"] for tool in catalog["result"]["tools"]} == {
                *TOOL_DISCOVERY_OPERATION_IDS, *CREATION_MATERIAL_OPERATION_IDS}
            entry = _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[0],
                reference_ref=reference["reference_ref"]))["entries"][0]
            arguments = {"effect_id": "original-copy", "reference_ref": reference["reference_ref"],
                         "path": "", "observation_ref": entry["observation"]["observation_ref"], "max_bytes": 0}
            rejected = _call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **arguments)
            failure = rejected["structuredContent"]
            assert rejected["isError"] and failure["code"] == "semantic_input_schema_mismatch"
            assert failure["details"]["path"] == ["max_bytes"]
            assert failure["details"]["expected"] == 1
            assert failure["details"]["call_execution"] == "not_started"
            assert json.loads(rejected["content"][0]["text"]) == failure
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[3],
                effect_id=arguments["effect_id"])) == {"state": "absent"}

            arguments["max_bytes"] = len(original)
            accepted = _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **arguments))
            copied = work.work_directory / accepted["work_file"]["path"]
            assert copied.read_bytes() == original
            assert copied.stat().st_ino != source.stat().st_ino
            copied.write_bytes(b"Retained analysis using the original observation.\n")
            too_small = {**arguments, "effect_id": "size-check", "max_bytes": 1}
            rejected_size = _call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **too_small)
            assert rejected_size["structuredContent"]["code"] == "creation_copy_too_large"
            size_details = rejected_size["structuredContent"]["details"]
            assert size_details["path"] == ["max_bytes"]
            assert size_details["required_bytes"] == len(original)
            assert "Reconcile" in size_details["correction"]
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[3],
                effect_id="size-check")) == {"state": "failed"}
            # Only after the original copy is confirmed failed is a new copy
            # identity used for a changed size limit.
            resized = _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2],
                **{**arguments, "effect_id": "size-corrected"}))
            assert (work.work_directory / resized["work_file"]["path"]).read_bytes() == original
            assert copied.read_bytes() == b"Retained analysis using the original observation.\n"
            # A changed payload under the existing identity is not a legal
            # correction once this effect succeeded; reconcile the original.
            conflict = _call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2],
                **{**arguments, "max_bytes": len(original) + 1})
            assert conflict["structuredContent"]["code"] == "creation_copy_identity_conflict"
            details = conflict["structuredContent"]["details"]
            assert details["effect_id"] == arguments["effect_id"]
            assert details["reconciliation_operation_id"] == CREATION_MATERIAL_OPERATION_IDS[3]
            assert json.loads(conflict["content"][0]["text"]) == conflict["structuredContent"]
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[3],
                effect_id=arguments["effect_id"])) == accepted
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **arguments)) == accepted
            assert copied.read_bytes() == b"Retained analysis using the original observation.\n"
            assert source.read_bytes() == original
            identity = work.seal()
            assert identity.consumed
            assert all(item.operation_ref == "creation-diagnostic-operation" for item in identity.consumed)
            status, _ = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert status == 401
    finally:
        if work is not None and not work._terminal:
            work.fail()
        runtime.close()
