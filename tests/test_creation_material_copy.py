import hashlib
import os

from fastapi.testclient import TestClient
import pytest

from meta_research.protected_creation_runtime import NativeCreationCall
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import MCP_PROTOCOL_VERSION
from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS
from meta_research.web import create_app
from test_creation_material_consumption import _call, _open, _value
from test_root_workspace import _make


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0, reason="requires actual chroot boundary")


def _native(work, script):
    return work.run(NativeCreationCall(("/bin/bash", "-c", script), "", 30, {}, (), ()))


def test_requested_copy_is_independent_bounded_and_replay_preserves_edits(tmp_path):
    source = tmp_path / "original"
    source.mkdir()
    content = b"print(4 * 4)\n"
    (source / "trial.py").write_bytes(content)
    with (source / "large.bin").open("wb") as stream:
        stream.truncate(4 * 1024**3)
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "copy-open")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            gateway = runtime.harnesses._gateway
            work = roots.protected_creation(binding, human.creation_material_snapshot(anchor), operation_ref="native-copy")
            with pytest.raises(OwnerConflict, match="protected_creation_unknown_outcome"):
                work.seal()
            assert _native(work, "echo ready").returncode == 0
            access = work.channel()
            mcp_headers = {"Authorization": "Bearer " + access.token, "Mcp-Protocol-Version": MCP_PROTOCOL_VERSION,
                           "Accept": "application/json, text/event-stream"}
            response = client.post("/mcp", headers=mcp_headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert response.status_code == 200, response.text
            assert {tool["name"] for tool in response.json()["result"]["tools"]} == set(CREATION_MATERIAL_OPERATION_IDS)
            class Connection:
                token = access.token
            connection = Connection()
            listing = _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=reference["reference_ref"]))
            trial = next(item for item in listing["entries"] if item["name"] == "trial.py")
            args = {"effect_id": "copy-trial", "reference_ref": reference["reference_ref"], "path": "trial.py",
                    "observation_ref": trial["observation"]["observation_ref"], "max_bytes": 100}
            receipt = _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **args))
            assert receipt["sha256"] == hashlib.sha256(content).hexdigest()
            copied = work.work_directory / receipt["work_file"]["path"]
            assert copied.stat().st_ino != (source / "trial.py").stat().st_ino
            assert copied.stat().st_nlink == 1
            assert _native(work, f"echo 'print(5 * 5)' > {receipt['working_path']}; python {receipt['working_path']}").stdout.strip() == "25"
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **args)) == receipt
            assert copied.read_bytes() == b"print(5 * 5)\n"
            assert _value(_call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[3], effect_id="copy-trial")) == receipt
            large = next(item for item in listing["entries"] if item["name"] == "large.bin")
            denied = _call(gateway, connection, CREATION_MATERIAL_OPERATION_IDS[2], **{**args,
                "effect_id": "large", "path": "large.bin", "observation_ref": large["observation"]["observation_ref"]})
            assert denied["isError"]
            identity = work.seal()
            assert len(identity.consumed) == 1
            status, _ = gateway.dispatch(connection.token, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            assert status == 401
            assert client.post("/mcp", headers=mcp_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).status_code == 401
            assert (source / "trial.py").read_bytes() == content
            assert (source / "large.bin").stat().st_blocks == 0
    finally:
        runtime.close()


def test_native_symlink_cannot_redirect_trusted_copy_to_original(tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("protected")
    runtime = _make(tmp_path / "runtime")
    try:
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control")) as client:
            anchor, binding, reference = _open(client, runtime, source, "symlink-open")
            human, roots = runtime.owners.human_collaboration, runtime.root_workspaces
            roots.configure_creation_runtime(executable="/bin/bash", credentials_home=tmp_path / "credentials")
            gateway = runtime.harnesses._gateway
            work = roots.protected_creation(binding, human.creation_material_snapshot(anchor), operation_ref="symlink-copy")
            assert _native(work, f"ln -s {tmp_path} /workspace/operations").returncode == 0
            access = work.channel()
            class Connection:
                token = access.token
            listing = _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[0], reference_ref=reference["reference_ref"]))
            copied = _call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[2], effect_id="unsafe", reference_ref=reference["reference_ref"],
                path="", observation_ref=listing["entries"][0]["observation"]["observation_ref"])
            assert copied["isError"]
            assert "creation_work_unsafe" in str(copied)
            assert _value(_call(gateway, Connection(), CREATION_MATERIAL_OPERATION_IDS[3], effect_id="unsafe"))["state"] == "failed"
            assert source.read_text() == "protected"
            work.fail()
    finally:
        runtime.close()
