from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.composition import build_production_runtime
from meta_research.codex_runtime import CODEX_LOCKED_VERSION
from meta_research.harness_adapters import ClaudeHarnessAdapter, HarnessAdapterUnavailable, HarnessSupervisorTransport
from meta_research.idea_skill import CodexIdeaSkillAdapter, IdeaSkillUnavailable
from meta_research.paths import prepare_data_root
from meta_research.provider_supervisor import read_supervisor_request
from meta_research.semantic_mcp import SemanticCallContext
from meta_research.quest_drafting import _CancellableProcessRunner
from test_harness_adapters import _invocation
from test_public_plan_stage import _DeterministicDraftingAdapter, _DeterministicProbe
from test_root_workspace import (
    _make, _context, _external_context, _scope, _channel, _call, _accepted,
    _Writer, _confirm_direct_quest, _finish_idea_stage,
)


def _native_executable(path):
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        f"    print('2.1.220' if 'claude' in Path(sys.argv[0]).name else 'codex-cli {CODEX_LOCKED_VERSION}')\n"
        "    raise SystemExit(0)\n"
        "if sys.argv[-2:] == ['features', 'list']:\n"
        "    print('hooks stable true\\nmulti_agent stable true\\nplugins stable true\\nremote_plugin stable true\\nshell_tool stable true\\nskill_search stable true\\nunified_exec stable true')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.read()\n"
        "marker = Path('native-marker.txt')\n"
        "previous = marker.read_text() if marker.exists() else ''\n"
        "marker.write_text(previous + os.getcwd() + '\\n')\n"
        "if 'phase=preflight' in prompt:\n"
        "    Path('preflight-nonce.txt').write_text('Acquisition preflight pending nonce')\n"
        "if previous and Path('preflight-nonce.txt').exists():\n"
        "    Path('native-observed-preflight.txt').write_text(Path('preflight-nonce.txt').read_text())\n"
        "human = [p.read_text() for p in Path('inbox').rglob('human.txt')] if Path('inbox').exists() else []\n"
        "if human:\n"
        "    Path('native-observed-human.txt').write_text('\\n'.join(human))\n"
        "value = {'cwd': os.getcwd(), 'previous': bool(previous), 'human': human, 'reply': 'Checked pending observations.', 'accepted': True, 'human_request': None}\n"
        "if '--output-last-message' in sys.argv:\n"
        "    schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())\n"
        "    value = {key: value[key] for key in schema['properties'] if key in value}\n"
        "    Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(value))\n"
        "    print(json.dumps({'type': 'thread.started', 'thread_id': 'local-native-root'}))\n"
        "else:\n"
        "    print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'local-claude-root'}))\n"
        "    print(json.dumps({'type': 'result', 'subtype': 'success', 'session_id': 'local-claude-root', 'is_error': False, 'result': json.dumps(value)}))\n",
        encoding="utf-8")
    path.chmod(0o700)
    return path


_SCHEMA = {"type": "object", "properties": {"cwd": {"type": "string"},
    "previous": {"type": "boolean"}, "human": {"type": "array", "items": {"type": "string"}},
    "reply": {"type": "string"}}, "required": ["cwd", "previous", "human", "reply"],
    "additionalProperties": False}


@pytest.mark.parametrize("durable", [False, True])
def test_real_idea_dispatch_native_initial_resume_and_public_plan_read(tmp_path, durable):
    adapter = CodexIdeaSkillAdapter(tmp_path / "transport",
        executable=str(_native_executable(tmp_path / "local-codex")))

    class Writer(_Writer):
        def generate_draft(self, request):
            binding = self.runtime.root_workspaces.bind_runtime(_context(request, "idea"))
            for index, native in enumerate((None, "local-native-root")):
                value, observed, _ = adapter._invoke(operation_name="primary", prompt="Check native cwd.",
                    schema=_SCHEMA, native_session_ref=native,
                    job_ref=f"idea-native-{index}" if durable else None, workspace_binding=binding)
                assert value == {"cwd": str(binding.directory), "previous": index == 1,
                    "human": [], "reply": "Checked pending observations."}
                assert observed == "local-native-root"
            if durable:
                replay = adapter._invoke(operation_name="primary", prompt="Check native cwd.",
                    schema=_SCHEMA, native_session_ref="local-native-root",
                    job_ref="idea-native-1", workspace_binding=binding)
                assert replay[0]["previous"] is True
                foreign_context = _external_context(_scope(self.runtime,
                    run_ref="foreign-native-work", root_ref="foreign-native-root"))
                foreign = self.runtime.root_workspaces.bind_runtime(foreign_context)
                with pytest.raises(IdeaSkillUnavailable, match="codex_operation_spool_invalid"):
                    adapter._invoke(operation_name="primary", prompt="Check native cwd.",
                        schema=_SCHEMA, native_session_ref="local-native-root",
                        job_ref="idea-native-1", workspace_binding=foreign)
                assert not (foreign.directory / "native-marker.txt").exists()
            return super().generate_draft(request)

    writer = Writer()
    runtime = _make(tmp_path / "runtime", idea=writer)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        assert (writer.location.directory / "native-marker.txt").read_text().splitlines() == [
            str(writer.location.directory), str(writer.location.directory)]
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
        from test_root_workspace import _Reader
        reader = _Reader(no_gap=False)
        runtime.close()
        runtime = _make(tmp_path / "runtime", plan=reader)
        for _ in range(6):
            assert runtime.plan_stage.process_once()
            if hasattr(reader, "context"):
                break
        channel = _channel(runtime, reader.context)
        page = _accepted(_call(runtime, channel, "research_workspace.discover"))
        native, = [item for item in page["files"] if item["path"] == "native-marker.txt"]
        assert native["work_ref"] == writer.location.work_ref
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=native["workspace_ref"], path=native["path"]))["text"] == (
                str(writer.location.directory) + "\n") * 2
    finally:
        runtime.close()


def test_shared_companion_adapter_interleaves_roots_and_rotates_human_request_job(tmp_path):
    from test_root_human_request_lifecycle import _open_arguments
    runtime = _make(tmp_path / "runtime")
    adapter = CodexCompanionAdapter(tmp_path / "companion-transport",
        executable=str(_native_executable(tmp_path / "local-codex")))
    adapter.bind_workspaces(runtime.root_workspaces)
    adapter.bind_resident_mcp_authority(runtime.harnesses)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    original_cwd = Path.cwd()
    try:
        a = _external_context(_scope(runtime, run_ref="native-a", root_ref="native-root-a"))
        b = _external_context(_scope(runtime, run_ref="native-b", root_ref="native-root-b"))
        locations = [runtime.root_workspaces.bind_runtime(context).location for context in (a, b)]

        def invoke(context, job, native=None):
            return adapter._invoke_root_operation(operation_name="companion-turn", prompt="Inspect observations.",
                schema=_SCHEMA, native_session_ref=native, job_ref=job,
                run_ref=context.run_ref, attempt_ref=context.attempt_ref,
                root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
                runtime_binding_hash=context.capability_binding_hash)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda pair: invoke(*pair), ((a, "native-job-a"), (b, "native-job-b"))))
        assert [value["cwd"] for value in values] == [str(item.directory) for item in locations]
        assert [value["previous"] for value in values] == [False, False]
        opened = _accepted(_call(runtime, _channel(runtime, a), "human_request.open",
            **_open_arguments("native-human-material", "offline_action")))
        destination = runtime.root_workspaces.destination_for_human_request(
            request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="native-human-answer",
            files=(("human.txt", b"measurement requires a 15 minute settling time"),))
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
            decision="provided", facts={"material_delivered": True}, note="Material ready.",
            idempotency_key="native-human-answer")
        resumed = invoke(a, "native-job-a-after-human", "local-native-root")
        assert resumed["cwd"] == str(locations[0].directory)
        assert resumed["previous"] is True
        assert resumed["human"] == ["measurement requires a 15 minute settling time"]
        assert _accepted(_call(runtime, _channel(runtime, a), "research_workspace.read",
            workspace_ref=locations[0].workspace_ref, path=delivered["files"][0]["path"]))["text"] == resumed["human"][0]
        assert Path.cwd() == original_cwd
        assert not (locations[1].directory / "inbox").exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("durable", [False, True])
def test_actual_claude_child_cwd_initial_resume_and_signed_replay(tmp_path, durable):
    from test_target_root_finalizer import _root_finalizer_fixture
    runtime, _lifecycle, _memory, _authority, handle, workspace, _evidence = _root_finalizer_fixture(tmp_path)
    transport = HarnessSupervisorTransport(tmp_path / "claude-supervisor") if durable else _CancellableProcessRunner()
    adapter = ClaudeHarnessAdapter(tmp_path / "claude-adapter", runner=transport)
    adapter.executable = str(_native_executable(tmp_path / "local-claude"))
    try:
        lease = runtime.target_run_authorities.agent_runtime.query_target_workspace(handle.target_run_ref)
        invocation = replace(_invocation("claude"), run_ref=handle.target_run_ref,
            root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
            fence_ref=handle.execution_fence_ref, target_workspace_ref=lease.workspace_ref,
            working_directory=str(workspace))
        first = adapter.invoke(invocation)
        second_invocation = replace(invocation, provider_operation_ref="provider-operation:claude-resume",
            native_session_ref=first.native_session_ref)
        second = adapter.invoke(second_invocation)
        assert first.native_session_ref == second.native_session_ref == "local-claude-root"
        assert (workspace / "native-marker.txt").read_text().splitlines() == [str(workspace)] * 2
        if durable:
            replay = adapter.invoke(second_invocation)
            assert replay.native_session_ref == "local-claude-root"
            assert (workspace / "native-marker.txt").read_text().splitlines() == [str(workspace)] * 2
            other = tmp_path / "foreign-directory"
            other.mkdir()
            with pytest.raises(HarnessAdapterUnavailable):
                adapter.invoke(replace(second_invocation, working_directory=str(other)))
            assert not (other / "native-marker.txt").exists()
            requests = list((tmp_path / "claude-supervisor").glob("provider-operations/*/*/supervisor-request.json"))
            assert len(requests) == 2
            for request in requests:
                assert read_supervisor_request(request, transport._transport_key)["working_directory"] == str(workspace)
        assert runtime.target_run_authorities.agent_runtime.query_target_workspace(handle.target_run_ref) == lease
        owner_run = runtime.owners.agent_runtime.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        runtime.owners.agent_runtime.harness_runs.start_operation(run_ref=owner_run.run_ref,
            operation_ref=second_invocation.provider_operation_ref, generation=1,
            invocation_hash="a" * 64, resume=False)
        context = SemanticCallContext(owner_run.run_ref, owner_run.attempt_ref, owner_run.root_session_ref,
            owner_run.fence_ref, owner_run.capability_binding_hash, "target", "target_root", "research_workspace.read")
        assert runtime.root_workspaces.bind_runtime(context).directory == workspace
        channel = _channel(runtime, context)
        content = _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=lease.workspace_ref, path="native-marker.txt"))
        assert content["text"] == (str(workspace) + "\n") * 2
        assert content["work_ref"] == handle.target_run_ref
        from test_root_human_request_lifecycle import _open_arguments
        opened = _accepted(_call(runtime, channel, "human_request.open",
            **_open_arguments("target-pending-human-file", "offline_action")))
        destination = runtime.root_workspaces.destination_for_human_request(
            request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
        assert destination.location.work_ref == owner_run.run_ref
        assert destination.location.root_session_ref == owner_run.root_session_ref
        assert destination.location.directory == workspace
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="target-human-answer",
            files=(("human.txt", b"original Target pending observation"),))
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
            decision="provided", facts={"material_delivered": True}, note="Material ready.",
            idempotency_key="target-human-answer")
        third = adapter.invoke(replace(invocation, provider_operation_ref="provider-operation:claude-human-resume",
            native_session_ref=first.native_session_ref))
        assert third.native_session_ref == first.native_session_ref
        assert (workspace / "native-observed-human.txt").read_text() == "original Target pending observation"
        assert (workspace / "native-marker.txt").read_text().splitlines() == [str(workspace)] * 3
        from test_stage_resource_handoff import _bundle_channel
        channel = _bundle_channel(runtime)
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=lease.workspace_ref, path=delivered["files"][0]["path"]))["text"] == "original Target pending observation"
    finally:
        runtime.close()


def test_signed_supervisor_rejects_conflicting_cwd_before_spawn(tmp_path):
    from meta_research.provider_supervisor import (
        CODEX_SUPERVISOR_REQUEST_SCHEMA_V2, ProviderSupervisorError,
        ensure_transport_key, supervise, write_supervisor_request,
    )
    transport = tmp_path / "signed-transport"
    _key_path, key = ensure_transport_key(transport)
    signed = tmp_path / "signed-directory"
    argument = tmp_path / "argument-directory"
    signed.mkdir()
    argument.mkdir()
    executable = _native_executable(tmp_path / "local-codex")
    for index, directory in enumerate((signed, argument)):
        invocation_hash = str(index + 1) * 64
        operation = transport / "provider-operations" / invocation_hash[:2] / invocation_hash
        operation.mkdir(parents=True)
        paths = {name: operation / filename for name, filename in (
            ("prompt_path", "prompt.txt"), ("schema_path", "output-schema.json"),
            ("stdout_path", "stdout.jsonl"), ("result_path", "last-message.json"),
            ("lock_path", "supervisor.lock"), ("ready_path", "supervisor-ready.json"),
            ("started_path", "provider-started.json"), ("receipt_path", "supervisor-exit.json"),
            ("stop_path", "supervisor-stop.json"))}
        paths["prompt_path"].write_text("Check actual cwd.")
        paths["schema_path"].write_text('{"type":"object","properties":{"cwd":{"type":"string"}}}')
        request = operation / "supervisor-request.json"
        write_supervisor_request(request, {"schema_ref": CODEX_SUPERVISOR_REQUEST_SCHEMA_V2,
            "invocation_hash": invocation_hash, "working_directory": str(directory),
            "argv": [str(executable), "exec", "--cd", str(argument),
                "--output-schema", str(paths["schema_path"]),
                "--output-last-message", str(paths["result_path"]), "-"],
            "timeout_seconds": 10, "prompt_max_bytes": 4096,
            "stream_max_bytes": 4096, "result_max_bytes": 4096,
            **{name: str(path) for name, path in paths.items()}}, key)
        if index == 0:
            with pytest.raises(ProviderSupervisorError, match="provider_working_directory_conflict"):
                supervise(request)
            assert not paths["started_path"].exists()
            assert not (signed / "native-marker.txt").exists()
            assert not (argument / "native-marker.txt").exists()
        else:
            supervise(request)
            assert (argument / "native-marker.txt").read_text() == str(argument) + "\n"


def test_real_hc_initialization_dispatch_preserves_directory_across_turns(tmp_path):
    adapter = CodexCompanionAdapter(tmp_path / "intent-adapter",
        executable=str(_native_executable(tmp_path / "local-codex")))
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        proposal_drafter=_DeterministicDraftingAdapter(), intent_drafting_provider=adapter,
        host_compute_probe=_DeterministicProbe())
    try:
        human = runtime.owners.human_collaboration
        creation = human.create_quest({}, "native-intent-open")
        destination = runtime.root_workspaces.destination_for_initialization(
            creation["initialization_id"], creation["intent_session"]["ref"])
        runtime.root_workspaces.deliver(destination, delivery_ref="initial-human-file",
            files=(("human.txt", b"pre-Quest pending measurements"),))
        for index in range(2):
            current = human.query_quest_creation(creation["initialization_id"])
            human.send_intent_message(creation["initialization_id"],
                expected_draft_revision=current["quest_draft"]["revision"],
                expected_draft_hash=current["quest_draft"]["hash"],
                message="Review the pending observations.", idempotency_key=f"native-intent-{index}")
            assert human.process_drafting_once()
        assert (destination.location.directory / "native-marker.txt").read_text().splitlines() == [
            str(destination.location.directory)] * 2
        assert (destination.location.quest_ref, destination.location.cycle_ref) == (None, None)
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
    finally:
        runtime.close()


def test_real_deepfetch_initial_acquire_uses_persisted_operation_and_original_locator(tmp_path):
    from meta_research.deepfetch import CodexDeepFetchAdapter
    from meta_research.owners.common import canonical_hash
    from meta_research.root_resident_mcp import RootResidentMcpChannels
    from test_public_first_question_deepfetch import (
        DeterministicDeepFetchProvider, DeterministicProbe, SnapshotAwareProposalDrafter,
        RecordingAcquisitionProvider,
        _authenticate, _open_and_queue_deepfetch,
    )

    class Provider(DeterministicDeepFetchProvider):
        def __init__(self):
            super().__init__()
            self.native = CodexDeepFetchAdapter(tmp_path / "deepfetch-transport")
            self.channels = RootResidentMcpChannels("deepfetch")

        def research_workspace_path(self, job_ref, runtime_binding_hash):
            return self.native.research_workspace_path(job_ref, runtime_binding_hash)

        def bind_workspaces(self, workspaces):
            self.channels.bind_workspaces(workspaces)

        def bind_resident_mcp_authority(self, authority):
            self.channels.bind_authority(authority)

        def execute(self, request):
            run = self.runtime.owners.agent_runtime.query_deepfetch_run_by_ref(request.run_ref)
            assert run.provider_operation_ref == request.job_ref
            assert request.job_ref == f"{request.run_ref}:deepfetch:{run.attempt_generation}"
            scope = dict(run_ref=request.run_ref, attempt_ref=request.attempt_ref,
                root_session_ref=request.root_session_ref, fence_ref=request.fence_ref,
                capability_binding_hash=canonical_hash(request.runtime_binding.as_dict()),
                phase="primary", job_ref=request.job_ref)
            key, access = self.channels.acquire(**scope)
            try:
                location = access.workspace_binding.location
                assert location.directory == self.research_workspace_path(request.job_ref,
                    scope["capability_binding_hash"])
                assert location.directory == self.research_workspace_path(request.job_ref + ":v4-turn:2",
                    scope["capability_binding_hash"])
                (location.directory / "radar.txt").write_bytes(b"native DeepFetch pending observations")
                value = self.channels.call_operation(**scope, operation_id="research_workspace.read",
                    arguments={"workspace_ref": location.workspace_ref, "path": "radar.txt"})
                assert value["text"] == "native DeepFetch pending observations"
                assert value["work_ref"] == request.run_ref
                self.location = location
            finally:
                self.channels.release(key)
            return super().execute(request)

    provider = Provider()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        deepfetch_provider=provider, proposal_drafter=SnapshotAwareProposalDrafter(),
        acquisition_provider=RecordingAcquisitionProvider(),
        host_compute_probe=DeterministicProbe())
    provider.runtime = runtime
    provider.channels.configure_endpoint("http://127.0.0.1:8765")
    client, headers = _authenticate(runtime)
    try:
        initialization_id, queued = _open_and_queue_deepfetch(client, headers, key_prefix="workspace-deepfetch")
        assert queued["deepfetch"]["status"] == "queued"
        assert runtime.deepfetch.process_once()
        assert provider.location.root_kind == "deepfetch"
        assert provider.location.cycle_ref is None
        assert provider.location.quest_ref is None
        assert provider.requests[0].initialization_id == initialization_id
    finally:
        runtime.close()


def test_actual_acquisition_wrapper_binds_preflight_and_scoped_batch_native_work(tmp_path):
    from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper
    from meta_research.acquisition_root import CodexAcquisitionRootAdapter
    from test_external_root_resident_mcp import _AcquisitionDelegate

    class Adapter(CodexAcquisitionRootAdapter):
        def acquire(self, request):
            value = super().acquire(request)
            scope = request.root_runtime_scope
            context = SemanticCallContext(scope["run_ref"], scope["attempt_ref"], scope["root_session_ref"],
                scope["fence_ref"], scope["runtime_binding_hash"], "acquisition", "acquisition-root-turn", "research_workspace.read")
            location = self.runtime.root_workspaces.bind_runtime(context).location
            assert location.directory.parent == self.research_workspace_root
            assert location.cycle_ref is None
            assert location.work_ref == request.session_ref
            assert location.request_ref == request.request_id
            read = _accepted(_call(self.runtime, _channel(self.runtime, context), "research_workspace.read",
                workspace_ref=location.workspace_ref, path="native-marker.txt"))
            assert read["text"] == (str(location.directory) + "\n") * (len(self.locations) + 2)
            assert _accepted(_call(self.runtime, _channel(self.runtime, context), "research_workspace.read",
                workspace_ref=location.workspace_ref, path="native-observed-preflight.txt"))["text"] == "Acquisition preflight pending nonce"
            self.locations.append(location)
            return value

    adapter = Adapter(tmp_path / "acquisition-provider", _AcquisitionDelegate(),
        executable=str(_native_executable(tmp_path / "local-codex")))
    drafting = _DeterministicDraftingAdapter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "runtime"),
        acquisition_provider=adapter, proposal_drafter=drafting, intent_drafting_provider=drafting,
        host_compute_probe=_DeterministicProbe())
    adapter.runtime = runtime
    adapter.locations = []
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        creation = _confirm_direct_quest(runtime)
        session = runtime.owners.agent_runtime.prepare_acquisition_session(
            initialization_id=creation["initialization_id"],
            draft_revision=creation["quest_draft"]["revision"],
            config={"mode": "oa_only", "library_entry_url": ""}, provider=adapter)
        preflight = runtime.root_workspaces.bind_acquisition_session(session.session_ref).location
        assert (preflight.directory / "native-marker.txt").read_text() == str(preflight.directory) + "\n"
        assert (preflight.directory / "preflight-nonce.txt").read_text() == "Acquisition preflight pending nonce"
        runtime.owners.agent_runtime.bind_acquisition_session_to_quest(creation["initialization_id"], creation["quest_ref"])
        for index in range(2):
            runtime.owners.agent_runtime.acquire_literature(session.session_ref,
                AcquisitionBatchRequest(request_id=f"workspace-acquisition-{index}",
                    route_policy="oa_first_then_institution", papers=(AcquisitionPaper(
                        paper_id="paper:1", title="Bounded unavailable paper", doi=None,
                        arxiv_id=None, source_urls=()),)), adapter)
        assert len(adapter.locations) == 2
        assert {item.directory for item in adapter.locations} == {preflight.directory}
        assert {item.root_session_ref for item in adapter.locations} == {session.session_ref}
        assert not (adapter.research_workspace_root / "native-marker.txt").exists()
    finally:
        runtime.close()
