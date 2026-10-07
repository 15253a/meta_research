from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.target_run_runtime import (
    AR_TARGET_ROOT_WORKSPACE_CONTINUITY_RECEIPT_KIND,
    AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND,
    TARGET_ROOT_WORKSPACE_CONTINUITY_MANIFEST_SCHEMA_REF,
    _receipt, _target_workspace_continuity_payload,
)
from meta_research.root_workspace import WorkspaceBinding
from meta_research.semantic_mcp import SemanticCallContext
from test_root_workspace import _accepted, _call, _channel
from test_root_human_request_lifecycle import _open_arguments
from test_stage_resource_handoff import _bundle_channel
from test_target_root_finalizer import _root_finalizer_fixture


class _ControlledHistory:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def read(self):
        yield self

    def execute(self, query, parameters):
        table = str(query).split(" FROM ", 1)[1].split(" ", 1)[0]
        return SimpleNamespace(all=lambda: self.rows[table])


class _CorruptedHandleRead:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def read(self):
        with self.database.read() as connection:
            def execute(query, parameters):
                result = connection.execute(query, parameters)
                if " FROM ar_target_root_handle_history " in str(query):
                    rows = [SimpleNamespace(**{**dict(row._mapping), "handle_hash": "0" * 64})
                            for row in result.all()]
                    return SimpleNamespace(all=lambda: rows)
                return result
            yield SimpleNamespace(execute=execute)


def _controlled_history(target, lease):
    with target._database.read() as connection:
        first = dict(connection.execute(text(
            "SELECT * FROM ar_target_run_workspaces WHERE workspace_ref = :workspace_ref"
        ), {"workspace_ref": lease.workspace_ref}).one()._mapping)
    first["status"] = "retired"
    workspaces = [SimpleNamespace(**first)]
    for ordinal in (2, 3):
        row = {**first, "workspace_ref": f"controlled-workspace:{ordinal}",
               "root_session_ref": f"controlled-root:{ordinal}",
               "target_attempt_ref": f"controlled-attempt:{ordinal}",
               "target_fence_ref": f"controlled-fence:{ordinal}",
               "ordinal": ordinal, "status": "active" if ordinal == 3 else "retired",
               "receipt_ref": f"controlled-lease-receipt:{ordinal}"}
        payload = {key: row[key] for key in ("target_ref", "target_run_ref", "root_session_ref",
                                            "target_attempt_ref", "target_fence_ref")}
        payload.update(implementation_relative_path="implementation", inputs_relative_path="inputs")
        row.update(root_name=canonical_hash({"workspace_ref": row["workspace_ref"]}),
                   payload_json=canonical_json(payload), payload_hash=canonical_hash(payload),
                   request_hash=canonical_hash({"command": "reserve_target_run_workspace", **payload}))
        row["receipt_hash"] = _receipt("agent_runtime", AR_TARGET_RUN_WORKSPACE_RECEIPT_KIND,
            row["receipt_ref"], row["workspace_ref"], {**payload,
                "workspace_ref": row["workspace_ref"], "ordinal": ordinal,
                "root_name": row["root_name"], "payload_hash": row["payload_hash"]}).payload_hash
        workspaces.append(SimpleNamespace(**row))
    handles = [SimpleNamespace(target_run_ref=row.target_run_ref, root_session_ref=row.root_session_ref,
        execution_attempt_ref=row.target_attempt_ref, execution_fence_ref=row.target_fence_ref,
        ordinal=row.ordinal) for row in workspaces]
    recoveries, continuities = [], []
    for predecessor, successor in zip(workspaces, workspaces[1:]):
        recovery = SimpleNamespace(target_ref=lease.target_ref,
            retired_workspace_ref=predecessor.workspace_ref,
            old_execution_attempt_ref=predecessor.target_attempt_ref,
            new_execution_attempt_ref=successor.target_attempt_ref,
            ordinal=predecessor.ordinal, transition_ref=f"controlled-transition:{successor.ordinal}")
        manifest = {"schema_ref": TARGET_ROOT_WORKSPACE_CONTINUITY_MANIFEST_SCHEMA_REF,
                    "excluded_top_level_paths": ["inputs"], "entries": []}
        payload = _target_workspace_continuity_payload(
            continuity_ref=f"controlled-continuity:{successor.ordinal}", transition_ref=recovery.transition_ref,
            target_ref=lease.target_ref, predecessor_workspace_ref=predecessor.workspace_ref,
            predecessor_workspace_payload_hash=predecessor.payload_hash,
            successor_workspace_ref=successor.workspace_ref,
            successor_workspace_payload_hash=successor.payload_hash, manifest_hash=canonical_hash(manifest))
        receipt_ref = f"controlled-continuity-receipt:{successor.ordinal}"
        receipt = _receipt("agent_runtime", AR_TARGET_ROOT_WORKSPACE_CONTINUITY_RECEIPT_KIND,
            receipt_ref, payload["continuity_ref"], {"payload_hash": canonical_hash(payload), **payload})
        continuities.append(SimpleNamespace(**payload, manifest_json=canonical_json(manifest),
            payload_json=canonical_json(payload), payload_hash=canonical_hash(payload),
            request_hash=canonical_hash({"command": "accept_target_root_workspace_continuity", **payload}),
            receipt_ref=receipt_ref, receipt_hash=receipt.payload_hash))
        recoveries.append(recovery)
    return {
        "ar_target_run_workspaces": workspaces,
        "ar_target_root_handle_history": handles,
        "ar_target_root_provider_recoveries": recoveries,
        "ar_target_root_workspace_continuities": continuities,
    }


def _controlled_owner_reader(monkeypatch, target, rows, initial_handle):
    handles = tuple(replace(initial_handle, root_session_ref=row.root_session_ref,
        execution_attempt_ref=row.target_attempt_ref, execution_fence_ref=row.target_fence_ref)
        for row in rows["ar_target_run_workspaces"])
    history = SimpleNamespace(target_ref=initial_handle.target_ref, handle_history=handles,
        recoveries=tuple(SimpleNamespace(transition_ref=row.transition_ref, ordinal=row.ordinal,
            old_handle=handles[index], replacement_handle=handles[index + 1])
            for index, row in enumerate(rows["ar_target_root_provider_recoveries"])))
    monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", lambda _ref: history)
    return history


def test_initial_workspace_history_uses_real_root_lifecycle_facts(tmp_path):
    runtime, _lifecycle, _memory, _authority, handle, directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        issued = runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref)
        assert issued.handle_history == (handle,)
        assert issued.recoveries == ()
        lease = target.query_target_workspace(handle.target_run_ref)
        assert target.query_target_workspace_history(handle.target_run_ref) == (lease,)
        assert target.read_target_workspace_locations(handle.target_run_ref) == ((lease, directory),)
    finally:
        runtime.close()


def test_real_root_history_reader_rejects_corrupted_initial_handle_hash(tmp_path, monkeypatch):
    runtime, _lifecycle, _memory, _authority, handle, _directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        reader = runtime.target_root_lifecycle
        original = reader._database
        assert runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref).handle_history == (handle,)
        monkeypatch.setattr(reader, "_database", _CorruptedHandleRead(original))
        with pytest.raises(OwnerConflict, match="target_root_handle_history_integrity_invalid"):
            runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref)
        monkeypatch.setattr(reader, "_database", original)
        assert runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref).handle_history == (handle,)
    finally:
        runtime.close()


def test_controlled_history_keeps_late_human_material_in_original_work_and_visible_to_bundle(tmp_path, monkeypatch):
    runtime, _lifecycle, _memory, _authority, handle, original_directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease = target.query_target_workspace(handle.target_run_ref)
        run = runtime.owners.agent_runtime.harness_runs.query_target_run_by_ref(handle.target_run_ref)
        runtime.owners.agent_runtime.harness_runs.start_operation(run_ref=run.run_ref,
            operation_ref="controlled-original-human-turn", generation=1, invocation_hash="b" * 64, resume=False)
        context = SemanticCallContext(run.run_ref, run.attempt_ref, run.root_session_ref,
            run.fence_ref, run.capability_binding_hash, "target", "target_root", "research_workspace.read")
        opened = _accepted(_call(runtime, _channel(runtime, context), "human_request.open",
            **_open_arguments("controlled-late-original-file", "offline_action")))
        rows = _controlled_history(target, lease)
        _controlled_owner_reader(monkeypatch, target, rows, handle)
        current = target._stored_target_workspace(rows["ar_target_run_workspaces"][-1], require_current=False)
        monkeypatch.setattr(target, "_database", _ControlledHistory(rows))
        monkeypatch.setattr(target, "query_target_workspace", lambda _run_ref: current)
        history = target.query_target_workspace_history(handle.target_run_ref)
        assert [workspace.ordinal for workspace in history] == [1, 2, 3]
        locations = target.read_target_workspace_locations(handle.target_run_ref)
        assert locations[0][1] == original_directory
        with pytest.raises(OwnerConflict, match="target_run_workspace_unavailable"):
            target.resolve_target_workspace(target_ref=handle.target_ref, target_run_ref=handle.target_run_ref,
                root_session_ref=handle.root_session_ref, attempt_ref=handle.execution_attempt_ref,
                fence_ref=handle.execution_fence_ref)
        destination = runtime.root_workspaces.destination_for_human_request(
            request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
        assert destination.location.root_session_ref == handle.root_session_ref
        assert destination.location.work_ref == handle.target_run_ref
        assert destination.location.directory == original_directory
        delivered = runtime.root_workspaces.deliver(destination, delivery_ref="controlled-late-answer",
            files=(("human.txt", b"Late original-root pending material"),))
        assert not (locations[-1][1] / delivered["files"][0]["path"]).exists()
        channel = _bundle_channel(runtime)
        value = _accepted(_call(runtime, channel, "research_workspace.read", workspace_ref=lease.workspace_ref,
            path=delivered["files"][0]["path"]))
        assert value["text"] == "Late original-root pending material"
        assert value["root_session_ref"] == handle.root_session_ref
        source = runtime.root_workspaces._target_locations(handle.target_ref, history=True)[-1]
        monkeypatch.setattr(runtime.root_workspaces, "bind_runtime",
            lambda _context: WorkspaceBinding(source, "controlled-scope"))
        assert lease.workspace_ref in {location.workspace_ref for location in runtime.root_workspaces._visible(context)}
        monkeypatch.setattr(target, "query_target_workspace", lambda _run_ref: None)
        assert target.query_target_workspace_history(handle.target_run_ref) == ()
        with pytest.raises(OwnerConflict, match="target_run_workspace_unavailable"):
            runtime.root_workspaces.destination_for_human_request(
                request_ref=opened["request_ref"], waiter_ref=opened["waiter"]["waiter_ref"])
    finally:
        runtime.close()


@pytest.mark.parametrize("corruption", ["missing-continuity", "receipt", "lease", "handle", "orphan", "gate-changed",
                                        "owner-rejects", "owner-missing", "owner-handle"])
def test_controlled_historical_projection_rejects_unverified_chain(tmp_path, monkeypatch, corruption):
    runtime, _lifecycle, _memory, _authority, handle, _directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease = target.query_target_workspace(handle.target_run_ref)
        rows = _controlled_history(target, lease)
        issued = _controlled_owner_reader(monkeypatch, target, rows, handle)
        current = target._stored_target_workspace(rows["ar_target_run_workspaces"][-1], require_current=False)
        monkeypatch.setattr(target, "_database", _ControlledHistory(rows))
        observed = iter((current, None)) if corruption == "gate-changed" else None
        monkeypatch.setattr(target, "query_target_workspace", lambda _ref: next(observed) if observed else current)
        if corruption == "missing-continuity":
            rows["ar_target_root_workspace_continuities"].pop(0)
        elif corruption == "receipt":
            rows["ar_target_root_workspace_continuities"][0].receipt_hash = "0" * 64
        elif corruption == "lease":
            rows["ar_target_run_workspaces"][0].receipt_hash = "0" * 64
        elif corruption == "handle":
            rows["ar_target_root_handle_history"][0].root_session_ref = "unrelated-root"
        elif corruption == "orphan":
            rows["ar_target_run_workspaces"].append(rows["ar_target_run_workspaces"][0])
        elif corruption == "owner-missing":
            monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", lambda _ref: None)
        elif corruption == "owner-rejects":
            def rejected(_ref):
                raise OwnerConflict("target_root_handle_history_authority_invalid")
            monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", rejected)
        elif corruption == "owner-handle":
            issued.handle_history = (replace(issued.handle_history[0], root_session_ref="unrelated-issued-root"),
                                     *issued.handle_history[1:])
        with pytest.raises(OwnerConflict):
            target.query_target_workspace_history(handle.target_run_ref)
    finally:
        runtime.close()
