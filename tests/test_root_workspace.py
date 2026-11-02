from pathlib import Path
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
        request.fence_ref, canonical_hash(request.runtime_binding.as_dict()), kind, "primary", "")


def _external_context(scope):
    return SemanticCallContext(scope["run_ref"], scope["attempt_ref"], scope["root_session_ref"],
        scope["fence_ref"], scope["runtime_binding_hash"], "companion", "primary", "")


def _directories(runtime, context):
    return runtime.root_workspaces.native_directories(runtime.root_workspaces.bind_runtime(context))


def _bundle_context(runtime):
    current = runtime.bundle_stage.query_current()["stage_run_request"]
    run = runtime.owners.agent_runtime.query_bundle_stage_run(current["request_ref"])
    return SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref, run.fence_ref,
        run.runtime_binding_hash, "bundle", "dispatch", "")


def _native_file(runtime, context, file):
    directory, = (item for item in _directories(runtime, context) if item["workspace_ref"] == file["workspace_ref"])
    path = Path(directory["directory"]) / file["path"]
    if "sha256" in file:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == file["sha256"]
    return path


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
        source, = (item for item in _directories(self.runtime, self.context) if item["root_kind"] == "idea")
        assert source["access"] == "read"
        assert (Path(source["directory"]) / "settling.txt").read_text() == "Allow 15 minutes before measuring.\n"
        self.value = source
        return super().generate_draft(request)


def _make(path, idea=None, plan=None):
    idea = idea or _DeterministicIdeaSkill()
    plan = plan or _DeterministicPlanSkill(no_gap=False)
    runtime = _runtime(path, idea_skill=idea, plan_skill=plan)
    idea.runtime = runtime
    plan.runtime = runtime
    return runtime


def test_plan_reads_completed_idea_work_after_restart_through_native_directory(tmp_path):
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
        assert runtime.owners.research_memory.query_snapshot() == before_rm
    finally:
        runtime.close()


def test_cycle_free_roots_are_isolated(tmp_path):
    runtime = _make(tmp_path / "runtime")
    try:
        a = _external_context(_scope(runtime, run_ref="work-a", root_ref="root-a"))
        b = _external_context(_scope(runtime, run_ref="work-b", root_ref="root-b"))
        roots_a = _directories(runtime, a)
        roots_b = _directories(runtime, b)
        assert len(roots_a) == len(roots_b) == 1
        assert roots_a[0]["directory"] != roots_b[0]["directory"]
        assert roots_a[0]["access"] == roots_b[0]["access"] == "read_write"
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
        entry, = receipt["files"]
        assert (destination.location.directory / entry["path"]).read_bytes() == b"sample,value\na,7\n"
        assert (receipt["quest_ref"], receipt["cycle_ref"], receipt["work_ref"]) == (None, None, root_ref)
        assert runtime.owners.research_memory.query_snapshot() == before
        with pytest.raises(SemanticMcpError, match="workspace_initialization_scope_invalid"):
            runtime.root_workspaces.bind_initialization(initialization_id, "copied-session")
    finally:
        runtime.close()


def test_creation_basis_source_keeps_exact_bytes_and_bounded_pages(tmp_path):
    from meta_research.owners.common import OwnerConflict
    runtime = _make(tmp_path / "runtime")
    try:
        creation = runtime.owners.human_collaboration.create_quest({}, "creation-source")
        binding = runtime.root_workspaces.bind_initialization(
            creation["initialization_id"], creation["intent_session"]["ref"])
        path = binding.directory / "field.bin"
        path.write_bytes(b"\x00\xefabc")
        source = {"workspace_ref": binding.location.workspace_ref, "binding": None,
                  "material_key": "field", "path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        basis = {"draft": {"initialization_id": creation["initialization_id"]},
                 "root_session_ref": binding.location.root_session_ref, "sources": [source]}
        read = runtime.owners.research_memory.creation_bases.read_source
        first = read(basis, "field", 0, 2)
        assert first["content"] == b"\x00\xef" and first["next_offset"] == 2 and first["total_bytes"] == 5
        final = read(basis, "field", first["next_offset"], 5)
        assert final["content"] == b"abc" and final["next_offset"] is None
        with pytest.raises(OwnerConflict, match="workspace_page_invalid"):
            read(basis, "field", 0, 65537)
        path.write_bytes(b"other")
        with pytest.raises(OwnerConflict, match="workspace_content_changed"):
            read(basis, "field", 0, 1)
    finally:
        runtime.close()


def test_human_delivery_and_native_files_use_same_original_root_reader(tmp_path):
    from test_root_human_request_lifecycle import _open_arguments
    runtime = _make(tmp_path / "runtime")
    try:
        _confirm_direct_quest(runtime)
        admitted = runtime.idea_stage.start("human-material-idea")
        run = runtime.owners.agent_runtime.query_idea_stage_run(admitted["stage_run_request"]["request_ref"])
        assert run is not None
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash, "idea", "primary", "")
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
        resumed = runtime.owners.agent_runtime.query_managed_run(run.run_ref)
        assert resumed["status"] == "running"
        root, = _directories(runtime, context)
        assert root["work_ref"] == run.run_ref
        assert (Path(root["directory"]) / "agent.txt").read_bytes() == b"native observations"
        assert (Path(root["directory"]) / receipt["files"][0]["path"]).read_bytes() == b"operator observations"
        assert runtime.owners.research_memory.query_snapshot() == before_rm
        assert runtime.owners.research_graph.query_snapshot() == before_rg
    finally:
        runtime.close()


def test_successor_cycle_of_same_quest_reads_previous_work(tmp_path):
    from test_public_reasoning_stage import _reasoning_runtime, _confirm_deepfetch_quest, _tick_reasoning, _DeterministicReasoningSkill
    class Writer(_Writer):
        def generate_draft(self, request):
            prior = getattr(self, "location", None)
            draft = super().generate_draft(request)
            context = _context(request, "idea")
            if prior:
                assert self.location.quest_ref == prior.quest_ref
                assert self.location.cycle_ref != prior.cycle_ref
                prior_dir, = (item for item in _directories(self.runtime, context)
                              if item["workspace_ref"] == prior.workspace_ref)
                assert prior_dir["access"] == "read"
                assert (Path(prior_dir["directory"]) / "settling.txt").read_text() == "Allow 15 minutes before measuring.\n"
                self.read_prior = True
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
        assert writer.read_prior is True
    finally:
        runtime.close()


def test_bundle_reads_issued_target_work_without_frozen_inputs_or_formal_intake(tmp_path):
    from test_formal_run_snapshots import _scenario
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
        value, = (entry for entry in _directories(runtime, _bundle_context(runtime))
                  if entry["workspace_ref"] == lease.workspace_ref)
        assert value["access"] == "read"
        assert (Path(value["directory"]) / "notes.txt").read_text() == "pending Target observation"
        assert value["work_ref"] == handle.target_run_ref
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(handle.target_ref)
        request = runtime.owners.advancement_engine.query_stage_request_by_ref(launch.stage_request_ref)
        assert value["cycle_ref"] == request.cycle_ref
        assert value["quest_ref"] == launch.quest_ref
        assert (Path(value["directory"]) / "outputs/data/run1.txt").read_text() == "36\n"
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
        assert (destination.location.directory / delivered["files"][0]["path"]).read_bytes() == b"positive pending material"
    finally:
        runtime.close()


def test_bundle_reader_honors_target_owner_visibility_gate(tmp_path, monkeypatch):
    from test_formal_run_snapshots import _scenario
    runtime, _lifecycle, _memory, handle, _evidence, _finalizer = _scenario(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease, path = target.read_target_workspace_location(handle.target_run_ref)
        (path / "pending.txt").write_bytes(b"Target pending bytes")
        request = runtime.bundle_stage.query_current()["stage_run_request"]
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request["request_ref"])
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.runtime_binding_hash, "bundle", "dispatch", "")
        own = runtime.root_workspaces.bind_runtime(context).location
        (own.directory / "bundle.txt").write_bytes(b"Bundle positive control")
        file = {"workspace_ref": lease.workspace_ref, "path": "pending.txt"}
        assert _native_file(runtime, context, file).read_text() == "Target pending bytes"
        original = target.query_target_workspace

        def acceptance_pending(target_run_ref):
            return None if target_run_ref == handle.target_run_ref else original(target_run_ref)

        monkeypatch.setattr(target, "query_target_workspace", acceptance_pending)
        assert all(item["target_ref"] != handle.target_ref for item in _directories(runtime, context))
        assert _native_file(runtime, context, {"workspace_ref": own.workspace_ref, "path": "bundle.txt"}).read_text() == "Bundle positive control"
        monkeypatch.setattr(target, "query_target_workspace", original)
        assert _native_file(runtime, context, file).read_text() == "Target pending bytes"
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
        assert (destination.location.directory / delivered["files"][0]["path"]).read_text() == "visible material"
    finally:
        runtime.close()
