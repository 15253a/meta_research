"""Observe real signed native-loss spools through the read-only root API."""
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import read_transport_envelope, write_transport_envelope
from meta_research.root_provider_observations import ProviderRootObservations
from test_acquisition_native_replacement import native_session
from test_root_provider_observations import insert, setup


@pytest.mark.parametrize("completed_receipt", [True, False])
def test_signed_native_replacement_preserves_history_and_read_only_output(native_session, setup, completed_receipt):
    runtime, adapter, _server, request, _workspace, provider = native_session
    assert adapter.acquire(request)[0].status == "obtained"
    db = setup[0]
    with runtime._database.read() as conn:
        actual = dict(conn.execute(text("SELECT * FROM ar_acquisition_sessions WHERE session_ref=:session"),
            {"session": request.session_ref}).mappings().one())
    insert(db, "ar_acquisition_sessions", {**{field: actual[field] for field in (
        "session_ref", "status", "config_hash", "runtime_binding_hash", "preflight_generation", "created_at", "updated_at")},
        "quest_ref": "q1", "initialization_id": "init1", "current_request_id": request.request_id})
    insert(db, "ar_acquisition_requests", {"request_id": request.request_id, "session_ref": request.session_ref,
        "status": "running", "created_at": 4, "updated_at": 4})
    insert(db, "ar_run_controls", {"run_ref": request.request_id, "run_kind": "acquisition", "quest_ref": "q1",
        "root_session_ref": request.session_ref, "attempt_ref": "unmanaged", "attempt_generation": 1,
        "runtime_binding_hash": actual["runtime_binding_hash"], "status": "running", "updated_at": 4})
    key = (provider / "provider-operations/.transport-seal.key").read_bytes()
    receipts = sorted((provider / "root-sessions").glob("*/turn-*.json"))
    first, replacement = [read_transport_envelope(path, key) for path in receipts]
    if not completed_receipt:
        # Completed spool, before its session receipt is durably recorded.
        receipts[1].unlink()
    reader = ProviderRootObservations(SimpleNamespace(data_root=SimpleNamespace(root=provider.parent), _database=db))
    before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in provider.rglob("*") if path.is_file()}
    observed = reader.query("q1")
    assert not observed["limited"], observed["reasons"]
    session = observed["sessions"][0]
    assert session["native_session_ref"] == replacement["native_session_ref"]
    assert len(session["operations"]) == 3  # prior native, exact resume rejection, replacement native
    pages = [reader.query_output("q1", request.session_ref, operation_ref=op["operation_ref"])
        for op in session["operations"]]
    assert {page["native_session_ref"] for page in pages} == {first["native_session_ref"], replacement["native_session_ref"]}
    old = [page for page in pages if page["native_session_ref"] == first["native_session_ref"]]
    assert any("thread.started" in page["text"] for page in old)
    assert any("meta_research.native_resume_rejected" in page["text"] for page in old)
    assert any("new-paper" in page["text"] for page in pages if page["native_session_ref"] == replacement["native_session_ref"])
    assert before == {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in provider.rglob("*") if path.is_file()}

    if completed_receipt:
        bad = {**replacement, "replacement": {**replacement["replacement"], "recovery_checkpoint": {
            **replacement["replacement"]["recovery_checkpoint"], "error_response_hash": "0" * 64}}}
        receipts[1].unlink()
        write_transport_envelope(receipts[1], bad, key)
    else:
        failed_job = replacement["replacement"]["job_ref"]
        failure_dir = provider / "codex-root/provider-operations" / canonical_hash({"job_ref": failed_job}) / "acquisition-root-turn"
        proof = failure_dir / "native-resume-rejected.json"
        bad = json.loads(proof.read_text())
        bad["payload"]["thread_id"] = "other-native"
        proof.write_text(json.dumps(bad))
    rejected = reader.query("q1")
    assert rejected["limited"]
    assert rejected["sessions"][0]["native_session_ref"] == first["native_session_ref"]
    assert len(rejected["sessions"][0]["operations"]) == 1
