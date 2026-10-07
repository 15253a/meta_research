import base64
import hashlib
import os
from dataclasses import replace

import pytest

from meta_research.owners.common import canonical_hash
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError
from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
from test_public_plan_stage import (
    _runtime, _confirm_direct_quest, _finish_idea_stage,
    _DeterministicIdeaSkill, _DeterministicPlanSkill,
)
from test_dataset_effect_scope_recovery import _scope


def _context(request, kind):
    return SemanticCallContext(request.run_ref, request.attempt_ref, request.root_session_ref,
        request.fence_ref, canonical_hash(request.runtime_binding.as_dict()), kind, "primary", "research_workspace.read")


def _external_context(scope):
    return SemanticCallContext(scope["run_ref"], scope["attempt_ref"], scope["root_session_ref"],
        scope["fence_ref"], scope["runtime_binding_hash"], "companion", "primary", "research_workspace.read")


def _channel(runtime, context):
    return runtime.harnesses.issue_resident_mcp_channel(run_ref=context.run_ref,
        attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
        fence_ref=context.fence_ref, capability_binding_hash=context.capability_binding_hash,
        root_kind=context.root_kind, phase=context.phase, subject_policy="operation_tree",
        operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS[context.root_kind])


def _call(runtime, channel, operation, **arguments):
    status, response, _ = runtime.harnesses.dispatch_mcp_http(channel.connection.token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": operation, "arguments": arguments}}, mcp_session_id=None)
    assert status == 200, response
    return response["result"]


def _accepted(result):
    assert not result.get("isError"), result
    return result["structuredContent"]


class _Writer(_DeterministicIdeaSkill):
    def generate_draft(self, request):
        location = self.runtime.root_workspaces.bind_runtime(_context(request, "idea")).location
        (location.directory / "settling.txt").write_bytes(b"Allow 15 minutes before measuring.\n")
        self.location = location
        return super().generate_draft(request)


class _Reader(_DeterministicPlanSkill):
    def generate_draft(self, request):
        self.context = _context(request, "plan")
        channel = _channel(self.runtime, self.context)
        page = _accepted(_call(self.runtime, channel, "research_workspace.discover"))
        entry, = [item for item in page["files"] if item["path"] == "settling.txt"]
        value = _accepted(_call(self.runtime, channel, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path=entry["path"], expected_sha256=entry["sha256"]))
        assert value["text"] == "Allow 15 minutes before measuring.\n"
        assert value["root_kind"] == "idea"
        self.value = value
        self.page = page
        return super().generate_draft(request)


def _make(path, idea=None, plan=None):
    idea = idea or _DeterministicIdeaSkill()
    plan = plan or _DeterministicPlanSkill(no_gap=False)
    runtime = _runtime(path, idea_skill=idea, plan_skill=plan)
    idea.runtime = runtime
    plan.runtime = runtime
    return runtime


def test_plan_reads_completed_idea_work_after_restart_through_public_mcp(tmp_path):
    writer = _Writer()
    runtime = _make(tmp_path / "runtime", idea=writer)
    _confirm_direct_quest(runtime)
    completed = _finish_idea_stage(runtime)
    request = completed["stage_run_request"]
    run = runtime.owners.agent_runtime.query_idea_stage_run(request["request_ref"])
    assert run.status == "completed"
    runtime.close()
    reader = _Reader(no_gap=False)
    runtime = _make(tmp_path / "runtime", plan=reader)
    try:
        before_rm = runtime.owners.research_memory.query_snapshot()
        for _ in range(6):
            assert runtime.plan_stage.process_once()
            if hasattr(reader, "value"):
                break
        assert reader.value["work_ref"] == run.run_ref
        assert reader.value["cycle_ref"] == run.cycle_ref
        assert reader.value["request_ref"] == run.request_ref
        assert reader.value["sha256"] == hashlib.sha256(b"Allow 15 minutes before measuring.\n").hexdigest()
        assert runtime.owners.research_memory.query_snapshot() == before_rm
    finally:
        runtime.close()


def test_cycle_free_roots_public_read_bytes_bounds_and_symlink_race(tmp_path, monkeypatch):
    runtime = _make(tmp_path / "runtime")
    try:
        a = _external_context(_scope(runtime, run_ref="work-a", root_ref="root-a"))
        b = _external_context(_scope(runtime, run_ref="work-b", root_ref="root-b"))
        location_a = runtime.root_workspaces.bind_runtime(a).location
        location_b = runtime.root_workspaces.bind_runtime(b).location
        (location_a.directory / "binary.bin").write_bytes(b"\x00\xff\x7fABC")
        (location_b.directory / "secret.txt").write_bytes(b"other root bytes")
        channel_a = _channel(runtime, a)
        channel_b = _channel(runtime, b)
        page = _accepted(_call(runtime, channel_a, "research_workspace.discover"))
        entry, = page["files"]
        assert entry["path"] == "binary.bin"
        assert entry["cycle_ref"] is None
        value = _accepted(_call(runtime, channel_a, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="binary.bin", max_bytes=3))
        assert base64.b64decode(value["base64"]) == b"\x00\xff\x7f"
        assert value["next_offset"] == 3
        denied = _call(runtime, channel_a, "research_workspace.read",
            workspace_ref=location_b.workspace_ref, path="secret.txt")
        assert denied["structuredContent"]["code"] == "workspace_not_visible"
        assert _accepted(_call(runtime, channel_b, "research_workspace.read",
            workspace_ref=location_b.workspace_ref, path="secret.txt"))["text"] == "other root bytes"
        for path in ("../secret.txt", "C:/secret.txt", "/secret.txt", "sub\\secret.txt"):
            rejected = _call(runtime, channel_a, "research_workspace.read",
                workspace_ref=entry["workspace_ref"], path=path)
            assert rejected["structuredContent"]["code"] == "workspace_path_invalid"
        (location_a.directory / "changed.txt").write_bytes(b"before")
        digest = hashlib.sha256(b"before").hexdigest()
        (location_a.directory / "changed.txt").write_bytes(b"after")
        rejected = _call(runtime, channel_a, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="changed.txt", expected_sha256=digest)
        assert rejected["structuredContent"]["code"] == "workspace_content_changed"
        (location_a.directory / "race.txt").write_bytes(b"safe")
        original = os.open
        def replace_at_open(path, flags, *args, **kwargs):
            if path == "race.txt":
                (location_a.directory / "race.txt").unlink()
                (location_a.directory / "race.txt").symlink_to(location_b.directory / "secret.txt")
            return original(path, flags, *args, **kwargs)
        monkeypatch.setattr(os, "open", replace_at_open)
        rejected = _call(runtime, channel_a, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="race.txt")
        assert rejected["structuredContent"]["code"] == "workspace_file_unsafe"
        assert _accepted(_call(runtime, channel_a, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="binary.bin", offset=3))["text"] == "ABC"
    finally:
        runtime.close()


def test_initialization_delivery_keeps_real_hc_root_without_quest(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        human = runtime.owners.human_collaboration
        opened = human.create_quest({}, "workspace-init")
        initialization_id = opened["initialization_id"]
        human.send_intent_message(initialization_id,
            expected_draft_revision=opened["quest_draft"]["revision"],
            expected_draft_hash=opened["quest_draft"]["hash"],
            message="Inspect the observations.", idempotency_key="workspace-intent")
        creation = human.query_quest_creation(initialization_id)
        root_ref = creation["intent_session"]["ref"]
        destination = runtime.root_workspaces.destination_for_initialization(initialization_id, root_ref)
        before = runtime.owners.research_memory.query_snapshot()
        receipt = runtime.root_workspaces.deliver(destination, delivery_ref="material-1",
            files=(("pilot.csv", b"sample,value\na,7\n"),))
        assert runtime.root_workspaces.deliver(destination, delivery_ref="material-1",
            files=(("pilot.csv", b"sample,value\na,7\n"),)) == receipt
        with pytest.raises(SemanticMcpError, match="workspace_delivery_conflict"):
            runtime.root_workspaces.deliver(destination, delivery_ref="material-1",
                files=(("pilot.csv", b"different"),))
        page = runtime.root_workspaces.discover_initialization(initialization_id, root_ref)
        entry, = page["files"]
        value = runtime.root_workspaces.read_initialization(initialization_id, root_ref,
            workspace_ref=entry["workspace_ref"], path=entry["path"])
        assert value["content"] == b"sample,value\na,7\n"
        assert (value["quest_ref"], value["cycle_ref"], value["work_ref"]) == (None, None, root_ref)
        assert runtime.owners.research_memory.query_snapshot() == before
        with pytest.raises(SemanticMcpError, match="workspace_initialization_scope_invalid"):
            runtime.root_workspaces.discover_initialization(initialization_id, "copied-session")
    finally:
        runtime.close()


def test_human_delivery_and_native_files_use_same_original_root_reader(tmp_path):
    from test_root_human_request_lifecycle import _open_arguments
    runtime = _make(tmp_path / "runtime")
    try:
        scope = _scope(runtime, run_ref="human-material-work", root_ref="human-material-root")
        context = _external_context(scope)
        location = runtime.root_workspaces.bind_runtime(context).location
        (location.directory / "agent.txt").write_bytes(b"native observations")
        channel = _channel(runtime, context)
        before_rm = runtime.owners.research_memory.query_snapshot()
        before_rg = runtime.owners.research_graph.query_snapshot()
        opened = _accepted(_call(runtime, channel, "human_request.open",
            **_open_arguments("deliver-material", "offline_action")))
        destination = runtime.root_workspaces.destination_for_human_request(
            request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
        assert destination.location == location
        receipt = runtime.root_workspaces.deliver(destination, delivery_ref="human-response-material",
            files=(("human.txt", b"operator observations"),))
        runtime.owners.human_collaboration.respond_to_human_request(opened["request_ref"],
            decision="provided", facts={"material_delivered": True}, note="Material is ready.",
            idempotency_key="human-material-answer")
        resumed = runtime.owners.agent_runtime.query_managed_run(scope["run_ref"])
        assert resumed["status"] == "running"
        page = _accepted(_call(runtime, _channel(runtime, context), "research_workspace.discover"))
        assert {item["path"] for item in page["files"]} == {"agent.txt", receipt["files"][0]["path"]}
        for entry in page["files"]:
            content = _accepted(_call(runtime, _channel(runtime, context), "research_workspace.read",
                workspace_ref=entry["workspace_ref"], path=entry["path"]))
            assert content["text"] == ("native observations" if entry["path"] == "agent.txt" else "operator observations")
            assert content["work_ref"] == scope["run_ref"]
        assert runtime.owners.research_memory.query_snapshot() == before_rm
        assert runtime.owners.research_graph.query_snapshot() == before_rg
    finally:
        runtime.close()


def test_successor_cycle_of_same_quest_cannot_read_previous_work(tmp_path):
    from test_public_reasoning_stage import _reasoning_runtime, _confirm_deepfetch_quest, _tick_reasoning, _DeterministicReasoningSkill
    class Writer(_Writer):
        def generate_draft(self, request):
            prior = getattr(self, "location", None)
            draft = super().generate_draft(request)
            context = _context(request, "idea")
            channel = _channel(self.runtime, context)
            page = _accepted(_call(self.runtime, channel, "research_workspace.discover"))
            assert [item["work_ref"] for item in page["files"]] == [request.run_ref]
            if prior:
                assert self.location.quest_ref == prior.quest_ref
                assert self.location.cycle_ref != prior.cycle_ref
                rejected = _call(self.runtime, channel, "research_workspace.read",
                    workspace_ref=prior.workspace_ref, path="settling.txt")
                assert rejected["structuredContent"]["code"] == "workspace_not_visible"
                assert _accepted(_call(self.runtime, channel, "research_workspace.read",
                    workspace_ref=self.location.workspace_ref, path="settling.txt"))["text"] == "Allow 15 minutes before measuring.\n"
                self.rejected_prior = True
            return replace(draft, primary_session_ref="idea-session-" + request.run_ref)
    writer = Writer(no_viable=True)
    runtime = _reasoning_runtime(tmp_path / "cycles", reasoning_skill=_DeterministicReasoningSkill(), idea_skill=writer)
    writer.runtime = runtime
    try:
        _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(16):
            current = _tick_reasoning(runtime)
            if current["stage_commit"] is not None:
                break
        assert current["stage_commit"] is not None
        _finish_idea_stage(runtime)
        assert writer.rejected_prior is True
    finally:
        runtime.close()


def test_bundle_reads_issued_target_work_without_frozen_inputs_or_formal_intake(tmp_path):
    from test_formal_run_snapshots import _scenario
    from test_stage_resource_handoff import _bundle_channel
    runtime, lifecycle, memory, handle, evidence, finalizer = _scenario(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease = target.query_target_workspace(handle.target_run_ref)
        original_lease, path = target.read_target_workspace_location(handle.target_run_ref)
        assert original_lease == lease
        assert path.name == canonical_hash({"workspace_ref": lease.workspace_ref})
        (path / "notes.txt").write_bytes(b"pending Target observation")
        before = tuple(replace(item, verification_observed_at=None)
                       for item in runtime.owners.research_memory.query_asset_inventory())
        roles_before = runtime.owners.research_graph.query_asset_roles()
        channel = _bundle_channel(runtime)
        page = _accepted(_call(runtime, channel, "research_workspace.discover"))
        own_target = [item for item in page["files"] if item["target_ref"] == handle.target_ref]
        entry, = [item for item in own_target if item["path"] == "notes.txt"]
        value = _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="notes.txt"))
        assert value["text"] == "pending Target observation"
        assert value["work_ref"] == handle.target_run_ref
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(handle.target_ref)
        request = runtime.owners.advancement_engine.query_stage_request_by_ref(launch.stage_request_ref)
        assert value["cycle_ref"] == request.cycle_ref
        assert value["quest_ref"] == launch.quest_ref
        assert all(not item["path"].startswith("inputs/") for item in own_target)
        rejected = _call(runtime, channel, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="inputs/manifest.json")
        assert rejected["structuredContent"]["code"] == "workspace_file_unsafe"
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=entry["workspace_ref"], path="outputs/data/run1.txt"))["text"] == "36\n"
        assert tuple(replace(item, verification_observed_at=None)
                     for item in runtime.owners.research_memory.query_asset_inventory()) == before
        assert runtime.owners.research_graph.query_asset_roles() == roles_before
        assert target.query_target_workspace(handle.target_run_ref) == lease
    finally:
        runtime.close()


def test_delivery_rejects_inbox_replacement_before_publication(tmp_path, monkeypatch):
    runtime = _make(tmp_path / "runtime")
    try:
        creation = runtime.owners.human_collaboration.create_quest({}, "delivery-race")
        destination = runtime.root_workspaces.destination_for_initialization(
            creation["initialization_id"], creation["intent_session"]["ref"])
        outside = tmp_path / "unauthorized"
        outside.mkdir()
        inbox = destination.location.directory / "inbox"
        original_rename = os.rename

        def replace_parent(source, target, *args, **kwargs):
            inbox.rmdir()
            inbox.symlink_to(outside, target_is_directory=True)
            return original_rename(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "rename", replace_parent)
        with pytest.raises(SemanticMcpError):
            runtime.root_workspaces.deliver(destination, delivery_ref="inbox-race",
                files=(("note.txt", b"must remain pending in its original work"),))
        assert list(outside.rglob("*")) == []
        inbox.unlink()
        monkeypatch.setattr(os, "rename", original_rename)
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="positive-control",
            files=(("note.txt", b"positive pending material"),))
        value = runtime.root_workspaces.read_initialization(destination.request_ref,
            destination.waiter_ref, workspace_ref=destination.location.workspace_ref,
            path=delivered["files"][0]["path"])
        assert value["content"] == b"positive pending material"
    finally:
        runtime.close()


def test_bundle_reader_honors_target_owner_visibility_gate(tmp_path, monkeypatch):
    from test_formal_run_snapshots import _scenario
    from test_stage_resource_handoff import _bundle_channel
    runtime, _lifecycle, _memory, handle, _evidence, _finalizer = _scenario(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease, path = target.read_target_workspace_location(handle.target_run_ref)
        (path / "pending.txt").write_bytes(b"Target pending bytes")
        request = runtime.bundle_stage.query_current()["stage_run_request"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request["request_ref"])
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash, "bundle", "dispatch", "research_workspace.read")
        own = runtime.root_workspaces.bind_runtime(context).location
        (own.directory / "bundle.txt").write_bytes(b"Bundle positive control")
        channel = _bundle_channel(runtime)
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=lease.workspace_ref, path="pending.txt"))["text"] == "Target pending bytes"
        original = target.query_target_workspace

        def acceptance_pending(target_run_ref):
            return None if target_run_ref == handle.target_run_ref else original(target_run_ref)

        monkeypatch.setattr(target, "query_target_workspace", acceptance_pending)
        page = _accepted(_call(runtime, channel, "research_workspace.discover"))
        assert all(item["target_ref"] != handle.target_ref for item in page["files"])
        assert _call(runtime, channel, "research_workspace.read", workspace_ref=lease.workspace_ref,
            path="pending.txt")["structuredContent"]["code"] == "workspace_not_visible"
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=own.workspace_ref, path="bundle.txt"))["text"] == "Bundle positive control"
        monkeypatch.setattr(target, "query_target_workspace", original)
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=lease.workspace_ref, path="pending.txt"))["text"] == "Target pending bytes"
    finally:
        runtime.close()


def test_delivery_rejects_reserved_directory_components(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        creation = runtime.owners.human_collaboration.create_quest({}, "delivery-reserved")
        destination = runtime.root_workspaces.destination_for_initialization(
            creation["initialization_id"], creation["intent_session"]["ref"])
        with pytest.raises(SemanticMcpError, match="workspace_delivery_invalid"):
            runtime.root_workspaces.deliver(destination, delivery_ref="reserved",
                files=(("nested/.delivery-user/note.txt", b"hidden material"),))
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="visible",
            files=(("nested/human/note.txt", b"visible material"),))
        (destination.location.directory / ".delivery-internal.txt").write_bytes(b"reserved internal file")
        page = runtime.root_workspaces.discover_initialization(destination.request_ref, destination.waiter_ref)
        assert [item["path"] for item in page["files"]] == [delivered["files"][0]["path"]]
    finally:
        runtime.close()


def test_public_reader_reports_large_files_and_bounded_discovery(tmp_path, monkeypatch):
    import meta_research.root_workspace as module
    runtime = _make(tmp_path / "runtime")
    try:
        context = _external_context(_scope(runtime, run_ref="limits-work", root_ref="limits-root"))
        location = runtime.root_workspaces.bind_runtime(context).location
        channel = _channel(runtime, context)
        (location.directory / "note.txt").write_bytes(b"normal pending material")
        with (location.directory / "large.bin").open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        page = _accepted(_call(runtime, channel, "research_workspace.discover"))
        entries = {item["path"]: item for item in page["files"]}
        assert set(entries) == {"note.txt", "large.bin"}
        assert entries["large.bin"]["bytes"] == 64 * 1024 * 1024 + 1
        assert entries["large.bin"]["readable"] is False
        assert entries["large.bin"]["sha256"] is None
        assert entries["large.bin"]["read_error"] == {"code": "workspace_file_too_large", "maximum_bytes": 64 * 1024 * 1024}
        assert entries["note.txt"]["readable"] is True
        assert page["limits"] == {"maximum_file_bytes": 64 * 1024 * 1024,
            "maximum_chunk_bytes": 65536, "maximum_scan_entries": 10000}
        result = _call(runtime, channel, "research_workspace.read",
            workspace_ref=location.workspace_ref, path="large.bin")
        assert result["structuredContent"]["code"] == "workspace_file_too_large"
        assert result["structuredContent"]["details"]["maximum_bytes"] == 64 * 1024 * 1024
        assert _accepted(_call(runtime, channel, "research_workspace.read",
            workspace_ref=location.workspace_ref, path="note.txt"))["text"] == "normal pending material"
        (location.directory / "large.bin").unlink()
        (location.directory / "empty").mkdir()
        monkeypatch.setattr(module, "_MAX_SCAN_FILES", 1)
        limited = _call(runtime, channel, "research_workspace.discover")
        assert limited["structuredContent"]["code"] == "workspace_scan_limit_exceeded"
        monkeypatch.setattr(module, "_MAX_SCAN_FILES", 10000)
        assert _accepted(_call(runtime, channel, "research_workspace.discover"))["files"][0]["path"] == "note.txt"
    finally:
        runtime.close()
