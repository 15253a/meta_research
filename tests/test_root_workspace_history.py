from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import OwnerConflict
from test_target_root_finalizer import _root_finalizer_fixture


class _ControlledWorkspaceRead:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def read(self):
        yield self

    def execute(self, query, parameters):
        assert " FROM ar_target_run_workspaces " in str(query)
        return SimpleNamespace(all=lambda: self.rows)


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


def test_initial_workspace_history_uses_real_root_lifecycle_facts(tmp_path, monkeypatch):
    runtime, _lifecycle, _memory, _authority, handle, directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        issued = runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref)
        assert issued.handle_history == (handle,)
        assert issued.recoveries == ()
        lease = target.query_target_workspace(handle.target_run_ref)
        assert lease.reservation_root_session_ref == handle.root_session_ref
        assert lease.reservation_ordinal == 1
        assert target.query_target_workspace_history(handle.target_run_ref) == (lease,)
        assert target.read_target_workspace_locations(handle.target_run_ref) == ((lease, directory),)
        monkeypatch.setattr(target, "query_target_workspace", lambda _ref: None)
        assert target.query_target_workspace_history(handle.target_run_ref) == ()
        with pytest.raises(OwnerConflict, match="target_run_workspace_unavailable"):
            target.read_target_workspace_locations(handle.target_run_ref)
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


@pytest.mark.parametrize("corruption", ["receipt", "orphan", "gate-changed", "owner-rejects",
                                        "owner-missing", "owner-root", "owner-run"])
def test_workspace_history_rejects_unverified_origin_or_issuer(tmp_path, monkeypatch, corruption):
    runtime, _lifecycle, _memory, _authority, handle, _directory, _evidence = _root_finalizer_fixture(tmp_path)
    try:
        target = runtime.target_run_authorities.agent_runtime
        lease = target.query_target_workspace(handle.target_run_ref)
        issued = runtime.owners.agent_runtime.query_target_root_handle_history(handle.target_ref)
        with target._database.read() as connection:
            rows = [SimpleNamespace(**dict(row._mapping)) for row in connection.execute(text(
                "SELECT * FROM ar_target_run_workspaces WHERE target_run_ref=:ref"),
                {"ref": handle.target_run_ref}).all()]
        monkeypatch.setattr(target, "_database", _ControlledWorkspaceRead(rows))
        observations = iter((lease, None)) if corruption == "gate-changed" else None
        monkeypatch.setattr(target, "query_target_workspace", lambda _ref: next(observations) if observations else lease)
        if corruption == "receipt":
            rows[0].receipt_hash = "0" * 64
        elif corruption == "orphan":
            rows.append(rows[0])
        elif corruption == "owner-missing":
            monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", lambda _ref: None)
        elif corruption == "owner-rejects":
            def rejected(_ref):
                raise OwnerConflict("target_root_handle_history_authority_invalid")
            monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", rejected)
        elif corruption in {"owner-root", "owner-run"}:
            changed = replace(handle, **({"root_session_ref": "unrelated-issued-root"} if corruption == "owner-root"
                                        else {"target_run_ref": "unrelated-issued-run"}))
            invalid = replace(issued, handle_history=(changed,))
            monkeypatch.setattr(target._execution_verifier, "query_target_root_handle_history", lambda _ref: invalid)
        with pytest.raises(OwnerConflict):
            target.query_target_workspace_history(handle.target_run_ref)
    finally:
        runtime.close()
