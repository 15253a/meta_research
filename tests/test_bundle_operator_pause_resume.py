"""A formally stopped Bundle primary resumes its original native with a fresh call."""
import json
import threading
import time

import pytest

import meta_research.bundle_skill as bundle_skill
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.idea_skill import _compile_codex_output_schema
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from test_public_bundle_stage import (
    _bundle_runtime, _prepare_bundle_request, _DeterministicBundleSkill,
)
from test_reasoning_operator_pause_resume import _control, _provider
from test_bundle_skill_adapter import _provider_wire_value

NATIVE = "operator-paused-native-bundle"


class _SupervisedBundle(CodexBundleSkillAdapter):
    def __init__(self, root, executable):
        self.fixture_root = root
        self.requests = []
        super().__init__(prepare_data_root(root).root / "bundle-skill-provider",
                         executable=str(executable), timeout_seconds=120)

    def generate_draft(self, request):
        self.requests.append(request)
        draft = _DeterministicBundleSkill().generate_draft(request)
        schema = _compile_codex_output_schema(bundle_skill._target_plan_envelope_schema(request))
        wire = _provider_wire_value({"target_plan": draft.draft}, schema)
        (self.fixture_root / "next-output.json").write_text(json.dumps(wire["provider_output"]))
        return super().generate_draft(request)


def _runtime(root, executable):
    adapter = _SupervisedBundle(root, executable)
    runtime = _bundle_runtime(root, bundle_skill_provider=adapter)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    return runtime, adapter


@pytest.mark.parametrize("tamper", [False, True], ids=["same-native-new-call", "reject-tampered-stop"])
def test_hc_resume_stopped_bundle_primary_preserves_native_and_sealed_history(tmp_path, monkeypatch, tamper):
    root = tmp_path / "data"
    root.mkdir()
    executable = _provider(tmp_path / "codex", root, (NATIVE,), stopped_zero=True)
    runtime, _ = _runtime(root, executable)
    _prepare_bundle_request(runtime)
    assert runtime.bundle_stage.process_once()
    request_ref = runtime.bundle_stage.query_current()["stage_run_request"]["request_ref"]
    before = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
    assert before is not None and before.primary_draft is None
    failures = []
    def call():
        try: runtime.bundle_stage.process_once()
        except BaseException as error: failures.append(error)
    worker = threading.Thread(target=call)
    worker.start()
    calls = root / "provider-calls.jsonl"
    deadline = time.monotonic() + 15
    while not calls.exists():
        assert time.monotonic() < deadline, (failures, runtime.bundle_stage.transient_error)
        time.sleep(.01)
    _control(runtime, before, "pause")
    worker.join(15)
    assert not worker.is_alive() and not failures
    spool = next((root / "bundle-skill-provider").glob("provider-operations/*/primary"))
    assert json.loads((spool / "supervisor-exit.json").read_text())["payload"]["termination_reason"] == "stopped"
    original = {p.name: p.read_bytes() for p in spool.iterdir() if p.is_file()}
    runtime.close()
    if tamper:
        receipt = json.loads((spool / "supervisor-exit.json").read_text())
        receipt["seal"] = "0" * 64
        (spool / "supervisor-exit.json").write_text(json.dumps(receipt))
    resources = bundle_skill._bundle_skill_resources()
    resources["SKILL.md"] += "\nFresh pagination guidance for this resumed call.\n"
    monkeypatch.setattr(bundle_skill, "_bundle_skill_resources", lambda: dict(resources))
    restarted, adapter = _runtime(root, executable)
    try:
        assert not restarted.bundle_stage.process_once()
        if tamper:
            with pytest.raises(OwnerConflict, match="operator_stop_checkpoint_invalid"):
                _control(restarted, before, "resume")
            assert restarted.owners.agent_runtime.query_managed_run(before.run_ref)["status"] == "suspended"
            assert len(calls.read_text().splitlines()) == 1
            return
        assert _control(restarted, before, "resume")["executed"] is True
        resumed = restarted.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert resumed.run_ref == before.run_ref
        assert resumed.root_session_ref == before.root_session_ref
        assert resumed.runtime_binding_hash == before.runtime_binding_hash
        assert resumed.native_session_ref == NATIVE
        assert resumed.attempt_ref != before.attempt_ref
        assert resumed.fence_ref != before.fence_ref
        assert resumed.primary_invocation.operation_ref != before.primary_invocation.operation_ref
        assert restarted.bundle_stage.process_once()
        after = restarted.owners.agent_runtime.query_bundle_stage_run(request_ref)
        assert after.primary_draft is not None and after.native_session_ref == NATIVE, restarted.bundle_stage.transient_error
        assert len(adapter.requests) == 1 and adapter.requests[0].native_session_ref == NATIVE
        with pytest.raises(OwnerConflict, match="runtime_fence_revoked"):
            restarted.owners.agent_runtime.record_bundle_primary_draft(
                run_ref=before.run_ref, attempt_ref=before.attempt_ref, fence_ref=before.fence_ref,
                native_session_ref=NATIVE, runtime_binding=before.runtime_binding,
                draft=after.primary_draft.draft, adapter_kind="codex_cli",
                idempotency_key="stopped-old-bundle-cannot-return",
            )
        argv = json.loads(calls.read_text().splitlines()[-1])
        assert argv[-3:] == ["resume", NATIVE, "-"]
        assert len(calls.read_text().splitlines()) == 2
        newer = [p for p in (root / "bundle-skill-provider").glob("provider-operations/*/primary") if p != spool]
        assert len(newer) == 1
        assert "Fresh pagination guidance" in (newer[0] / "prompt.txt").read_text()
        assert original == {p.name: p.read_bytes() for p in spool.iterdir() if p.is_file()}
    finally:
        restarted.close()
