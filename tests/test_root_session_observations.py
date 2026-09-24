from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

import meta_research.root_session_observations as observations
from meta_research.root_session_observations import RootSessionObservations
from meta_research.stage_root_observations import _bundle_rolling_unit_ref
from meta_research.provider_supervisor import ensure_transport_key, write_transport_envelope
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request
from test_stage_root_observations import _seed_spool


class _NoProviderRoots:
    def query(self, _quest_ref):
        return {"sessions": [], "limited": False, "reasons": []}

    def query_output(self, *args, **kwargs):
        raise ValueError("root_session_not_found")


@pytest.fixture
def root_reader(tmp_path, monkeypatch):
    runtime = _bundle_runtime(tmp_path / "root-observation-fixture")
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        current = runtime.bundle_stage.query_current()
        request = runtime.owners.advancement_engine.query_bundle_stage_request(
            current["stage_run_request"]["cycle_ref"]
        )
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request.request_ref)
        reader = RootSessionObservations(runtime)
        monkeypatch.setattr(reader, "_provider_reader", lambda: _NoProviderRoots())
        yield reader, runtime, request, run
    finally:
        runtime.close()


def _add_stage_call(runtime, run, phase, *, status="completed", sequence=1):
    if phase == "primary":
        invocation = run.primary_invocation
    elif phase == "review":
        invocation = run.review_invocation
    else:
        invocation = SimpleNamespace(
            operation_ref=run.primary_invocation.operation_ref,
            invocation_ref=_bundle_rolling_unit_ref(
                operation_ref=run.primary_invocation.operation_ref,
                operation_name=phase, attempt_ref=run.attempt_ref,
            ),
        )
    kind = "bundle_primary" if phase == "primary" else "bundle_review"
    with runtime._database.write() as connection:
        connection.execute(text("""
            INSERT INTO ar_provider_units (unit_ref,operation_ref,run_ref,attempt_ref,
                fence_ref,unit_kind,status,started_at,completed_at)
            VALUES (:unit_ref,:operation_ref,:run_ref,:attempt_ref,:fence_ref,
                :unit_kind,:status,:started_at,:completed_at)
        """), {
            "unit_ref": invocation.invocation_ref, "operation_ref": invocation.operation_ref,
            "run_ref": run.run_ref, "attempt_ref": run.attempt_ref,
            "fence_ref": run.fence_ref, "unit_kind": kind, "status": status,
            "started_at": float(sequence), "completed_at": float(sequence) + .5 if status == "completed" else None,
        })
    scope = {
        "run_ref": run.run_ref, "run_kind": "bundle_stage",
        "attempt_ref": run.attempt_ref, "attempt_generation": run.attempt_generation,
        "root_session_ref": run.root_session_ref, "fence_ref": run.fence_ref,
        "status": "completed" if status == "completed" else "running",
        "unit_ref": invocation.invocation_ref, "operation_ref": invocation.operation_ref,
        "unit_kind": kind, "operation_name": phase,
    }
    directory = _seed_spool(runtime.data_root.root, scope, (
        {"type": "thread.started", "thread_id": "native-bundle-root"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": f"{phase} public message"}},
        {"type": "turn.completed", "thread_id": "native-bundle-root"},
    ), operation_name=phase)
    return invocation.invocation_ref, directory


def test_stage_root_merges_primary_review_and_dispatch_without_losing_messages(root_reader):
    reader, runtime, request, run = root_reader
    calls = [_add_stage_call(runtime, run, phase, sequence=index + 1)[0]
             for index, phase in enumerate(("primary", "review", "dispatch-2"))]
    listing = reader.query(request.accepted_question.quest_ref)
    assert listing["limited"] is False
    session = next(s for s in listing["sessions"] if s["session_ref"] == run.root_session_ref)
    assert [o["operation_ref"] for o in session["operations"]] == calls
    assert session["is_executing"] is False
    assert run.root_session_ref not in listing["active_session_refs"]
    for operation_ref, phase in zip(calls, ("primary", "review", "dispatch-2")):
        page = reader.query_output(request.accepted_question.quest_ref, run.root_session_ref,
                                   operation_ref=operation_ref)
        assert f"{phase} public message" in page["text"]
        assert page["session_ref"] == run.root_session_ref
        assert page["operation_ref"] == operation_ref
        assert page["native_session_ref"] == "native-bundle-root"
        assert page["status"] == "terminal"


def test_history_stays_bound_when_a_new_call_becomes_current(root_reader, monkeypatch):
    reader, runtime, request, run = root_reader
    old_ref, _ = _add_stage_call(runtime, run, "primary")
    before = reader.query_output(request.accepted_question.quest_ref, run.root_session_ref,
                                 operation_ref=old_ref)
    newer_ref, _ = _add_stage_call(runtime, run, "dispatch-7", status="active", sequence=2)
    monkeypatch.setattr(observations, "_process_is_bound", lambda *args: True)
    after = reader.query_output(request.accepted_question.quest_ref, run.root_session_ref,
                                operation_ref=old_ref)
    assert after["stream_ref"] == before["stream_ref"]
    assert after["text"] == before["text"]
    assert "dispatch-7" not in after["text"]
    listing = reader.query(request.accepted_question.quest_ref)
    assert listing["active_session_refs"] == [run.root_session_ref]
    session = next(s for s in listing["sessions"] if s["session_ref"] == run.root_session_ref)
    assert session["operations"][-1]["operation_ref"] == newer_ref
    assert session["operations"][-1]["status"] == "executing"


def test_output_rejects_foreign_quest_session_operation_and_stale_cursor(root_reader):
    reader, runtime, request, run = root_reader
    operation_ref, _ = _add_stage_call(runtime, run, "primary")
    quest_ref = request.accepted_question.quest_ref
    for quest, session, operation in (
        ("another-quest", run.root_session_ref, operation_ref),
        (quest_ref, "another-root", operation_ref),
        (quest_ref, run.root_session_ref, "another-operation"),
    ):
        with pytest.raises(ValueError, match="root_session_(not_found|operation_not_found)"):
            reader.query_output(quest, session, operation_ref=operation)
    with pytest.raises(ValueError, match="root_session_output_cursor_stale"):
        reader.query_output(quest_ref, run.root_session_ref, operation_ref=operation_ref, after=10**9)


def test_tampered_signed_stage_invocation_cannot_supply_output(root_reader):
    reader, runtime, request, run = root_reader
    operation_ref, directory = _add_stage_call(runtime, run, "primary")
    (directory / "invocation.json").write_text('{"not":"signed"}', encoding="utf-8")
    with pytest.raises(ValueError, match="root_session_output_unavailable"):
        reader.query_output(request.accepted_question.quest_ref, run.root_session_ref,
                            operation_ref=operation_ref)


def test_targets_keep_independent_roots_and_waiting_bundle_is_not_active(root_reader, monkeypatch):
    reader, runtime, request, run = root_reader
    _add_stage_call(runtime, run, "primary")
    roots = []
    for index in (1, 2):
        roots.append({"root_session_ref": f"target-session-{index}", "run_ref": f"target-run-{index}",
            "target_ref": f"target-{index}", "target_key": f"T{index}", "target_ordinal": index - 1, "stage": "bundle",
            "cycle_ref": request.cycle_ref, "question_ref": request.accepted_question.question_ref,
            "epoch": request.epoch, "owner_session_ref": run.root_session_ref,
            "status": "running" if index == 1 else "admitted", "lifecycle_status": "active",
            "created_at": 10.0 + index, "updated_at": 20.0 + index})
    monkeypatch.setattr(reader, "_target_rows", lambda *args: roots)
    monkeypatch.setattr(reader, "_target_operations", lambda connection, row: (
        [{"operation_ref": "target-call-1", "status": "running", "generation": 1,
          "created_at": 15.0, "completed_at": None}] if row["target_ref"] == "target-1" else []))
    store = SimpleNamespace(_operation_bindings={"target-call-1": ("a" * 64, "codex")},
                            _source_path=lambda _hash: Path("/fixture/stdout.jsonl"))
    monkeypatch.setattr(reader, "_target_store", lambda *args: (store, None))
    monkeypatch.setattr(observations, "_process_is_bound", lambda *args: True)
    listing = reader.query(request.accepted_question.quest_ref)
    targets = {s["target_ref"]: s for s in listing["sessions"] if s["kind"] == "target"}
    assert listing["limited"] is False
    assert listing["active_session_refs"] == ["target-session-1"]
    assert targets["target-1"]["status"] == "executing"
    assert targets["target-2"]["status"] == "pending"
    assert targets["target-1"]["owner_session_ref"] == run.root_session_ref
    assert targets["target-1"]["session_ref"] != targets["target-2"]["session_ref"]


def test_executing_requires_signed_pid_with_the_exact_operation_token(tmp_path, monkeypatch):
    root = tmp_path / "provider"
    directory = root / "provider-operations" / "operation" / "primary"
    directory.mkdir(parents=True)
    _, key = ensure_transport_key(root)
    invocation_hash = "a" * 64
    request_path = str((directory / "supervisor-request.json").resolve())
    marker = {"schema_ref": "meta-research/codex-provider-started/v2",
        "invocation_hash": invocation_hash, "provider_operation_path": request_path,
        "provider_process_id": 12345, "provider_process_group": 12345}
    write_transport_envelope(directory / "provider-started.json", marker, key)
    monkeypatch.setattr(observations.os, "getpgid", lambda _pid: 12345)
    original_open = Path.open
    from io import BytesIO
    def read_environment(path, *args, **kwargs):
        if str(path) == "/proc/12345/environ":
            return BytesIO(f"META_RESEARCH_PROVIDER_OPERATION={request_path}\0".encode())
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", read_environment)
    assert observations._process_is_bound(directory, invocation_hash)
    assert not observations._process_is_bound(directory, "b" * 64)
    monkeypatch.setattr(observations.os, "getpgid", lambda _pid: 98765)
    assert not observations._process_is_bound(directory, invocation_hash)


def test_real_target_admission_is_visible_before_launch_binds_its_root(tmp_path, monkeypatch):
    from test_target_root_finalizer import _admit_independent_target_root

    runtime = _bundle_runtime(tmp_path / "target-root-admission-observation")
    try:
        target, _candidate, _plan, admission, _handle = _admit_independent_target_root(runtime)
        reader = RootSessionObservations(runtime)
        monkeypatch.setattr(reader, "_provider_reader", lambda: _NoProviderRoots())
        with runtime._database.read() as connection:
            launch = connection.execute(text(
                "SELECT quest_ref, root_session_ref FROM ar_target_launches WHERE target_ref=:target_ref"
            ), {"target_ref": target.target_ref}).mappings().one()
        assert launch["root_session_ref"] is None
        listing = reader.query(launch["quest_ref"])
        assert listing["limited"] is False
        session = next(s for s in listing["sessions"] if s["target_ref"] == target.target_ref)
        assert session["session_ref"] == admission.run.root_session_ref
        assert session["short_title"] == f"Target T{target.ordinal + 1}"
        assert session["status"] == "pending"
        assert session["is_executing"] is False
        assert session["operations"] == []
    finally:
        runtime.close()


@pytest.mark.parametrize("transport_caught_up", [True, False])
def test_target_output_uses_mapped_byte_coordinates_and_finishes_only_after_mapping(
    root_reader, monkeypatch, tmp_path, transport_caught_up,
):
    reader, runtime, request, _stage_run = root_reader
    source = tmp_path / "target-stdout.jsonl"
    source.write_bytes(b"x" * 400)
    row = {"run_ref": "target-run", "root_session_ref": "target-root"}
    operation = {"operation_ref": "target-operation", "status": "executed"}
    mapped = "根消息\n"
    byte_count = len(mapped.encode())
    page = {"operation_ref": "target-operation", "stream_ref": "target-stream",
        "text": mapped, "offset": 0, "next_offset": byte_count,
        "mapped_bytes": byte_count, "source_bytes": 400,
        "source_caught_up": transport_caught_up, "has_more": not transport_caught_up,
        "root_native_session_ref": "native-target"}
    store = SimpleNamespace(
        _operation_bindings={"target-operation": ("a" * 64, "codex")},
        _source_path=lambda _: source,
        query=lambda *args, **kwargs: SimpleNamespace(as_dict=lambda: dict(page)),
    )
    monkeypatch.setattr(reader, "_stage_rows", lambda *args: [])
    monkeypatch.setattr(reader, "_target_rows", lambda *args: [row])
    monkeypatch.setattr(reader, "_target_operations", lambda *args: [operation])
    monkeypatch.setattr(reader, "_target_store", lambda *args: (store, SimpleNamespace(
        run_ref="target-run", native_session_ref="native-target")))
    monkeypatch.setattr(runtime.harnesses._owner, "latest_operation", lambda _: SimpleNamespace(
        operation_ref="target-operation"))
    monkeypatch.setattr(reader, "_target_operation", lambda *args: {"status": "completed"})
    result = reader.query_output(request.accepted_question.quest_ref, "target-root",
                                 operation_ref="target-operation")
    assert result["source_bytes"] == result["next_offset"] == byte_count
    assert result["has_more"] is False and result["source_caught_up"] is True
    assert result["transport_source_bytes"] == 400
    assert result["native_session_ref"] == result["root_native_session_ref"] == "native-target"
    assert result["status"] == ("terminal" if transport_caught_up else "waiting")


def test_reasoning_checkpoint_preserves_review_before_reused_unit_resume(root_reader, monkeypatch):
    reader, runtime, request, _run = root_reader
    quest_ref = request.accepted_question.quest_ref
    row = {"run_ref": "reasoning-run", "root_session_ref": "reasoning-root", "stage": "reasoning",
        "current_attempt_ref": "reasoning-attempt", "current_fence_ref": "reasoning-fence",
        "cycle_ref": request.cycle_ref, "question_ref": request.accepted_question.question_ref,
        "epoch": request.epoch, "status": "running", "created_at": 1.0, "updated_at": 4.0}
    unit = {"unit_ref": "reasoning-review-unit", "operation_ref": "reasoning-operation",
        "attempt_ref": "reasoning-attempt", "fence_ref": "reasoning-fence", "generation": 1,
        "invocation_phase": "review", "unit_kind": "reasoning_review", "status": "active",
        "reasoning_checkpoint_ref": "accepted-checkpoint", "reasoning_checkpoint_recorded_at": 2.0,
        "started_at": 3.0, "completed_at": None}
    monkeypatch.setattr(reader, "_stage_rows", lambda connection, quest, session=None:
        [row] if quest == quest_ref and session in {None, row["root_session_ref"]} else [])
    monkeypatch.setattr(reader, "_stage_units", lambda connection, run, operation=None:
        [unit] if operation in {None, unit["unit_ref"]} else [])
    monkeypatch.setattr(reader, "_target_rows", lambda *args: [])
    monkeypatch.setattr(observations, "_process_is_bound", lambda *args: True)
    for phase in ("review", "autonomous-resume"):
        scope = reader._stage_scope(row, unit)
        _seed_spool(runtime.data_root.root, scope, (
            {"type": "thread.started", "thread_id": "native-reasoning-root"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": phase + " public"}},
        ), operation_name=phase)
    listing = reader.query(quest_ref)
    session = listing["sessions"][0]
    assert listing["limited"] is False
    assert listing["active_session_refs"] == ["reasoning-root"]
    assert [(o["operation_ref"], o["status"]) for o in session["operations"]] == [
        ("reasoning-review-unit~review", "completed"), ("reasoning-review-unit", "executing")]
    assert [o["created_at"] for o in session["operations"]] == [2.0, 3.0]
    previous = reader.query_output(quest_ref, "reasoning-root", operation_ref="reasoning-review-unit~review")
    current = reader.query_output(quest_ref, "reasoning-root", operation_ref="reasoning-review-unit")
    assert "review public" in previous["text"] and "autonomous-resume public" not in previous["text"]
    assert "autonomous-resume public" in current["text"]
    assert previous["status"] == "terminal" and current["status"] == "live"
    for quest, operation in (("another-quest", "reasoning-review-unit~review"),
                              (quest_ref, "another-unit~review")):
        with pytest.raises(ValueError, match="root_session_(not_found|operation_not_found)"):
            reader.query_output(quest, "reasoning-root", operation_ref=operation)


@pytest.fixture
def target_history_reader(tmp_path, monkeypatch):
    import json
    from dataclasses import replace
    from meta_research.bundle_protocol import projection_plain_value
    from meta_research.feed import DurableFeed
    from meta_research.owners.common import canonical_hash, canonical_json
    from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
    from meta_research.provider_supervisor import SUPERVISOR_EXIT_SCHEMA_V2, write_exit_receipt
    from meta_research.target_raw_output import TargetRawOutputStore
    from test_target_root_finalizer import _admit_independent_target_root
    from test_target_root_observations import _event

    runtime = _bundle_runtime(tmp_path / "target-history")
    try:
        target, candidate, plan, admission, old = _admit_independent_target_root(runtime)
        with runtime._database.read() as connection:
            launch = connection.execute(text("SELECT * FROM ar_target_launches WHERE target_ref=:ref"),
                                        {"ref": target.target_ref}).mappings().one()
        lifecycle = SQLiteTargetRootLifecycleAuthority(runtime._database, DurableFeed(runtime._database),
                                                       runtime.target_run_authorities.agent_runtime)
        lifecycle.activate(launch_ref=launch["launch_ref"], handle=old, candidate=candidate,
                           formal_plan=plan, idempotency_key="observe-history-activate")
        new = replace(old, root_session_ref="recovered-target-root", execution_attempt_ref="recovered-attempt",
                      execution_fence_ref="recovered-fence")
        value = projection_plain_value(new)
        with runtime._database.write() as connection:
            connection.execute(text("""INSERT INTO ar_target_root_handle_history
                (target_ref,ordinal,target_run_ref,root_session_ref,execution_attempt_ref,
                 execution_fence_ref,handle_json,handle_hash,recorded_at)
                VALUES (:target_ref,2,:target_run_ref,:root_session_ref,:execution_attempt_ref,
                        :execution_fence_ref,:document,:digest,20.0)"""),
                {**value, "document": canonical_json(value), "digest": canonical_hash(value)})
            connection.execute(text("""UPDATE ar_harness_runs SET root_session_ref=:root,
                attempt_ref=:attempt,fence_ref=:fence,attempt_generation=2,
                native_session_ref='native-new',status='running',profile_json=NULL,profile_hash=NULL
                WHERE run_ref=:run"""), {"root": new.root_session_ref, "attempt": new.execution_attempt_ref,
                "fence": new.execution_fence_ref, "run": old.target_run_ref})
        store = TargetRawOutputStore(tmp_path / "transport")
        _, key = ensure_transport_key(tmp_path / "transport")
        receipts = {}
        for index, (handle, status, native) in enumerate(((old, "executed", "native-old"),
                (old, "failed", "native-old"), (new, "running", "native-new")), 1):
            operation = f"target-history-call-{index}"
            scope = {"schema_ref": "meta-research/target-root-observation-scope/v1",
                "target_run_ref": handle.target_run_ref, "attempt_ref": handle.execution_attempt_ref,
                "attempt_generation": 1 if handle == old else 2, "root_session_ref": handle.root_session_ref,
                "fence_ref": handle.execution_fence_ref, "native_session_ref": None}
            event = _event(1, text=f"durable call {index}", scope=scope)
            event["target_root_observation"]["root_native_session_ref"] = native
            with runtime._database.write() as connection:
                connection.execute(text("""INSERT INTO ar_harness_provider_operations
                    (operation_ref,run_ref,generation,invocation_hash,status,outcome_code,created_at,completed_at)
                    VALUES (:operation,:run,:generation,:hash,:status,NULL,:created,:completed)"""),
                    {"operation": operation, "run": old.target_run_ref, "generation": index,
                     "hash": canonical_hash(operation), "status": status,
                     "created": float(index), "completed": None if status == "running" else float(index) + .5})
                connection.execute(text("""INSERT INTO ar_harness_evidence_events
                    (event_ref,operation_ref,sequence,summary_json,summary_hash,recorded_at)
                    VALUES (:event_ref,:operation,:sequence,:document,:hash,:recorded)"""),
                    {"event_ref": event["event_ref"], "run": old.target_run_ref, "operation": operation,
                     "sequence": event["sequence"], "document": canonical_json(event),
                     "hash": canonical_hash(event), "recorded": float(index)})
            digest = canonical_hash({"transport": operation})
            directory = tmp_path / "transport" / "provider-operations" / digest[:2] / digest
            directory.mkdir(parents=True)
            (directory / "prompt.txt").write_text(operation)
            (directory / "output-schema.json").write_text('{"type":"object"}')
            (directory / "stdout.jsonl").write_text(json.dumps({"type": "thread.started", "thread_id": native}) + "\n" +
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": f"raw call {index}"}}) + "\n")
            write_exit_receipt(directory / "supervisor-exit.json", key=key, invocation_hash=digest,
                prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
                stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
                returncode=124, input_bytes=0, termination_reason="timeout", schema_ref=SUPERVISOR_EXIT_SCHEMA_V2)
            receipts[operation] = {"schema_ref": "meta-research/harness-provider-transport-receipt/v1",
                "spool_ref": "provider-spool:" + digest, "transport_invocation_hash": digest,
                "supervisor_receipt_hash": canonical_hash(json.loads((directory / "supervisor-exit.json").read_text())),
                "termination_reason": "timeout", "provider_returncode": 124}
            store.bind_operation(operation, digest, family="codex")
        reader = RootSessionObservations(runtime)
        monkeypatch.setattr(reader, "_provider_reader", lambda: _NoProviderRoots())
        monkeypatch.setattr(runtime.harnesses, "_target_raw_output_store", store)
        monkeypatch.setattr(runtime.harnesses._owner, "query_target_run_by_ref", lambda _: SimpleNamespace(
            run_ref=new.target_run_ref, root_session_ref=new.root_session_ref, native_session_ref="native-new"))
        monkeypatch.setattr(runtime.harnesses._owner, "latest_operation", lambda _: SimpleNamespace(operation_ref="target-history-call-3"))
        monkeypatch.setattr(observations, "_process_is_bound", lambda *args: True)
        yield reader, runtime, launch["quest_ref"], old, new, store, receipts
    finally:
        runtime.close()


def test_target_recovery_separates_roots_but_same_root_resume_keeps_calls(target_history_reader):
    reader, _runtime, quest, old, new, _store, _receipts = target_history_reader
    listing = reader.query(quest)
    assert listing["limited"] is False
    roots = {s["session_ref"]: s for s in listing["sessions"] if s["kind"] == "target"}
    assert set(roots) == {old.root_session_ref, new.root_session_ref}
    assert [o["operation_ref"] for o in roots[old.root_session_ref]["operations"]] == [
        "target-history-call-1", "target-history-call-2"]
    assert [o["operation_ref"] for o in roots[new.root_session_ref]["operations"]] == ["target-history-call-3"]
    assert not roots[old.root_session_ref]["is_current"] and not roots[old.root_session_ref]["is_executing"]
    assert roots[old.root_session_ref]["activity_label"] == "已由新会话接续"
    assert listing["active_session_refs"] == [new.root_session_ref]
    for index, handle in ((1, old), (2, old), (3, new)):
        page = reader.query_output(quest, handle.root_session_ref, operation_ref=f"target-history-call-{index}")
        assert f"raw call {index}" in page["text"]
        assert page["native_session_ref"] == ("native-old" if handle == old else "native-new")
    for session, operation in ((old.root_session_ref, "target-history-call-3"),
                               (new.root_session_ref, "target-history-call-1")):
        with pytest.raises(ValueError, match="root_session_operation_not_found"):
            reader.query_output(quest, session, operation_ref=operation)
    with pytest.raises(ValueError, match="root_session_not_found"):
        reader.query_output("foreign-quest", old.root_session_ref, operation_ref="target-history-call-1")


@pytest.mark.parametrize("damage", ["missing_scope", "bad_scope_hash", "wrong_fence", "missing_transport", "bad_handle_hash"])
def test_target_uncertain_history_is_limited_and_never_attached_to_current(target_history_reader, damage):
    import json
    from meta_research.owners.common import canonical_hash, canonical_json
    reader, runtime, quest, old, new, store, _receipts = target_history_reader
    with runtime._database.write() as connection:
        if damage == "missing_scope":
            connection.execute(text("DELETE FROM ar_harness_evidence_events WHERE operation_ref='target-history-call-1'"))
        elif damage == "bad_scope_hash":
            connection.execute(text("UPDATE ar_harness_evidence_events SET summary_hash=:hash WHERE operation_ref='target-history-call-1'"),
                               {"hash": "0" * 64})
        elif damage == "wrong_fence":
            event = json.loads(connection.execute(text("SELECT summary_json FROM ar_harness_evidence_events WHERE operation_ref='target-history-call-1'")).scalar_one())
            event["target_root_observation"]["scope"]["fence_ref"] = "another-fence"
            event["target_run_scope"]["fence_ref"] = "another-fence"
            connection.execute(text("UPDATE ar_harness_evidence_events SET summary_json=:json, summary_hash=:hash WHERE operation_ref='target-history-call-1'"),
                               {"json": canonical_json(event), "hash": canonical_hash(event)})
        elif damage == "bad_handle_hash":
            connection.execute(text("UPDATE ar_target_root_handle_history SET handle_hash=:hash WHERE root_session_ref=:root"),
                               {"hash": "0" * 64, "root": old.root_session_ref})
        else:
            del store._operation_bindings["target-history-call-1"]
    listing = reader.query(quest)
    assert listing["limited"] is True
    roots = {s["session_ref"]: s for s in listing["sessions"] if s["kind"] == "target"}
    assert set(roots) == {old.root_session_ref, new.root_session_ref}
    assert roots[old.root_session_ref]["limited"] is True
    assert [o["operation_ref"] for o in roots[new.root_session_ref]["operations"]] == ["target-history-call-3"]
    with pytest.raises(ValueError, match="root_session_(operation_not_found|output_unavailable)"):
        reader.query_output(quest, old.root_session_ref, operation_ref="target-history-call-1")


def test_failed_target_history_recovers_only_the_hashed_signed_receipt(target_history_reader):
    from meta_research.owners.common import canonical_hash, canonical_json
    reader, runtime, quest, old, new, store, receipts = target_history_reader
    receipt = receipts["target-history-call-2"]
    with runtime._database.write() as connection:
        workspace_ref = connection.execute(text("SELECT workspace_ref FROM ar_target_run_workspaces WHERE target_ref=:ref"),
                                           {"ref": old.target_ref}).scalar_one()
        values = {"transition_ref": "observe-recovery", "recovery_ref": "observe-recovery-ref", "target_ref": old.target_ref,
            "ordinal": 1, "blocker_ref": "observe-blocker", "old_execution_attempt_ref": old.execution_attempt_ref,
            "new_execution_attempt_ref": new.execution_attempt_ref, "retired_workspace_ref": workspace_ref,
            "failed_provider_operation_ref": "target-history-call-2", "failure_code": "provider_timeout",
            "blocker_json": "{}", "blocker_hash": canonical_hash({}),
            "transport_receipt_json": canonical_json(receipt), "transport_receipt_hash": canonical_hash(receipt),
            "provider_evidence_ref": "observe-provider-evidence", "provider_evidence_json": "{}",
            "provider_evidence_json_hash": canonical_hash({}), "provider_evidence_hash": canonical_hash({}),
            "successor_reservation_json": "{}", "successor_reservation_hash": canonical_hash({}),
            "recovery_evidence_refs_json": "[]", "recovery_evidence_refs_hash": canonical_hash([]),
            "generic_binding_ref": None, "generic_binding_receipt_ref": None, "generic_binding_receipt_hash": None,
            "idempotency_key": "observe-recovery-key", "request_hash": canonical_hash({}), "recovered_at": 20.0}
        connection.execute(text("INSERT INTO ar_target_root_provider_recoveries (" + ",".join(values) +
            ") VALUES (" + ",".join(":" + key for key in values) + ")"), values)
    del store._operation_bindings["target-history-call-2"]
    page = reader.query_output(quest, old.root_session_ref, operation_ref="target-history-call-2")
    assert "raw call 2" in page["text"] and page["native_session_ref"] == "native-old"
    del store._operation_bindings["target-history-call-2"]
    with runtime._database.write() as connection:
        connection.execute(text("UPDATE ar_target_root_provider_recoveries SET transport_receipt_hash=:hash"), {"hash": "0" * 64})
    with pytest.raises(ValueError, match="root_session_output_unavailable"):
        reader.query_output(quest, old.root_session_ref, operation_ref="target-history-call-2")
