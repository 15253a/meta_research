"""A real initialized DeepFetch grant can ask, receive and read its exact reply."""
from __future__ import annotations

import json
import copy
import subprocess
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.deepfetch_sources import SOURCE_LEDGER_SCHEMA
from meta_research.human_reply import ProvidedReply
from meta_research.paths import prepare_data_root
from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
from test_external_root_resident_mcp import _fake_codex
from test_public_first_question_deepfetch import (
    CommonAcquisitionEffectRunner, DeterministicProbe, RecordingAcquisitionProvider,
    PROTOTYPE_EMPTY_FINAL, PROTOTYPE_EMPTY_LEDGER, _authenticate, _open_and_queue_deepfetch,
)
from meta_research.owners.common import OwnerConflict


class _HumanRequestRunner(CommonAcquisitionEffectRunner):
    runtime = None
    opened = None
    readback = None

    def __call__(self, argv, prompt, timeout, environment=None):
        if "web_evidence_gate=v1" in prompt:
            return super().__call__(argv, prompt, timeout, environment)
        token = environment["META_RESEARCH_MCP_TOKEN"]

        def call(name, arguments):
            status, envelope = self.runtime.harnesses.dispatch_mcp(token, {
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
            assert status == 200
            result = envelope["result"]
            assert not result.get("isError"), result
            return result["structuredContent"]

        self.opened = call("human_request.open", {
            "effect_id": "initialization-tool-repair", "request_kind": "system_operation_help",
            "obligation": "Restore this initialized research root's local tool.",
            "business_purpose": "Continue the exact current literature preparation.",
            "condition": {"impact": "This root cannot read its supplied material.",
                          "safe_response": "Confirm the repair without sharing credentials."},
            "acceptance_conditions": ["The operator requests one retry after repair."],
        })
        assert self.opened["operation_binding"]["quest_ref"] is None
        assert self.opened["receipt"]["status"] == "accepted"
        assert self.opened["task_yield"]["status"] == "yielded"
        assert call("human_request.open.reconcile", {"effect_id": "initialization-tool-repair"}) == self.opened
        binding = self.opened["operation_binding"]
        original = dict(root_kind="deepfetch", run_ref=binding["task_ref"],
                        attempt_ref=binding["attempt_ref"], root_session_ref=binding["root_session_ref"],
                        fence_ref=binding["fence_ref"], runtime_binding_hash=binding["runtime_binding_hash"])
        owner = self.runtime.owners.agent_runtime
        for wrong in ({"root_session_ref": "foreign-session"}, {"fence_ref": "stale-fence"},
                      {"run_ref": "fabricated-initialization-run"}):
            with pytest.raises(OwnerConflict):
                owner.verify_root_agent_human_request_reconcile_scope(**{**original, **wrong})
        request = owner.query_human_request(self.opened["request_ref"])
        forged = {**binding, "task_ref": "fabricated-initialization-run"}
        target = copy.deepcopy(request["target_assertion"])
        target["root"]["run_ref"] = forged["task_ref"]
        with pytest.raises(OwnerConflict):
            owner.open_human_request_effect(effect_key="mcp-effect:forged-initialization",
                effect_id="forged-initialization", operation_binding=forged,
                predecessor_request_ref=None, request_kind="system_operation_help",
                obligation="Forged root must not persist a request.", business_purpose="Negative authority test.",
                target_assertion=target, acceptance_conditions=("Never accepted.",),
                direct_waiter={"waiter_ref": "root_run:" + forged["task_ref"], "generation": binding["generation"],
                               "target_assertion": target, "wait_scope": "local", "other_blockers": []}, quest_ref=None)
        assert len(owner.query_human_requests()) == 1
        human = self.runtime.owners.human_collaboration
        self.response = human.respond_to_human_request(
            self.opened["request_ref"], reply=ProvidedReply(
                "The local reader was repaired; retry this original work.", {"action": "retry"}),
            idempotency_key="initialization-tool-reply")
        resolved = call("human_request.open.reconcile", {"effect_id": "initialization-tool-repair"})
        assert resolved["resolution"]["response_ref"] == self.response["response_ref"]
        self.readback = json.loads(call("human_request.read", {
            "request_ref": self.opened["request_ref"], "response_ref": self.response["response_ref"],
            "limit": 16384,
        })["text"])
        self.reply_bytes = call("research_workspace.read", self.readback["response"]["delivery"]["reply_reader"])

        public_root = Path(next(line.removeprefix("public_output_root=") for line in prompt.splitlines()
                                if line.startswith("public_output_root=")))
        (public_root / "fulltext").mkdir(parents=True, exist_ok=True)
        (public_root / "papers.json").write_text(json.dumps({
            **PROTOTYPE_EMPTY_LEDGER, "schema_version": SOURCE_LEDGER_SCHEMA,
        }), encoding="utf-8")
        (public_root / "summary.md").write_text("No paper was read in this bounded lifecycle test.\n", encoding="utf-8")
        Path(argv[argv.index("--output-last-message") + 1]).write_text(json.dumps(PROTOTYPE_EMPTY_FINAL), encoding="utf-8")
        self.calls.append((argv, prompt, timeout, environment))
        events = [{"type": "thread.started", "thread_id": "native-common-effect"}]
        for action, query in (("search", "bounded lifecycle fixture"), ("other", "https://example.org/fixture")):
            events.append({"type": "item.completed", "item": {"id": "lifecycle-" + action,
                "type": "web_search", "query": query, "action": {"type": action}}})
        return subprocess.CompletedProcess(argv, 0, stdout="\n".join(json.dumps(event) for event in events), stderr="")


def test_initialization_deepfetch_formal_human_reply_returns_to_original_root(tmp_path):
    runner = _HumanRequestRunner()
    provider = CodexDeepFetchAdapter(tmp_path / "provider", model_ref="gpt-test",
                                    executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"),
        deepfetch_provider=provider, acquisition_provider=RecordingAcquisitionProvider(),
        host_compute_probe=DeterministicProbe(), power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_harness_diagnostics=False, startup_power_probe=False)
    runner.runtime = runtime
    client, headers = _authenticate(runtime)
    try:
        initialization_id, _ = _open_and_queue_deepfetch(client, headers, key_prefix="initialization-hr")
        runtime.deepfetch.process_once()
        assert runner.opened is not None and runner.readback is not None
        assert runner.readback["response"]["response_ref"] == runner.response["response_ref"]
        assert runner.readback["response"]["delivery"] == runner.response["delivery"]
        assert "The local reader was repaired" in runner.reply_bytes["text"]
        assert runner.readback["response"]["delivery"]["root_session_ref"] == runner.opened["operation_binding"]["root_session_ref"]
        current = runtime.owners.agent_runtime.query_human_request(runner.opened["request_ref"])
        assert current["status"] == "satisfied"
        assert current["direct_waiters"][0]["status"] == "consumed"
        assert len(runtime.owners.agent_runtime.query_human_requests()) == 1
        final = runtime.owners.human_collaboration.query_quest_creation(initialization_id)
        assert final["creation_context"] == "quest_initialization"
        assert final["deepfetch"]["status"] == "succeeded", json.dumps(final["deepfetch"])
    finally:
        client.close()
        runtime.close()


def test_initialization_acquisition_wait_replay_does_not_advance_or_repeat_effect(tmp_path):
    from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper
    from meta_research.acquisition_root import CodexAcquisitionRootAdapter
    from test_external_root_resident_mcp import _AcquisitionDelegate, _SequenceRunner

    ready = {"status": "ready", "browser_context_ref": None, "reason_code": None,
             "evidence": {"configuration_health": "ready", "browser_control": "not_required",
                          "authorized_resource": "provided_only"}}

    class Runner(_SequenceRunner):
        runtime = None
        opened = None
        artifact = None

        def __call__(self, argv, prompt, timeout, environment=None):
            if len(self.prompts) == 1:
                task = json.loads(next(line.removeprefix("request=") for line in prompt.splitlines()
                                       if line.startswith("request=")))
                self.artifact = Path(task["target_dir"]) / "already-written.html"
                self.artifact.write_text("<html><article>Exact provided article body.</article></html>", encoding="utf-8")
                _, envelope = self.runtime.harnesses.dispatch_mcp(environment["META_RESEARCH_MCP_TOKEN"], {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "human_request.open", "arguments": {
                            "effect_id": "provided-local-repair", "request_kind": "system_operation_help",
                            "obligation": "Repair verification of this original local file.",
                            "business_purpose": "Resume the exact acquisition without downloading again.",
                            "condition": {"impact": "Verification is paused.", "safe_response": "Request retry after repair."},
                            "acceptance_conditions": ["The operator requests retry after repair."],
                        }}})
                assert not envelope["result"].get("isError"), envelope
                self.opened = envelope["result"]["structuredContent"]
                self._outputs = iter([{"results": [{"paper_id": "local-paper", "status": "waiting_user",
                    "path": None, "format": None, "failure": {"code": "local_verifier_needs_repair", "detail": "Waiting for operator repair."}}]}])
            elif len(self.prompts) == 2:
                assert "resume" in argv
                assert self.artifact.is_file()
                self._outputs = iter([{"results": [{"paper_id": "local-paper", "status": "obtained",
                    "path": str(self.artifact), "format": "html", "failure": None}]}])
            return super().__call__(argv, prompt, timeout, environment)

    runner = Runner([ready])
    provider = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), acquisition_provider=provider,
        host_compute_probe=DeterministicProbe(), power_inhibitor=OperatorAttestedPowerInhibitor(),
        startup_harness_diagnostics=False, startup_power_probe=False)
    runner.runtime = runtime
    try:
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:18768")
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "acquisition-wait-open")
        owner = runtime.owners.agent_runtime
        session = owner.prepare_acquisition_session(initialization_id=opened["initialization_id"],
            draft_revision=opened["quest_draft"]["revision"],
            config={"mode": "provided_only", "library_entry_url": ""}, provider=provider)
        request = AcquisitionBatchRequest("local-paper-effect", "oa_first_then_institution",
            (AcquisitionPaper("local-paper", "Exact provided article", None, None, ()),))
        first = owner.acquire_literature(session.session_ref, request, provider)
        assert first.status == "waiting_user"
        prior_control = owner.query_managed_run(request.request_id)
        assert prior_control["status"] == "suspended"
        saved = (runner.artifact.read_bytes(), runner.artifact.stat().st_mtime_ns)
        prior_native = provider._latest_native_session_ref(session.session_ref)
        repeated = owner.acquire_literature(session.session_ref, request, provider)
        assert repeated == first
        assert owner.query_managed_run(request.request_id) == prior_control
        assert len(runner.prompts) == 2
        assert provider._latest_native_session_ref(session.session_ref) == prior_native
        assert (runner.artifact.read_bytes(), runner.artifact.stat().st_mtime_ns) == saved
        human.respond_to_human_request(runner.opened["request_ref"],
            reply=ProvidedReply("Verification repaired; continue the existing file.", {"action": "retry"}),
            idempotency_key="acquisition-wait-response")
        complete = owner.acquire_literature(session.session_ref, request, provider)
        assert complete.status == "obtained"
        assert len(runner.prompts) == 3
        assert provider._latest_native_session_ref(session.session_ref) == prior_native
        assert (runner.artifact.read_bytes(), runner.artifact.stat().st_mtime_ns) == saved
        assert owner.query_managed_run(request.request_id)["status"] == "completed"
        assert owner.acquire_literature(session.session_ref, request, provider) == complete
        assert len(runner.prompts) == 3
    finally:
        runtime.close()
