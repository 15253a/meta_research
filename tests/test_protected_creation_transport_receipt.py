"""Controlled output, real protected helper and signed adapter transport."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.creation_work import ProtectedCreationRunner
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.protected_creation_runtime import ProtectedCreationRuntime
from meta_research.provider_supervisor import read_transport_envelope, supervisor_host_identity


pytestmark = pytest.mark.skipif(os.name != "posix" or os.geteuid() != 0,
    reason="requires actual Linux protected helper")
SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}},
    "required": ["reply"], "additionalProperties": False}


@pytest.fixture(scope="module")
def protected_runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("creation-transport-runtime")
    credentials = root / "credentials"
    credentials.mkdir()
    runtime = ProtectedCreationRuntime(root / "jail", Path("/bin/bash"), credentials)
    yield runtime
    assert runtime.request_stop()["descendants_ended"] is True


def _invoke(adapter, runner, job="protected-output"):
    return adapter._invoke(operation_name="initialization-understanding", prompt="Controlled transport output.",
        schema=SCHEMA, native_session_ref=None, job_ref=job, invocation_runner=runner,
        provider_execution_binding={"work_ref": job})


def test_real_protected_helper_closure_reaches_signed_receipt_and_replays_without_launch(protected_runtime, tmp_path):
    calls = []

    def run(call):
        calls.append(call)
        result = call.argv[call.argv.index("--output-last-message") + 1]
        script = """import json,pathlib,sys
pathlib.Path(sys.argv[1]).write_text(json.dumps({'reply':'accepted protected output'}))
print(json.dumps({'type':'thread.started','thread_id':'controlled-native-parent'}))
"""
        return protected_runtime.run(replace(call,
            argv=("/bin/bash", "-c", 'exec /bin/python - "$@"', "creation", result), prompt=script))

    work = SimpleNamespace(operation=SimpleNamespace(operation_ref="protected-output"),
        run=run, request_stop=protected_runtime.request_stop)
    runner = ProtectedCreationRunner(work)
    adapter = CodexCompanionAdapter(tmp_path / "adapter", process_runner=SimpleNamespace())
    result = _invoke(adapter, runner)
    assert result[:2] == ({"reply": "accepted protected output"}, "controlled-native-parent")
    directory = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": "protected-output"}) / "initialization-understanding"
    _, key = adapter._transport_key()
    receipt = read_transport_envelope(directory / "supervisor-exit.json", key)
    assert receipt["process_tree"] == {"schema_ref": "meta-research/provider-process-tree/v1",
        "state": "closed", "mechanism": "linux-subreaper-echild", **supervisor_host_identity()}
    assert (directory / "completed.json").is_file()
    assert _invoke(adapter, runner) == result
    assert len(calls) == 1


@pytest.mark.parametrize("proof", [None, {}, {"state": "closed"}])
def test_local_transport_rejects_missing_or_malformed_process_proof(tmp_path, proof):
    def run(call):
        call.output_paths[0].write_text('{"reply":"must remain unsealed"}')
        stdout = '{"type":"thread.started","thread_id":"controlled-native-parent"}\n'
        call.stdout_sink(stdout.encode())
        result = subprocess.CompletedProcess(call.argv, 0, stdout, "")
        if proof is not None:
            result.process_tree = proof
        return result

    runner = ProtectedCreationRunner(SimpleNamespace(operation=SimpleNamespace(operation_ref="protected-output"),
        run=run, request_stop=lambda: {"status": "completed", "descendants_ended": True}))
    adapter = CodexCompanionAdapter(tmp_path / "adapter", process_runner=SimpleNamespace())
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
        _invoke(adapter, runner)
    assert not list(tmp_path.rglob("completed.json"))
    assert not list(tmp_path.rglob("supervisor-exit.json"))
