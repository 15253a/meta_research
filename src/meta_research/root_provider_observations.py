"""Read-only discovery of durable DeepFetch and Acquisition root sessions.

The database supplies ownership; signed provider records supply operation and
native-thread identity. No observer calls an Owner reconciliation method.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from sqlalchemy import text

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError, provider_operation_ref, read_transport_envelope,
)

_MAX_RECORDS = 512
_MAX_SOURCE_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 8 * 1024 * 1024
_MAX_PAGE_BYTES = 256 * 1024
_REGISTRY_SCHEMA = "meta-research/deepfetch-provider-operation-registry/v1"
_DF_SCHEMA = "meta-research/deepfetch-provider-operation/v3"
_ACQ_SCHEMA = "meta-research/acquisition-root-session/v1"


class ProviderRootObservations:
    def __init__(self, runtime):
        self._runtime = runtime
        self._root = Path(runtime.data_root.root)
        self._database = runtime._database

    def query(self, quest_ref: str) -> dict[str, object]:
        sessions, _, reasons = self._catalog(quest_ref)
        return {"sessions": sessions, "limited": bool(reasons),
                "reasons": [{"code": code} for code in sorted(reasons)]}

    def query_output(self, quest_ref: str, session_ref: str, *,
                     operation_ref: str, after: int = 0,
                     limit: int = 65536) -> dict[str, object]:
        if type(after) is not int or after < 0 or type(limit) is not int or limit < 1:
            raise ValueError("root_session_output_query_invalid")
        sessions, operations, _ = self._catalog(quest_ref)
        session = next((s for s in sessions if s["session_ref"] == session_ref), None)
        if session is None:
            raise ValueError("root_session_not_found")
        operation = operations.get((session_ref, operation_ref))
        if operation is None:
            raise ValueError("root_session_operation_not_found")
        path = operation["directory"] / "stdout.jsonl"
        try:
            self._safe(path, file=True, optional=True)
            if not path.exists():
                native, visible, source_bytes, mtime, limited = (
                    operation["native"], b"", 0, None, False)
                projected_bytes = 0
            else:
                stat = path.stat()
                source_bytes, mtime = stat.st_size, stat.st_mtime
                if source_bytes > _MAX_SOURCE_BYTES:
                    raise ValueError("root_session_output_source_too_large")
                native = self._native(path, operation["native"])
                if native is None:
                    visible, projected_bytes, limited = b"", 0, bool(source_bytes)
                else:
                    visible, projected_bytes, limited = self._read_root_page(
                        path, native, after, max(4, min(limit, _MAX_PAGE_BYTES)))
                if operation["terminal_hash"] is not None:
                    digest = hashlib.sha256()
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != operation["terminal_hash"]:
                        raise ValueError("root_session_output_integrity_invalid")
            if after > projected_bytes:
                raise ValueError("root_session_output_cursor_stale")
            # A page may stop in a UTF-8 character. Leave its bytes for the next
            # page, matching the existing raw-output byte-cursor contract.
            try:
                rendered = visible.decode("utf-8")
            except UnicodeDecodeError as error:
                if error.reason != "unexpected end of data":
                    raise ValueError("root_session_output_cursor_invalid") from None
                rendered = visible[:error.start].decode("utf-8")
            consumed = len(rendered.encode("utf-8"))
            next_offset = after + consumed
            status = ("terminal" if operation["public"]["status"] in
                      {"completed", "failed"} else
                      "live" if session["is_executing"] else "waiting")
            return {
                "schema_ref": "meta-research/root-session-output/v1",
                "quest_ref": quest_ref, "session_ref": session_ref,
                "root_session_ref": session_ref, "operation_ref": operation_ref,
                "run_ref": session["run_ref"], "run_kind": session["kind"],
                "phase": operation["public"]["phase"],
                "root_native_session_ref": native, "native_session_ref": native,
                "transport_invocation_hash": operation["invocation_hash"],
                "stream_ref": "root-session-stream:" + canonical_hash({
                    "session_ref": session_ref, "operation_ref": operation_ref,
                    "invocation_hash": operation["invocation_hash"]}),
                "text": rendered, "offset": after, "next_offset": next_offset,
                "source_bytes": projected_bytes, "provider_source_bytes": source_bytes,
                "source_updated_at": mtime, "observed_at": time.time(),
                "has_more": next_offset < projected_bytes,
                "source_caught_up": next_offset >= projected_bytes,
                "status": status, "limited": limited,
                "exact": False, "unredacted": True,
            }
        except (OSError, ProviderSupervisorError, UnicodeError, json.JSONDecodeError):
            raise ValueError("root_session_output_unavailable") from None

    def _catalog(self, quest_ref):
        sessions, operations, reasons = [], {}, set()
        with self._database.read() as conn:
            quest = conn.execute(text(
                "SELECT initialization_id FROM rg_quests WHERE quest_ref=:q"),
                {"q": quest_ref}).mappings().first()
            if quest is None:
                return sessions, operations, reasons
            requests = []
            for table, clause, kind in (
                ("hc_deepfetch_requests", "initialization_id=:v", "quest_initialization"),
                ("hc_manual_deepfetch_requests", "quest_ref=:v", "manual_question_creation"),
                ("ae_autonomous_deepfetch_requests", "quest_ref=:v", "autonomous_question_creation"),
            ):
                value = quest["initialization_id"] if kind == "quest_initialization" else quest_ref
                rows = conn.execute(text(f"SELECT * FROM {table} WHERE {clause} "
                    "ORDER BY created_at LIMIT :n"), {"v": value, "n": _MAX_RECORDS + 1}).mappings().all()
                if len(rows) > _MAX_RECORDS:
                    reasons.add("root_session_list_limited")
                requests.extend((dict(row), kind) for row in rows[:_MAX_RECORDS])
            for request, kind in requests:
                run = conn.execute(text(
                    "SELECT r.*, s.root_session_ref, s.native_session_ref "
                    "FROM ar_deepfetch_runs r JOIN ar_deepfetch_sessions s "
                    "ON s.run_ref=r.run_ref WHERE r.request_ref=:r"),
                    {"r": request["request_ref"]}).mappings().first()
                if run is None:
                    continue
                try:
                    owner_session = None
                    if kind == "autonomous_question_creation":
                        owner_session = conn.execute(text(
                            "SELECT root_session_ref FROM ar_stage_runs WHERE request_ref=:r"),
                            {"r": request["reasoning_stage_run_request_ref"]}).scalar()
                    session, discovered = self._deepfetch(
                        quest_ref, request, kind, dict(run), owner_session, reasons)
                    sessions.append(session)
                    operations.update(discovered)
                except (OSError, ValueError, KeyError, TypeError, ProviderSupervisorError):
                    reasons.add("deepfetch_root_observation_unavailable")
            acquisitions = conn.execute(text(
                "SELECT * FROM ar_acquisition_sessions WHERE quest_ref=:q "
                "OR (quest_ref IS NULL AND initialization_id=:i)"),
                {"q": quest_ref, "i": quest["initialization_id"]}).mappings().all()
            for row in acquisitions:
                requests = conn.execute(text(
                    "SELECT request_id,status,created_at,updated_at,completed_at "
                    "FROM ar_acquisition_requests WHERE session_ref=:s "
                    "ORDER BY created_at LIMIT :n"),
                    {"s": row["session_ref"], "n": _MAX_RECORDS + 1}).mappings().all()
                if len(requests) > _MAX_RECORDS:
                    reasons.add("root_session_list_limited")
                try:
                    session, discovered = self._acquisition(
                        quest_ref, dict(row), requests[:_MAX_RECORDS], reasons)
                    sessions.append(session)
                    operations.update(discovered)
                except (OSError, ValueError, KeyError, TypeError, ProviderSupervisorError):
                    reasons.add("acquisition_root_observation_unavailable")
        return sessions, operations, reasons

    @staticmethod
    def _session(quest_ref, session_ref, kind, title, status, created, updated, **fields):
        return {"session_ref": session_ref, "root_session_ref": session_ref,
                "kind": kind, "title": title, "status": status,
                "is_executing": status == "executing", "quest_ref": quest_ref,
                "run_ref": None, "target_ref": None, "cycle_ref": None,
                "question_ref": None, "stage": None, "related_stages": [],
                "scope_label": "Quest", "owner_session_ref": None,
                "created_at": created, "updated_at": updated, "operations": [],
                "is_current": None, "native_session_ref": None, **fields}

    def _deepfetch(self, quest_ref, request, kind, run, owner_session, reasons):
        document = json.loads(request["request_json"]) if kind == "autonomous_question_creation" else request
        scope = document.get("scope") if kind == "autonomous_question_creation" else json.loads(request["scope_json"])
        if not isinstance(scope, dict) or canonical_hash(scope) != document["scope_hash"]:
            raise ValueError("deepfetch_scope_invalid")
        draft_hash = document.get("draft_hash", document.get("quest_draft_hash"))
        status = _status(run["status"])
        session = self._session(quest_ref, run["root_session_ref"], "deepfetch", "DeepFetch",
            status, run["created_at"], run["updated_at"], run_ref=run["run_ref"],
            cycle_ref=request.get("cycle_ref"),
            question_ref=scope.get("source_question_ref") or request.get("parent_question_ref"),
            stage="reasoning" if kind == "autonomous_question_creation" else None,
            related_stages=["reasoning"] if kind == "autonomous_question_creation" else [],
            scope_label={"quest_initialization": "Quest 初始化", "manual_question_creation": "手动创建问题",
                         "autonomous_question_creation": "Reasoning · 创建问题"}[kind],
            owner_session_ref=owner_session, native_session_ref=run["native_session_ref"],
            creation_context_kind=kind, creation_context_ref=request.get("context_ref"),
            request_ref=request["request_ref"])
        workspace = self._root / "deepfetch-provider"
        if not workspace.exists():
            return session, {}
        key = self._key(workspace)
        generations = run.get("provider_operation_generation") or 1
        if type(generations) is not int or generations < 1:
            raise ValueError("deepfetch_generation_invalid")
        if generations > _MAX_RECORDS:
            reasons.add("root_session_operations_limited")
        jobs = []
        for generation in range(max(1, generations - _MAX_RECORDS + 1), generations + 1):
            root_job = provider_operation_ref(run["run_ref"], "deepfetch", generation)
            if generation == generations and root_job != run["provider_operation_ref"]:
                raise ValueError("deepfetch_operation_identity_invalid")
            root_dir = workspace / "provider-operations" / (
                run["runtime_binding_hash"] + "-" + canonical_hash({"job_ref": root_job}))
            if not root_dir.exists():
                continue
            self._safe(root_dir)
            jobs.append(root_job)
            registry = root_dir / "registered-turns"
            if registry.exists():
                self._safe(registry)
                markers = sorted(registry.glob("turn-*.json"), key=lambda p: p.name)
                if len(markers) > _MAX_RECORDS:
                    reasons.add("root_session_operations_limited")
                for marker in markers[:_MAX_RECORDS]:
                    self._safe(marker, file=True)
                    match = re.fullmatch(r"turn-(\d+)\.json", marker.name)
                    if match is None:
                        raise ValueError("deepfetch_registry_invalid")
                    turn = int(match[1])
                    job = f"{root_job}:v4-turn:{turn}"
                    if read_transport_envelope(marker, key) != {
                        "schema_ref": _REGISTRY_SCHEMA, "root_job_ref": root_job,
                        "turn_number": turn, "provider_job_ref": job}:
                        raise ValueError("deepfetch_registry_invalid")
                    jobs.append(job)
        discovered = {}
        native = run["native_session_ref"]
        for job in dict.fromkeys(jobs):
            op_root = workspace / "provider-operations" / (
                run["runtime_binding_hash"] + "-" + canonical_hash({"job_ref": job}))
            if not op_root.exists():
                continue
            self._safe(op_root)
            paths = sorted((p for p in op_root.iterdir() if re.fullmatch(
                r"deepfetch-(initial|resume-\d+)", p.name)), key=lambda p: p.stat().st_mtime)
            for directory in paths:
                expected = {"schema_ref": _DF_SCHEMA, "job_ref": job,
                    "request_ref": request["request_ref"], "correlation_ref": run["correlation_ref"],
                    "draft_hash": draft_hash, "scope_hash": document["scope_hash"],
                    "runtime_binding_hash": run["runtime_binding_hash"],
                    "segment_name": directory.name.removeprefix("deepfetch-")}
                try:
                    operation = self._operation(directory, key, expected, native,
                        "DeepFetch", directory.name, status)
                    if operation is not None:
                        native = operation["native"] or native
                        self._append(session, operation, discovered)
                except (OSError, ValueError, ProviderSupervisorError):
                    reasons.add("deepfetch_root_operation_unavailable")
        session["native_session_ref"] = native
        if status == "executing" and not any(
            op["status"] == "executing" for op in session["operations"]
        ):
            # DeepFetch remains a running logical task while it has handed
            # control to Acquisition. A completed provider turn is not a live
            # model call; display the root as waiting for its continuation.
            session["status"] = "waiting" if session["operations"] else "pending"
            session["is_executing"] = False
            session["activity_label"] = "等待后续调用" if session["operations"] else "等待模型调用启动"
        return session, discovered

    def _acquisition(self, quest_ref, row, requests, reasons):
        status = _status(row["status"])
        session = self._session(quest_ref, row["session_ref"], "acquisition", "Acquisition",
            status, row["created_at"], row["updated_at"], run_ref=row["current_request_id"],
            scope_label="Quest 共享 · 全文获取",
            activity_label={"acquiring": "正在获取资料", "probing": "正在检查资料获取能力",
                            "ready": "等待资料获取请求"}.get(row["status"]))
        workspace = self._root / "acquisition-root-provider"
        if not workspace.exists():
            return session, {}
        key = self._key(workspace)
        receipts_dir = workspace / "root-sessions" / hashlib.sha256(row["session_ref"].encode()).hexdigest()
        jobs, native = {}, None
        if receipts_dir.exists():
            self._safe(receipts_dir)
            markers = sorted(receipts_dir.glob("turn-*.json"))
            if len(markers) > _MAX_RECORDS:
                reasons.add("root_session_operations_limited")
            for generation, path in enumerate(markers[:_MAX_RECORDS], 1):
                self._safe(path, file=True)
                receipt = read_transport_envelope(path, key)
                if (path.name != f"turn-{generation:08d}.json" or
                    receipt.get("schema_ref") != _ACQ_SCHEMA or
                    receipt.get("session_ref") != row["session_ref"] or
                    receipt.get("generation") != generation or
                    receipt.get("previous_native_session_ref") != native or
                    not isinstance(receipt.get("native_session_ref"), str) or
                    not receipt["native_session_ref"]):
                    raise ValueError("acquisition_session_chain_invalid")
                if native is not None and native != receipt["native_session_ref"]:
                    raise ValueError("acquisition_native_session_changed")
                native = receipt["native_session_ref"]
                jobs[receipt["job_ref"]] = (receipt["phase"], "completed")
        generation = row["preflight_generation"]
        preflight = f"acquisition:{row['session_ref']}:preflight:{row['config_hash']}"
        if generation > 1:
            preflight += f":generation:{generation}"
        jobs.setdefault(preflight, ("preflight", status if row["status"] == "probing" else "completed"))
        for request in requests:
            active = row["current_request_id"] == request["request_id"] and row["status"] == "acquiring"
            op_status = "executing" if active else _status(request["status"])
            for phase in ("batch", "reconcile"):
                job = f"acquisition:{row['session_ref']}:{phase}:{request['request_id']}"
                jobs.setdefault(job, (phase, op_status))
        discovered = {}
        codex_workspace = workspace / "codex-root"
        if not codex_workspace.exists():
            session["native_session_ref"] = native
            return session, discovered
        op_key = self._key(codex_workspace)
        for job, (phase, op_status) in jobs.items():
            if not job.startswith(f"acquisition:{row['session_ref']}:"):
                raise ValueError("acquisition_operation_identity_invalid")
            directory = codex_workspace / "provider-operations" / canonical_hash({"job_ref": job}) / "acquisition-root-turn"
            if not directory.exists():
                continue
            try:
                operation = self._operation(directory, op_key, {
                    "job_ref": job, "operation_name": "acquisition-root-turn"},
                    native, "Acquisition", phase, op_status)
                if operation is not None:
                    native = operation["native"] or native
                    self._append(session, operation, discovered)
            except (OSError, ValueError, ProviderSupervisorError):
                reasons.add("acquisition_root_operation_unavailable")
        session["native_session_ref"] = native
        return session, discovered

    def _append(self, session, operation, discovered):
        public = operation["public"]
        session["operations"].append(public)
        session["operations"].sort(key=lambda item: (item["created_at"], item["operation_ref"]))
        discovered[(session["session_ref"], public["operation_ref"])] = operation
        session["updated_at"] = max(session["updated_at"], public["updated_at"])

    def _operation(self, directory, key, expected, native, label, phase, default_status):
        self._safe(directory)
        invocation_path = directory / "invocation.json"
        if not invocation_path.exists():
            return None
        self._safe(invocation_path, file=True)
        invocation = read_transport_envelope(invocation_path, key)
        if any(invocation.get(k) != v for k, v in expected.items()):
            raise ValueError("root_session_operation_identity_invalid")
        sealed_native = invocation.get("native_session_ref")
        if sealed_native is not None and (not isinstance(sealed_native, str) or not sealed_native):
            raise ValueError("root_session_native_identity_invalid")
        if native and sealed_native and native != sealed_native:
            raise ValueError("root_session_native_identity_invalid")
        path = directory / "stdout.jsonl"
        observed_native = native or sealed_native
        source_mtime = invocation_path.stat().st_mtime
        if path.exists():
            self._safe(path, file=True)
            observed_native = self._native(path, observed_native)
            source_mtime = max(source_mtime, path.stat().st_mtime)
        invocation_hash = canonical_hash(invocation)
        status, terminal_hash = default_status, None
        terminal = directory / "supervisor-exit.json"
        if terminal.exists():
            self._safe(terminal, file=True)
            receipt = read_transport_envelope(terminal, key)
            if receipt.get("invocation_hash") != invocation_hash:
                raise ValueError("root_session_exit_identity_invalid")
            status = "completed" if receipt.get("returncode") == 0 and receipt.get("termination_reason") == "completed" else "failed"
            terminal_hash = receipt.get("stdout_file_hash")
            if not isinstance(terminal_hash, str) or len(terminal_hash) != 64:
                raise ValueError("root_session_exit_identity_invalid")
            source_mtime = max(source_mtime, terminal.stat().st_mtime)
        elif status == "executing":
            # Import lazily: the aggregate uses this reader through a lazy
            # import too. Reuse its sealed PID + exact operation environment
            # verification rather than interpreting a missing exit as activity.
            from meta_research.root_session_observations import _process_is_bound
            if not _process_is_bound(directory, invocation_hash):
                status = "waiting"
        operation_ref = "provider-root-operation:" + canonical_hash({
            "directory": str(directory.relative_to(self._root)), "invocation_hash": invocation_hash})
        return {"directory": directory, "native": observed_native,
                "invocation_hash": invocation_hash, "terminal_hash": terminal_hash,
                "public": {"operation_ref": operation_ref, "label": f"{label} · {phase}",
                    "phase": phase, "status": status,
                    "created_at": invocation_path.stat().st_mtime, "updated_at": source_mtime}}

    def _key(self, workspace):
        path = workspace / "provider-operations" / ".transport-seal.key"
        self._safe(path, file=True)
        key = path.read_bytes()
        if len(key) != 32:
            raise ValueError("root_session_transport_key_invalid")
        return key

    def _safe(self, path, *, file=False, optional=False):
        try:
            relative = path.relative_to(self._root)
        except ValueError:
            raise ValueError("root_session_path_invalid") from None
        cursor = self._root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise ValueError("root_session_path_invalid")
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("root_session_path_invalid")
        if optional and not path.exists():
            return
        if not (path.is_file() if file else path.is_dir()):
            raise ValueError("root_session_path_invalid")
        if not path.resolve().is_relative_to(self._root.resolve()):
            raise ValueError("root_session_path_invalid")

    @staticmethod
    def _native(path, expected):
        with path.open("rb") as stream:
            line = stream.readline(65537)
        if not line or not line.endswith(b"\n"):
            return expected
        if len(line) > 65536:
            raise ValueError("root_session_native_identity_invalid")
        event = json.loads(line)
        if (not isinstance(event, dict) or event.get("type") != "thread.started" or
            event.get("parent_thread_id") not in (None, "") or
            not isinstance(event.get("thread_id"), str) or not event["thread_id"] or
            expected is not None and expected != event["thread_id"]):
            raise ValueError("root_session_native_identity_invalid")
        return event["thread_id"]

    @staticmethod
    def _read_root_page(path, native, after, limit):
        # Cursor refers to the root-only UTF-8 byte stream. Explicit child actor
        # events are excluded; a root command's own returned tool result remains.
        chunks, total, limited = [], 0, False
        with path.open("rb") as stream:
            while True:
                line = stream.readline(_MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_LINE_BYTES or not line.endswith(b"\n"):
                    limited = True
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    limited = True
                    break
                if not isinstance(event, dict):
                    limited = True
                    break
                item = event.get("item")
                item = item if isinstance(item, dict) else {}
                actors = [scope.get(field) for scope in (event, item)
                    for field in ("thread_id", "session_id", "session_ref", "agent_id")
                    if scope.get(field) not in (None, "")]
                parents = [scope.get("parent_thread_id") for scope in (event, item)]
                if (any(actor != native for actor in actors) or any(parents) or
                    event.get("type") in {"sub_agent_activity", "reasoning", "agent_reasoning"} or
                    item.get("type") in {"reasoning", "agent_reasoning"}):
                    continue
                start, end = total, total + len(line)
                if end > after and start < after + limit:
                    chunks.append(line[max(0, after - start):min(len(line), after + limit - start)])
                total = end
        return b"".join(chunks), total, limited


def _status(status):
    if status in {"running", "acquiring", "probing"}:
        return "executing"
    if status in {"executed", "completed", "succeeded", "obtained", "partial", "missing"}:
        return "completed"
    if status in {"failed", "unavailable"}:
        return "failed"
    if status in {"cancelled", "paused", "suspended", "suspended_fenced"}:
        return "paused"
    if status in {"ready", "waiting_user", "waiting"}:
        return "waiting"
    return "pending"
