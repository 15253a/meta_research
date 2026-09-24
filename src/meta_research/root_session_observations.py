"""Read-only root Session index and exact, operation-bound output pages.

The index is an observation of existing Owner rows, never execution authority.
Provider calls stay within their canonical root Session; child Sessions are not
indexed. All spool reads delegate to the existing signed transport readers.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from sqlalchemy import text

from meta_research.bundle_protocol import TargetWorkHandle
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.owners.agent_runtime_harness import _validated_target_root_event
from meta_research.owners.target_run_runtime import _decode_stored_record
from meta_research.provider_supervisor import (
    PROVIDER_OPERATION_ENV, ProviderSupervisorError,
    read_transport_envelope, read_transport_key_for_operation,
)
from meta_research.stage_root_observations import (
    StageRootObservationError, StageRootObservationReader,
)
from meta_research.target_raw_output import TargetRawOutputUnavailable


_STAGES = {"idea": "Idea", "plan": "Plan", "bundle": "Bundle", "reasoning": "Reasoning"}
_MAX_ROOTS = 512
_MAX_OPERATIONS = 2048


def _validate_ref(value, code):
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(code)


def _state(value):
    if value in {"completed", "succeeded", "executed", "closed", "terminal"}:
        return "completed"
    if value in {"failed", "interrupted", "revoked", "cancelled", "terminated"}:
        return "failed"
    if value in {"paused", "suspended", "suspended_fenced"}:
        return "paused"
    return "waiting"


def _operation_status(value, executing):
    return "executing" if executing else _state(value)


def _process_is_bound(directory: Path, invocation_hash: str) -> bool:
    """Verify one sealed PID and its exact operation token without scanning /proc."""
    if os.name != "posix" or (directory / "supervisor-exit.json").exists():
        return False
    marker_path = directory / "provider-started.json"
    if not marker_path.is_file() or marker_path.is_symlink():
        return False
    try:
        if marker_path.stat().st_size > 128 * 1024:
            return False
        _, key = read_transport_key_for_operation(directory)
        marker = read_transport_envelope(marker_path, key)
        pid = marker.get("provider_process_id")
        request_path = str((directory / "supervisor-request.json").resolve())
        if (marker.get("schema_ref") not in {
                "meta-research/codex-provider-started/v2",
                "meta-research/provider-started/v2"}
                or marker.get("invocation_hash") != invocation_hash
                or marker.get("provider_operation_path") != request_path
                or type(pid) is not int or pid <= 1
                or marker.get("provider_process_group") != pid
                or os.getpgid(pid) != pid):
            return False
        with Path(f"/proc/{pid}/environ").open("rb") as stream:
            environment = stream.read(1024 * 1024).split(b"\0")
        return f"{PROVIDER_OPERATION_ENV}={request_path}".encode() in environment
    except (OSError, ValueError, ProviderSupervisorError):
        return False


class RootSessionObservations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.database = runtime._database
        self.data_root = runtime.data_root.root

    def _provider_reader(self):
        from meta_research.root_provider_observations import ProviderRootObservations
        return ProviderRootObservations(self.runtime)

    def query(self, quest_ref: str) -> dict:
        _validate_ref(quest_ref, "root_session_quest_not_found")
        with self.database.read_snapshot() as connection:
            if connection.execute(text(
                "SELECT 1 FROM rg_quests WHERE quest_ref=:quest_ref"
            ), {"quest_ref": quest_ref}).first() is None:
                raise ValueError("root_session_quest_not_found")
            row = connection.execute(text(
                "SELECT quest_ref, cycle_ref, question_ref, stage, epoch, status "
                "FROM ae_foreground_heads WHERE quest_ref=:quest_ref"
            ), {"quest_ref": quest_ref}).mappings().first()
            foreground = None if row is None else dict(row)
            sessions, reasons = [], []
            for query in (self._stage_sessions, self._target_sessions):
                try:
                    sessions.extend(query(connection, quest_ref, foreground))
                except Exception:
                    # No exception strings: database/transport errors may include paths.
                    reasons.append({"code": "root_session_index_part_unavailable"})
        try:
            providers = self._provider_reader().query(quest_ref)
            sessions.extend(providers["sessions"])
            reasons.extend(providers.get("reasons", []))
            if providers.get("limited") and not providers.get("reasons"):
                reasons.append({"code": "root_provider_sessions_limited"})
        except Exception:
            reasons.append({"code": "root_provider_sessions_unavailable"})
        sessions.sort(key=lambda item: (item["created_at"], item["session_ref"]), reverse=True)
        if len(sessions) > _MAX_ROOTS:
            sessions = sessions[:_MAX_ROOTS]
            reasons.append({"code": "root_session_index_limited"})
        for session in sessions:
            if session.get("limited"):
                reasons.append({"code": "root_session_operations_limited"})
        return {
            "schema_ref": "meta-research/root-sessions/v1", "quest_ref": quest_ref,
            "observed_at": time.time(), "foreground": foreground, "sessions": sessions,
            "active_session_refs": [s["session_ref"] for s in sessions if s["is_executing"]],
            "limited": bool(reasons), "reasons": reasons,
        }

    def _base_session(self, row, *, kind, title, stage, foreground):
        is_current = bool(foreground and row.get("cycle_ref") == foreground["cycle_ref"]
                          and row.get("question_ref") == foreground["question_ref"]
                          and stage == foreground["stage"]
                          and row.get("epoch") == foreground["epoch"])
        return {
            "session_ref": row["root_session_ref"], "root_session_ref": row["root_session_ref"],
            "kind": kind, "title": title, "stage": stage, "related_stages": [stage],
            "scope_label": "本轮研究" if kind == "stage" else "Target 执行",
            "owner_session_ref": row.get("owner_session_ref"),
            "status": _state(row["status"]), "is_executing": False, "is_current": is_current,
            "run_ref": row["run_ref"], "target_ref": row.get("target_ref"),
            "cycle_ref": row.get("cycle_ref"), "question_ref": row.get("question_ref"),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "operations": [], "limited": False,
        }

    def _stage_rows(self, connection, quest_ref, session_ref=None):
        return connection.execute(text("""
            SELECT r.*, q.quest_ref, q.question_ref, s.created_at AS session_created_at
            FROM ar_stage_runs r JOIN ae_stage_run_requests q ON q.request_ref=r.request_ref
              AND q.cycle_ref=r.cycle_ref AND q.stage=r.stage AND q.epoch=r.epoch
            JOIN ar_stage_sessions s ON s.session_ref=r.root_session_ref AND s.run_ref=r.run_ref
            WHERE q.quest_ref=:quest_ref AND (:session_ref IS NULL OR s.session_ref=:session_ref)
            ORDER BY r.created_at DESC, r.run_ref DESC LIMIT :maximum
        """), {"quest_ref": quest_ref, "session_ref": session_ref, "maximum": _MAX_ROOTS + 1}).mappings().all()

    def _stage_units(self, connection, row, operation_ref=None):
        return connection.execute(text("""
            SELECT u.*, a.generation, a.reasoning_checkpoint_ref,
                   a.reasoning_checkpoint_recorded_at, i.phase AS invocation_phase
            FROM ar_provider_units u JOIN ar_stage_attempts a ON a.attempt_ref=u.attempt_ref
              AND a.run_ref=u.run_ref AND a.fence_ref=u.fence_ref AND a.root_session_ref=:root_session_ref
            LEFT JOIN ar_stage_provider_invocations i ON i.invocation_ref=u.unit_ref
              AND i.run_ref=u.run_ref AND i.attempt_ref=u.attempt_ref
              AND i.fence_ref=u.fence_ref AND i.operation_ref=u.operation_ref
            WHERE u.run_ref=:run_ref AND (:operation_ref IS NULL OR u.unit_ref=:operation_ref)
              AND u.unit_kind IN (:primary_kind,:review_kind)
            ORDER BY u.started_at DESC, u.unit_ref DESC LIMIT :maximum
        """), {"root_session_ref": row["root_session_ref"], "run_ref": row["run_ref"],
               "operation_ref": operation_ref, "primary_kind": row["stage"] + "_primary",
               "review_kind": row["stage"] + "_review", "maximum": _MAX_OPERATIONS + 1}).mappings().all()

    def _stage_scope(self, row, unit):
        phase = unit["invocation_phase"]
        if (row["stage"] == "reasoning" and unit["unit_kind"] == "reasoning_review"
                and unit["reasoning_checkpoint_ref"] and unit["reasoning_checkpoint_recorded_at"]
                and unit["started_at"] >= unit["reasoning_checkpoint_recorded_at"]):
            phase = "autonomous-resume"
        elif phase is None and not (row["stage"] == "bundle" and unit["unit_kind"] == "bundle_review"):
            phase = unit["unit_kind"].rsplit("_", 1)[-1]
        return {
            "run_ref": row["run_ref"], "run_kind": row["stage"] + "_stage",
            "attempt_ref": unit["attempt_ref"], "attempt_generation": unit["generation"],
            "root_session_ref": row["root_session_ref"], "fence_ref": unit["fence_ref"],
            "status": "completed" if unit["status"] in {"completed", "revoked"} else "running",
            "unit_ref": unit["unit_ref"], "operation_ref": unit["operation_ref"],
            "unit_kind": unit["unit_kind"], "operation_name": phase,
        }

    def _stage_reader(self, row, unit, *, historical_review=False):
        scope = self._stage_scope(row, unit)
        if historical_review:
            if scope["operation_name"] != "autonomous-resume":
                raise ValueError("root_session_operation_not_found")
            # The accepted checkpoint separates two physical turns that reuse
            # the same Owner unit. Its earlier signed review remains history.
            scope = {**scope, "operation_name": "review", "status": "completed"}
        return StageRootObservationReader(self.data_root, scope_lookup=lambda _ref: scope), scope

    def _stage_operation(self, row, unit, *, historical_review=False):
        reader, scope = self._stage_reader(row, unit, historical_review=historical_review)
        phase, executing = scope["operation_name"], False
        current = (unit["attempt_ref"] == row["current_attempt_ref"]
                   and unit["fence_ref"] == row["current_fence_ref"])
        if unit["status"] == "active" and current and not historical_review:
            try:
                resolved = reader._resolve_operation(scope)
                if resolved:
                    directory, invocation_hash, phase, _native = resolved
                    executing = _process_is_bound(directory, invocation_hash)
            except (StageRootObservationError, OSError):
                pass
        return {
            "operation_ref": unit["unit_ref"] + ("~review" if historical_review else ""), "phase": phase,
            "label": {"primary": "研究", "review": "复核", "autonomous-resume": "继续研究"}.get(phase, "接续调用"),
            "status": "completed" if historical_review else _operation_status(unit["status"], executing),
            "created_at": unit["reasoning_checkpoint_recorded_at"] if historical_review else unit["started_at"],
            "updated_at": unit["reasoning_checkpoint_recorded_at"] if historical_review else unit["completed_at"] or unit["started_at"],
        }

    def _stage_sessions(self, connection, quest_ref, foreground):
        sessions = []
        for row in self._stage_rows(connection, quest_ref):
            row = dict(row)
            session = self._base_session(row, kind="stage", title=_STAGES[row["stage"]], stage=row["stage"], foreground=foreground)
            units = self._stage_units(connection, row)
            session["limited"] = len(units) > _MAX_OPERATIONS
            for unit in reversed(units[:_MAX_OPERATIONS]):
                if self._stage_scope(row, unit)["operation_name"] == "autonomous-resume":
                    historical_reader, historical_scope = self._stage_reader(row, unit, historical_review=True)
                    try:
                        if historical_reader._resolve_operation(historical_scope) is not None:
                            session["operations"].append(self._stage_operation(row, unit, historical_review=True))
                    except (StageRootObservationError, OSError):
                        session["limited"] = True
                session["operations"].append(self._stage_operation(row, unit))
            session["is_executing"] = any(o["status"] == "executing" for o in session["operations"])
            if session["is_executing"]:
                session["status"] = "executing"
            elif not units and session["status"] == "waiting":
                session["status"] = "pending"
            sessions.append(session)
        return sessions

    def _target_rows(self, connection, quest_ref, session_ref=None):
        current_rows = connection.execute(text("""
            SELECT h.*, l.target_ref, l.stage_request_ref, t.target_key, t.ordinal AS target_ordinal,
                   q.cycle_ref, q.question_ref, q.epoch, b.root_session_ref AS owner_session_ref,
                   life.status AS lifecycle_status
            FROM ar_harness_runs h JOIN ar_target_harness_admissions a ON a.target_run_ref=h.run_ref
            JOIN ar_target_launches l ON l.target_ref=a.target_ref AND l.target_run_ref=h.run_ref
            JOIN rg_targets t ON t.target_ref=l.target_ref AND t.graph_ref=l.graph_ref
            JOIN ae_stage_run_requests q ON q.request_ref=l.stage_request_ref AND q.quest_ref=l.quest_ref
            LEFT JOIN ar_stage_runs b ON b.request_ref=q.request_ref
            LEFT JOIN ar_target_root_lifecycles life ON life.target_run_ref=h.run_ref
              AND life.target_ref=l.target_ref
            WHERE l.quest_ref=:quest_ref
            ORDER BY h.created_at DESC, h.run_ref DESC LIMIT :maximum
        """), {"quest_ref": quest_ref, "maximum": _MAX_ROOTS + 1}).mappings().all()
        roots = []
        for current_row in current_rows:
            current = dict(current_row)
            current["_current_root_session_ref"] = current["root_session_ref"]
            current["_historical"] = False
            history = connection.execute(text("""
                SELECT * FROM ar_target_root_handle_history
                WHERE target_ref=:target_ref AND target_run_ref=:run_ref
                ORDER BY ordinal DESC LIMIT :maximum
            """), {**current, "maximum": _MAX_ROOTS + 1}).mappings().all()
            current["_identity_limited"] = len(history) > _MAX_ROOTS
            rows = {current["root_session_ref"]: current}
            for stored in history[:_MAX_ROOTS]:
                try:
                    handle = _decode_stored_record(stored["handle_json"], stored["handle_hash"], TargetWorkHandle)
                    if (handle.target_ref != current["target_ref"]
                            or handle.target_run_ref != current["run_ref"]
                            or handle.root_session_ref != stored["root_session_ref"]
                            or handle.execution_attempt_ref != stored["execution_attempt_ref"]
                            or handle.execution_fence_ref != stored["execution_fence_ref"]):
                        raise ValueError("root_session_target_binding_invalid")
                    if handle.root_session_ref == current["root_session_ref"]:
                        if (handle.execution_attempt_ref != current["attempt_ref"]
                                or handle.execution_fence_ref != current["fence_ref"]
                                or stored["ordinal"] != current["attempt_generation"]):
                            raise ValueError("root_session_target_binding_invalid")
                        continue
                    if stored["ordinal"] >= current["attempt_generation"]:
                        raise ValueError("root_session_target_binding_invalid")
                    rows[handle.root_session_ref] = {
                        **current, "root_session_ref": handle.root_session_ref,
                        "attempt_ref": handle.execution_attempt_ref,
                        "fence_ref": handle.execution_fence_ref,
                        "attempt_generation": stored["ordinal"], "native_session_ref": None,
                        "created_at": stored["recorded_at"], "updated_at": stored["recorded_at"],
                        "status": "interrupted", "lifecycle_status": None, "_historical": True,
                    }
                except (TypeError, ValueError, KeyError):
                    current["_identity_limited"] = True
                    # Preserve an indexed but unverified historical session as
                    # unavailable; its invalid handle never grants output access.
                    if stored["root_session_ref"] == current["root_session_ref"]:
                        current["_binding_invalid"] = True
                    else:
                        rows[stored["root_session_ref"]] = {
                            **current, "root_session_ref": stored["root_session_ref"],
                            "attempt_ref": stored["execution_attempt_ref"],
                            "fence_ref": stored["execution_fence_ref"],
                            "attempt_generation": stored["ordinal"], "native_session_ref": None,
                            "created_at": stored["recorded_at"], "updated_at": stored["recorded_at"],
                            "status": "interrupted", "lifecycle_status": None,
                            "_historical": True, "_binding_invalid": True,
                        }
            known_roots = set(rows)
            for row in rows.values():
                row["_known_roots"] = known_roots
                row["_identity_limited"] = current["_identity_limited"]
                if session_ref is None or row["root_session_ref"] == session_ref:
                    roots.append(row)
        return roots

    def _target_operation_identity(self, connection, operation):
        # Read one representative for each distinct durable identity. Repeated
        # stdout events do not require loading the entire research transcript.
        events = connection.execute(text("""
            SELECT event_ref, sequence, summary_json, summary_hash
            FROM ar_harness_evidence_events
            WHERE operation_ref=:operation_ref
              AND json_type(summary_json, '$.target_root_observation') IS NOT NULL
            GROUP BY json_extract(summary_json, '$.target_root_observation.scope'),
                     json_extract(summary_json, '$.target_root_observation.root_native_session_ref')
            LIMIT 5
        """), operation).mappings().all()
        if not events:
            return None
        if len(events) > 4:
            raise ValueError("root_session_target_binding_invalid")
        identities = []
        for stored in events:
            event = json.loads(stored["summary_json"])
            observation = event["target_root_observation"]
            scope = observation["scope"]
            native = observation["root_native_session_ref"]
            verified = _validated_target_root_event(event,
                expected_scope={**scope, "native_session_ref": native},
                expected_native_session_ref=native)
            if (verified[:4] != (stored["event_ref"], stored["sequence"],
                    stored["summary_json"], stored["summary_hash"])
                    or scope["target_run_ref"] != operation["run_ref"]
                    or type(scope["attempt_generation"]) is not int
                    or scope["attempt_generation"] < 1):
                raise ValueError("root_session_target_binding_invalid")
            identities.append({field: scope[field] for field in (
                "target_run_ref", "attempt_ref", "attempt_generation", "root_session_ref", "fence_ref")}
                | {"native_session_ref": native})
        if any(identity != identities[0] for identity in identities[1:]):
            raise ValueError("root_session_target_binding_invalid")
        return identities[0]

    def _target_operations(self, connection, row, operation_ref=None):
        if row.get("_binding_invalid"):
            row["_identity_limited"] = True
            return []
        operations = connection.execute(text("""
            SELECT operation_ref, run_ref, generation, status, outcome_code, created_at, completed_at
            FROM ar_harness_provider_operations WHERE run_ref=:run_ref
              AND (:operation_ref IS NULL OR operation_ref=:operation_ref)
            ORDER BY generation DESC LIMIT :maximum
        """), {"run_ref": row["run_ref"], "operation_ref": operation_ref,
               "maximum": _MAX_OPERATIONS + 1}).mappings().all()
        matched = []
        row["_identity_limited"] = row.get("_identity_limited", False) or len(operations) > _MAX_OPERATIONS
        for operation in operations[:_MAX_OPERATIONS]:
            try:
                identity = self._target_operation_identity(connection, operation)
                if identity is None:
                    # Generation one has no predecessor root to confuse with
                    # this one. After recovery a missing scope is ambiguous.
                    if row.get("_historical") or row["attempt_generation"] != 1:
                        row["_identity_limited"] = True
                        continue
                elif identity["root_session_ref"] != row["root_session_ref"]:
                    if identity["root_session_ref"] not in row["_known_roots"]:
                        row["_identity_limited"] = True
                    continue
                elif any(identity[field] != row[column] for field, column in (
                    ("target_run_ref", "run_ref"), ("attempt_ref", "attempt_ref"),
                    ("attempt_generation", "attempt_generation"), ("fence_ref", "fence_ref"))):
                    raise ValueError("root_session_target_binding_invalid")
                matched.append({**operation, "_native_session_ref": None if identity is None else identity["native_session_ref"]})
            except Exception:
                row["_identity_limited"] = True
        return matched

    def _target_store(self, row, operation):
        harness = self.runtime.harnesses
        run = harness._owner.query_target_run_by_ref(row["run_ref"])
        if run is None or run.root_session_ref != row.get("_current_root_session_ref", row["root_session_ref"]):
            raise ValueError("root_session_target_binding_invalid")
        store = harness._target_raw_output_store
        if store is None:
            raise ValueError("root_session_output_unavailable")
        if operation["operation_ref"] not in store._operation_bindings:
            try:
                # Only an in-memory read binding is recovered here.
                harness._recover_target_raw_output_binding(run_ref=run.run_ref, operation_ref=operation["operation_ref"])
            except Exception:
                with self.database.read() as connection:
                    receipts = connection.execute(text("""
                        SELECT transport_receipt_json, transport_receipt_hash
                        FROM ar_target_root_provider_recoveries
                        WHERE target_ref=:target_ref AND old_execution_attempt_ref=:attempt_ref
                          AND failed_provider_operation_ref=:operation_ref
                    """), {"target_ref": row["target_ref"], "attempt_ref": row["attempt_ref"],
                           "operation_ref": operation["operation_ref"]}).mappings().all()
                if len(receipts) != 1 or operation["status"] != "failed":
                    raise ValueError("root_session_output_unavailable")
                stored = receipts[0]
                receipt = json.loads(stored["transport_receipt_json"])
                if (canonical_json(receipt) != stored["transport_receipt_json"]
                        or canonical_hash(receipt) != stored["transport_receipt_hash"]):
                    raise ValueError("root_session_output_unavailable")
                store.bind_verified_transport_receipt(operation["operation_ref"], receipt)
        return store, run

    def _target_operation(self, row, operation):
        executing = False
        if operation["status"] == "running" and row["status"] == "running" and not row.get("_historical"):
            try:
                store, _run = self._target_store(row, operation)
                invocation_hash, _family = store._operation_bindings[operation["operation_ref"]]
                executing = _process_is_bound(store._source_path(invocation_hash).parent, invocation_hash)
            except Exception:
                pass
        return {
            "operation_ref": operation["operation_ref"], "phase": "target-turn",
            "label": f"执行 {operation['generation']}",
            "status": _operation_status(operation["status"], executing),
            "created_at": operation["created_at"],
            "updated_at": operation["completed_at"] or operation["created_at"],
        }

    def _target_sessions(self, connection, quest_ref, foreground):
        sessions = []
        for row in self._target_rows(connection, quest_ref):
            row = dict(row)
            short_title = f"Target T{int(row['target_ordinal']) + 1}"
            session = self._base_session(row, kind="target", title=f"{short_title} · {row['target_key']}", stage="bundle", foreground=foreground)
            session["short_title"] = short_title
            session["target_key"] = row["target_key"]
            operations = self._target_operations(connection, row)
            session["limited"] = row.get("_identity_limited", False) or len(operations) > _MAX_OPERATIONS
            if row.get("_historical"):
                for operation in operations:
                    try:
                        self._target_store(row, operation)
                    except Exception:
                        session["limited"] = True
            session["operations"] = [self._target_operation(row, o) for o in reversed(operations[:_MAX_OPERATIONS])]
            session["is_executing"] = any(o["status"] == "executing" for o in session["operations"])
            session["status"] = "executing" if session["is_executing"] else _state(row["lifecycle_status"] or row["status"])
            if not operations and row["status"] in {"admitting", "admitted"}:
                session["status"] = "pending"
            if row.get("_historical"):
                session["is_current"] = False
                session["is_executing"] = False
                session["status"] = "failed"
                session["activity_label"] = "已由新会话接续"
                session["updated_at"] = max([row["updated_at"]] + [
                    o["updated_at"] for o in session["operations"] if o["updated_at"] is not None])
            sessions.append(session)
        return sessions

    def query_output(self, quest_ref, session_ref, *, operation_ref, after=0, limit=65536):
        for value, code in ((quest_ref, "root_session_quest_not_found"),
                            (session_ref, "root_session_not_found"),
                            (operation_ref, "root_session_operation_not_found")):
            _validate_ref(value, code)
        if type(after) is not int or after < 0 or type(limit) is not int or not 4 <= limit <= 256 * 1024:
            raise ValueError("root_session_output_cursor_invalid")
        with self.database.read_snapshot() as connection:
            stages = self._stage_rows(connection, quest_ref, session_ref)
            if stages:
                if len(stages) != 1:
                    raise ValueError("root_session_binding_invalid")
                row = dict(stages[0])
                historical_review = operation_ref.endswith("~review")
                unit_ref = operation_ref.removesuffix("~review") if historical_review else operation_ref
                units = self._stage_units(connection, row, unit_ref)
                if len(units) != 1:
                    raise ValueError("root_session_operation_not_found")
                unit = units[0]
                reader, _scope = self._stage_reader(row, unit, historical_review=historical_review)
                try:
                    if historical_review and reader._resolve_operation(_scope) is None:
                        raise ValueError("root_session_operation_not_found")
                    page = reader.query_raw(row["run_ref"], after=after, limit=limit).as_dict()
                except StageRootObservationError as error:
                    raise ValueError(_output_error(error.code)) from error
                op = self._stage_operation(row, unit, historical_review=historical_review)
                return self._output_page(page, quest_ref, session_ref, operation_ref, op["status"])
            targets = self._target_rows(connection, quest_ref, session_ref)
            if targets:
                if len(targets) != 1:
                    raise ValueError("root_session_binding_invalid")
                row = dict(targets[0])
                operations = self._target_operations(connection, row, operation_ref)
                if len(operations) != 1:
                    raise ValueError("root_session_operation_not_found")
                operation = operations[0]
                try:
                    store, run = self._target_store(row, operation)
                    latest = self.runtime.harnesses._owner.latest_operation(run.run_ref)
                    page = store.query(operation_ref, after=after, limit=limit,
                        expected_native_session_ref=(operation.get("_native_session_ref") or
                            (run.native_session_ref if not row.get("_historical") and latest and latest.operation_ref == operation_ref else None)),
                        terminal=operation["status"] in {"executed", "failed"}).as_dict()
                    invocation_hash, _family = store._operation_bindings[operation_ref]
                    source = store._source_path(invocation_hash)
                    page["source_updated_at"] = source.stat().st_mtime if page["source_bytes"] else None
                except (TargetRawOutputUnavailable, OSError, ValueError, TypeError, KeyError) as error:
                    raise ValueError(_output_error(getattr(error, "code", ""))) from error
                page["native_session_ref"] = page.get("root_native_session_ref")
                # Target pages address the mapped root JSONL, not the enclosing
                # transport spool. Keep byte cursors in one coordinate system.
                transport_caught_up = page["source_caught_up"]
                page["transport_source_bytes"] = page["source_bytes"]
                page["transport_caught_up"] = transport_caught_up
                page["source_bytes"] = page["mapped_bytes"]
                page["has_more"] = page["next_offset"] < page["source_bytes"]
                page["source_caught_up"] = not page["has_more"]
                op = self._target_operation(row, operation)
                result = self._output_page(page, quest_ref, session_ref, operation_ref, op["status"])
                if not transport_caught_up and result["status"] == "terminal":
                    result["status"] = "waiting"
                return result
        page = self._provider_reader().query_output(quest_ref, session_ref,
            operation_ref=operation_ref, after=after, limit=limit)
        return {**page, "quest_ref": quest_ref,
                "root_native_session_ref": page.get("native_session_ref")}

    @staticmethod
    def _output_page(page, quest_ref, session_ref, operation_ref, operation_status):
        return {
            **page, "schema_ref": "meta-research/root-session-output/v1",
            "quest_ref": quest_ref,
            "session_ref": session_ref, "root_session_ref": session_ref,
            "operation_ref": operation_ref, "observed_at": time.time(),
            "root_native_session_ref": page.get("native_session_ref"),
            "status": "live" if operation_status == "executing" else
                "terminal" if operation_status in {"completed", "failed"} else "waiting",
        }


def _output_error(code):
    if code.endswith("cursor_stale"):
        return "root_session_output_cursor_stale"
    if code.endswith("cursor_invalid"):
        return "root_session_output_cursor_invalid"
    return "root_session_output_unavailable"
