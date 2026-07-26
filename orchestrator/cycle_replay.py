"""Research-cycle file closure for deterministic replay.

SQLite remains the structured source of truth.  This module owns only the
post/around-call file side of the Codex boundary:

* every ContextPack is archived with its exact four regions and provenance;
* every accepted stage envelope (JSON/text files plus ``Artifact.md``) is
  stored in an immutable, content-addressed event directory;
* a Chinese ``handoff-N.md`` is emitted once for every accepted stage turn;
* after the DB cycle is terminal, the accepted reasoning ``md`` is promoted
  byte-for-byte to ``cycle_report.md`` and the whole directory is sealed by a
  hash inventory.

All writes are outside the research DB transaction and are idempotent.  A
crash can therefore leave a terminal DB cycle without its file closure; the
``reconcile_sqlite`` API repairs that gap before the ordinary storage snapshot
publisher is allowed to publish the next recovery point.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union

from .artifact_capability import (
    ArtifactCapabilityError,
    normalize_sha256,
    open_artifact,
    read_artifact_bytes,
)
from .interfaces import Artifact, ContextPack, ManagedArtifactRef
from .storage_paths import RegisteredPathError, _read_restore_receipt


ARCHIVE_SCHEMA = "meta-research-cycle-replay/v1"
CONTEXT_SCHEMA = "meta-research-context-pack-archive/v1"
ARTIFACT_SCHEMA = "meta-research-stage-artifact-archive/v1"
REPORT_SCHEMA = "meta-research-cycle-report/v1"
STATE_SCHEMA = "meta-research-cycle-state/v1"
TERMINAL_STATES = ("done", "failed", "aborted")
_TERMINAL_TARGET_STATES = frozenset({
    "complete", "skipped", "failed", "engineering_blocked",
})
_SCIENTIFIC_DECISION_TYPES = frozenset({
    "bundle_scientific_contract", "bundle_scientific_terminal",
})
_REVIEW_DECISION_TYPES = frozenset({
    "runtime_review_request", "runtime_review",
    "runtime_bundle_result_review_ack",
    "bundle_code_review", "bundle_result_review",
})
_POOL_DECISION_TYPES = frozenset({
    "bundle_training_candidate",
    "pool_training_publication", "pool_publication",
})
_STATE_DECISION_TYPES = (
    _SCIENTIFIC_DECISION_TYPES | _REVIEW_DECISION_TYPES
    | _POOL_DECISION_TYPES | {"runtime_cycle_summary"}
)

_CYCLE_RE = re.compile(r"^c[1-9][0-9]*$")
_SAFE_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HANDOFF_RE = re.compile(r"^handoff-([1-9][0-9]*)\.md$")
_MAX_ARCHIVE_FILE_BYTES = 128 * 1024 * 1024


class CycleReplayError(RuntimeError):
    """A replay asset is unsafe, corrupt, or conflicts with an immutable one."""


def _json_bytes(value: Any, *, pretty: bool = True) -> bytes:
    kwargs = {
        "ensure_ascii": False,
        "sort_keys": True,
        "allow_nan": False,
    }
    if pretty:
        kwargs["indent"] = 2
    else:
        kwargs["separators"] = (",", ":")
    return (json.dumps(value, **kwargs) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _regular_bytes(path: Path) -> bytes:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise CycleReplayError(f"回放资产缺失: {path}") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CycleReplayError(f"回放资产不是常规文件: {path}")
    if info.st_size > _MAX_ARCHIVE_FILE_BYTES:
        raise CycleReplayError(f"回放资产超过单文件上限: {path}")
    return path.read_bytes()


def _sync_dir(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_dir(path: Path) -> None:
    """Create one directory and reject symlink/non-directory substitutions."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        path.mkdir(mode=0o700)
        info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise CycleReplayError(f"回放目录类型非法: {path}")


def _ensure_tree(root: Path, relative: PurePosixPath) -> Path:
    current = root
    _ensure_dir(current)
    for part in relative.parts:
        current = current / part
        _ensure_dir(current)
    return current


def _atomic_write(path: Path, raw: bytes, *, immutable: bool = False) -> None:
    if len(raw) > _MAX_ARCHIVE_FILE_BYTES:
        raise CycleReplayError(f"回放写入超过单文件上限: {path}")
    if not path.is_absolute():
        raise CycleReplayError(f"回放写入路径须为绝对路径: {path}")
    # Walk from the filesystem root so every existing parent component is
    # checked for a symlink substitution before the atomic rename.
    anchor = Path(path.anchor)
    relative_parent = PurePosixPath(*path.parent.parts[1:])
    _ensure_tree(anchor, relative_parent)
    if path.exists() or path.is_symlink():
        existing = _regular_bytes(path)
        if existing == raw:
            return
        if immutable:
            raise CycleReplayError(f"不可变回放资产发生同名异内容冲突: {path}")
    temporary = path.parent / f".{path.name}.staging-{uuid.uuid4().hex}"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOFOLLOW", 0))
    fd = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _sync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_component(value: Optional[str], *, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or _SAFE_COMPONENT_RE.fullmatch(value) is None:
        raise CycleReplayError(f"{label} 不能安全映射为回放文件名: {value!r}")
    return value


def _safe_artifact_name(value: str) -> PurePosixPath:
    if (not isinstance(value, str) or not value or value.startswith("/")
            or "\\" in value or "\x00" in value):
        raise CycleReplayError(f"阶段产物文件名非法: {value!r}")
    # Match manifest._check_rel_path: preserve Unicode/dotfiles and reject only
    # non-canonical/escaping POSIX components.  Do this before PurePosixPath,
    # which would otherwise normalize away ``.`` and empty segments.
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise CycleReplayError(f"阶段产物文件名越界: {value!r}")
    return PurePosixPath(*raw_parts)


def _artifact_bytes(filename: str, value: Any) -> bytes:
    if filename.endswith(".json"):
        return _json_bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, bytes):
        return value
    # Text/config passthroughs from the envelope are JSON values.  Preserve an
    # unambiguous canonical representation instead of Python ``repr``.
    return _json_bytes(value)


class CycleReplayArchive:
    """Independent, idempotent API for one work-root's cycle file assets."""

    def __init__(self, work_root: Union[Path, str], *, owner_guard=None,
                 submission_registry=None):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.owner_guard = owner_guard or (lambda: None)
        # Optional quest-local SQL authority for runtime MCP stage submissions.
        # It is deliberately read-only from this file outbox: the MCP service
        # remains the sole writer, while replay may prove that a path-backed
        # file was accepted by that exact quest before indexing it.
        self.submission_registry = submission_registry
        self._lock = threading.RLock()
        if not self.work_root.is_dir() or self.work_root.is_symlink():
            raise CycleReplayError("work_root 须为已存在的非 symlink 目录")
        _ensure_dir(self.work_root / "cycles")

    def cycle_dir(self, cycle_id: str) -> Path:
        if not isinstance(cycle_id, str) or _CYCLE_RE.fullmatch(cycle_id) is None:
            raise CycleReplayError(f"cycle_id 非法: {cycle_id!r}")
        return self.work_root / "cycles" / cycle_id

    def persist_context_pack(self, pack: ContextPack, *, label: Optional[str] = None) -> Dict[str, str]:
        """Archive an exact ContextPack and publish a stable latest alias.

        Repeated calls with the same pack are byte no-ops.  Different packs for
        retries/reviews coexist under ``context_pack/history/<stage>/<pack_hash>/``;
        only the readable alias for that stage/target/label advances.
        """
        self.owner_guard()
        with self._lock:
            cycle = self.cycle_dir(pack.cycle_id)
            if pack.stage not in ("idea", "plan", "bundle", "reasoning"):
                raise CycleReplayError(f"ContextPack stage 非法: {pack.stage!r}")
            target = _safe_component(pack.target_id, label="target_id")
            label = _safe_component(label, label="context label")
            if label == pack.stage:
                label = None
            expected_hash = hashlib.sha256(("\x00".join((
                pack.anchor_md, pack.neighborhood_md, pack.retrieval_md,
                json.dumps(pack.refs, ensure_ascii=False),
            ))).encode("utf-8")).hexdigest()
            declared_hash = getattr(pack, "pack_hash", "")
            if declared_hash and declared_hash != expected_hash:
                # StubCompiler historically hashes the three prose sections
                # without NUL/refs.  It has its own archiver; accepting that
                # incompatible identity here would make the production replay
                # manifest lie about exact bytes.
                raise CycleReplayError(
                    f"ContextPack pack_hash 与四区内容不一致: {declared_hash} != {expected_hash}")
            pack_hash = declared_hash or expected_hash
            if re.fullmatch(r"[0-9a-f]{64}", pack_hash) is None:
                raise CycleReplayError("ContextPack pack_hash 非 sha256 hex")
            # Focused/test pack objects may omit the optional field.  Binding
            # the computed identity lets the subsequent Artifact event point
            # back to the exact archived input.
            try:
                pack.pack_hash = pack_hash
            except (AttributeError, TypeError):
                pass
            name_parts = [pack.stage]
            if target:
                name_parts.append(target)
            if label:
                name_parts.append(label)
            name = ".".join(name_parts)
            context = cycle / "context_pack"
            history_scope = ".".join(
                [pack.stage] + ([target] if target is not None else []))
            history = context / "history" / history_scope / pack_hash
            exact = {
                "schema": CONTEXT_SCHEMA,
                "cycle_id": pack.cycle_id,
                "stage": pack.stage,
                "target_id": pack.target_id,
                "pack_hash": pack_hash,
                "anchor_md": pack.anchor_md,
                "neighborhood_md": pack.neighborhood_md,
                "retrieval_md": pack.retrieval_md,
                "refs": list(pack.refs),
                "sources": sorted(set(pack.sources)),
            }
            manifest = {
                "schema": CONTEXT_SCHEMA,
                "cycle_id": pack.cycle_id,
                "stage": pack.stage,
                "target_id": pack.target_id,
                "pack_hash": pack_hash,
                "sources": sorted(set(pack.sources)),
                "refs": list(pack.refs),
                "section_sha256": {
                    "anchor_md": _sha256(pack.anchor_md.encode("utf-8")),
                    "neighborhood_md": _sha256(pack.neighborhood_md.encode("utf-8")),
                    "retrieval_md": _sha256(pack.retrieval_md.encode("utf-8")),
                },
            }
            alias_manifest = {
                **manifest, "label": label,
                "history_ref": f"history/{history_scope}/{pack_hash}/manifest.json",
            }
            readable = (
                "--- ① 固定锚（任务关键，不截断）---\n" + pack.anchor_md.strip()
                + "\n\n--- ② 结构邻域 ---\n" + (pack.neighborhood_md.strip() or "（空）")
                + "\n\n--- ③ 检索区 ---\n" + (pack.retrieval_md.strip() or "（空）")
                + "\n\n--- ④ 引用区 ---\n" + ("\n".join(pack.refs) or "（空）") + "\n"
            ).encode("utf-8")
            exact_raw = _json_bytes(exact)
            history_manifest_raw = _json_bytes(manifest)
            alias_manifest_raw = _json_bytes(alias_manifest)
            if (cycle / "replay_manifest.json").exists():
                self.verify_cycle(pack.cycle_id)
                expected = (
                    (history / "pack.json", exact_raw),
                    (history / "pack.md", readable),
                    (history / "manifest.json", history_manifest_raw),
                    (context / f"{name}.pack.json", exact_raw),
                    (context / f"{name}.pack.md", readable),
                    (context / f"{name}.manifest.json", alias_manifest_raw),
                )
                for path, raw in expected:
                    if not path.exists() or _regular_bytes(path) != raw:
                        raise CycleReplayError(
                            f"终态 cycle 已封口，拒绝追加/改写 ContextPack alias: {path}")
                return {"pack_hash": pack_hash, "name": name}
            _atomic_write(history / "pack.json", exact_raw, immutable=True)
            _atomic_write(history / "pack.md", readable, immutable=True)
            _atomic_write(history / "manifest.json", history_manifest_raw, immutable=True)
            _atomic_write(context / f"{name}.pack.json", exact_raw)
            _atomic_write(context / f"{name}.pack.md", readable)
            _atomic_write(context / f"{name}.manifest.json", alias_manifest_raw)
            return {"pack_hash": pack_hash, "name": name}

    def persist_stage_artifact(
            self, *, cycle_id: str, stage: str, artifact: Artifact,
            target_id: Optional[str] = None, purpose: Optional[str] = None,
            pack_hash: Optional[str] = None, runner_call_id: Optional[int] = None,
            handoff: bool = True) -> Dict[str, Any]:
        """Persist one accepted runner envelope, including its human ``md``."""
        return self.persist_stage_output(
            cycle_id=cycle_id, stage=stage, files=artifact.files, md=artifact.md,
            target_id=target_id, purpose=purpose, pack_hash=pack_hash,
            runner_call_id=runner_call_id, handoff=handoff,
            stage_submission_ref=artifact.stage_submission_ref,
            stage_submission_artifact_hash=artifact.stage_submission_hash,
            provenance={
                "prompt_sha256": artifact.prompt_sha256,
                "transcript_ref": artifact.transcript_ref,
                "execution_receipt_ref": artifact.execution_receipt_ref,
                "provider_receipt_ref": artifact.provider_receipt_ref,
                "stage_submission_ref": artifact.stage_submission_ref,
                "stage_submission_artifact_hash": artifact.stage_submission_hash,
            })

    def persist_stage_output(
            self, *, cycle_id: str, stage: str, files: Mapping[str, Any], md: str = "",
            target_id: Optional[str] = None, purpose: Optional[str] = None,
            pack_hash: Optional[str] = None, runner_call_id: Optional[int] = None,
            handoff: bool = True, provenance: Optional[Mapping[str, Any]] = None,
            stage_submission_ref: Optional[str] = None,
            stage_submission_artifact_hash: Optional[str] = None) -> Dict[str, Any]:
        """Persist a validated stage output or deterministic derived output.

        ``files`` are encoded exactly as the runner-to-orchestrator contract
        dictates: JSON canonically, text/bytes verbatim.  The immutable event
        id includes the invocation id when available, so two intentional turns
        with identical content remain two replayable calls.
        """
        self.owner_guard()
        with self._lock:
            if stage not in ("idea", "plan", "bundle", "reasoning"):
                raise CycleReplayError(f"阶段产物 stage 非法: {stage!r}")
            if not isinstance(files, Mapping) or not files:
                raise CycleReplayError("阶段产物 files 须为非空 mapping")
            if not isinstance(md, str):
                raise CycleReplayError("阶段产物 md 须为字符串")
            target = _safe_component(target_id, label="target_id")
            purpose = _safe_component(purpose or stage, label="purpose")
            if pack_hash is not None and re.fullmatch(r"[0-9a-f]{64}", pack_hash) is None:
                raise CycleReplayError("阶段产物 pack_hash 非 sha256 hex")
            if (runner_call_id is not None
                    and (isinstance(runner_call_id, bool) or not isinstance(runner_call_id, int)
                         or runner_call_id <= 0)):
                raise CycleReplayError("runner_call_id 须为正整数")
            encoded: List[Tuple[PurePosixPath, bytes]] = []
            managed_entries: List[Dict[str, Any]] = []
            for filename in sorted(files):
                rel = _safe_artifact_name(filename)
                value = files[filename]
                if isinstance(value, ManagedArtifactRef):
                    managed_entries.append(self._managed_artifact_entry(
                        cycle_id, rel, value, stage=stage,
                        target_id=target_id, purpose=purpose,
                        pack_hash=pack_hash, runner_call_id=runner_call_id,
                        stage_submission_ref=stage_submission_ref,
                        stage_submission_artifact_hash=(
                            stage_submission_artifact_hash)))
                else:
                    encoded.append((rel, _artifact_bytes(filename, value)))
            md_raw = md.encode("utf-8")
            identity = {
                "cycle_id": cycle_id,
                "stage": stage,
                "target_id": target_id,
                "purpose": purpose,
                "pack_hash": pack_hash,
                "runner_call_id": runner_call_id,
                "files": [
                    *[{"path": rel.as_posix(), "sha256": _sha256(raw), "bytes": len(raw)}
                      for rel, raw in encoded],
                    *managed_entries,
                ],
                "md_sha256": _sha256(md_raw),
            }
            event_id = _sha256(_json_bytes(identity, pretty=False))
            cycle = self.cycle_dir(cycle_id)
            event = cycle / "artifacts" / "history" / event_id
            key_parts = [stage]
            if target:
                key_parts.append(target)
            if purpose and purpose != stage:
                key_parts.append(purpose)
            key = ".".join(key_parts)
            if (cycle / "replay_manifest.json").exists():
                self.verify_cycle(cycle_id)
                if not event.exists():
                    raise CycleReplayError(
                        f"终态 cycle 已封口，拒绝追加新 turn: {cycle.name}")
                return {
                    "event_id": event_id,
                    "handoff_no": self._handoff_no_for_event(cycle, event_id),
                    "key": key,
                }
            for rel, raw in encoded:
                _atomic_write(event / "files" / Path(*rel.parts), raw, immutable=True)
            if managed_entries:
                _atomic_write(
                    event / "managed-files.json",
                    _json_bytes({"version": 1, "files": managed_entries}), immutable=True)
            _atomic_write(event / "stage.md", md_raw, immutable=True)
            event_manifest = {
                "schema": ARTIFACT_SCHEMA,
                "event_id": event_id,
                **identity,
                "provenance": {key: value for key, value in (provenance or {}).items()
                               if value is not None},
            }
            _atomic_write(event / "manifest.json", _json_bytes(event_manifest), immutable=True)

            canonical = cycle / "artifacts" / "by-stage" / key
            for rel, raw in encoded:
                _atomic_write(canonical / Path(*rel.parts), raw)
            if managed_entries:
                _atomic_write(
                    canonical / "_managed-files.json",
                    _json_bytes({"version": 1, "files": managed_entries}))
            _atomic_write(canonical / f"{stage}.md", md_raw)
            pointer = {
                "schema": ARTIFACT_SCHEMA,
                "event_id": event_id,
                "stage": stage,
                "target_id": target_id,
                "purpose": purpose,
                "pack_hash": pack_hash,
                "history_ref": f"history/{event_id}/manifest.json",
            }
            _atomic_write(cycle / "artifacts" / f"{key}.latest.json", _json_bytes(pointer))
            self._write_compatibility_aliases(
                cycle, stage=stage, target=target, purpose=purpose,
                encoded=encoded, md_raw=md_raw)
            handoff_no = self._persist_handoff(
                cycle, event_id=event_id, stage=stage, target=target,
                purpose=purpose, pack_hash=pack_hash,
                file_names=[item[0].as_posix() for item in encoded]
                + [item["path"] for item in managed_entries], md=md,
            ) if handoff else None
            return {"event_id": event_id, "handoff_no": handoff_no, "key": key}

    def finalize_cycle(self, *, cycle_id: str, status: str, route: Optional[str],
                       question_id: Optional[str] = None,
                       next_question_id: Optional[str] = None,
                       next_intent: Optional[str] = None,
                       restored_sqlite_truth_only: bool = False) -> Dict[str, Any]:
        """Seal one terminal research cycle and promote the reasoning report.

        A normal reasoning output with non-empty ``md`` is copied without even
        newline normalization.  Mechanical/failed/legacy cycles and a
        rigorously verified SQLite-only restore receive explicitly labelled
        orchestrator provenance instead of fabricated model prose.
        """
        self.owner_guard()
        with self._lock:
            if status not in TERMINAL_STATES:
                raise CycleReplayError(f"cycle 尚非终态，不能封口: {cycle_id}/{status}")
            cycle = self.cycle_dir(cycle_id)
            _ensure_dir(cycle)
            reasoning = self._latest_stage_event(cycle, "reasoning")
            restore_provenance = (
                self._restore_provenance(cycle_id)
                if restored_sqlite_truth_only else None)
            if restored_sqlite_truth_only and (
                    status != "done" or reasoning is not None
                    or restore_provenance is None):
                raise CycleReplayError(
                    f"cycle {cycle_id} SQLite-only 恢复封口授权非法")
            if restore_provenance is not None:
                try:
                    restored_state = json.loads(_regular_bytes(
                        cycle / "cycle_state.json").decode("utf-8"))
                except (CycleReplayError, UnicodeDecodeError,
                        json.JSONDecodeError) as error:
                    raise CycleReplayError(
                        f"cycle {cycle_id} SQLite-only 恢复状态投影缺失/损坏"
                    ) from error
                self._assert_restored_state_projection(
                    cycle_id=cycle_id, state=restored_state,
                    restore_provenance=restore_provenance)
            if (status == "done" and reasoning is None
                    and restore_provenance is None):
                raise CycleReplayError(
                    f"done cycle {cycle_id} 缺 reasoning stage event，拒绝机械假绿封口")
            source_event = None
            if reasoning is not None:
                source_event, _event_manifest, md_raw = reasoning
            else:
                md_raw = b""
            if restore_provenance is not None:
                report_kind = "restored_sqlite_truth_only"
                report_raw = self._restored_sqlite_report(
                    cycle_id=cycle_id, status=status, route=route,
                    question_id=question_id,
                    restore_provenance=restore_provenance).encode("utf-8")
            elif md_raw.strip():
                report_raw = md_raw
                report_kind = "reasoning_md_promoted"
            else:
                report_kind = (
                    "orchestrator_terminal" if cycle.exists() and any(cycle.iterdir())
                    else "legacy_recovered")
                report_raw = self._mechanical_report(
                    cycle_id=cycle_id, status=status, route=route,
                    question_id=question_id, next_question_id=next_question_id,
                    next_intent=next_intent,
                    reasoning_present=reasoning is not None).encode("utf-8")
            report_manifest = {
                "schema": REPORT_SCHEMA,
                "cycle_id": cycle_id,
                "status": status,
                "route": route,
                "report_kind": report_kind,
                "source_event_id": source_event,
                "source_md_sha256": (_sha256(md_raw) if reasoning is not None else None),
                "cycle_report_sha256": _sha256(report_raw),
                "cycle_report_bytes": len(report_raw),
            }
            if restore_provenance is not None:
                report_manifest["restore_provenance"] = restore_provenance
            report_path = cycle / "cycle_report.md"
            report_manifest_path = cycle / "cycle_report.manifest.json"
            # Once published, a terminal report is immutable.  Reconciliation
            # may repair a missing companion manifest but can never silently
            # replace already-published prose with a different model turn.
            _atomic_write(report_path, report_raw, immutable=True)
            _atomic_write(report_manifest_path, _json_bytes(report_manifest), immutable=True)
            closure = self._closure_manifest(
                cycle, cycle_id=cycle_id, status=status, route=route,
                report_kind=report_kind, source_event=source_event)
            _atomic_write(cycle / "replay_manifest.json", _json_bytes(closure), immutable=True)
            return closure

    def reconcile_sqlite(self, source) -> List[str]:  # noqa: ANN001 - daemon/connection protocol
        """Seal every terminal research cycle missing its replay closure.

        ``source`` may be ``WriteDaemon``, a sqlite connection, or a focused
        test double exposing ``query``.  The method is read-only with respect
        to SQLite.  Import worker cycles are deliberately skipped: they are
        process-accounting cycles and, by contract, do not produce a research
        ``cycle_report``.
        """
        self.owner_guard()
        rows = self._query(source,
            "SELECT id,status,route,active_question_id,next_question_id,next_intent "
            "FROM cycle WHERE status IN ('done','failed','aborted') ORDER BY id")
        sealed: List[str] = []
        for ci, status, route, active_q, next_q, next_intent in rows:
            worker = self._query_one(source,
                "SELECT 1 FROM decision WHERE cycle_id=? AND actor='orchestrator' "
                "AND type='import_worker_cycle' LIMIT 1", (ci,))
            if worker is not None:
                continue
            cycle_id = f"c{ci}"
            report = self.cycle_dir(cycle_id) / "replay_manifest.json"
            state = self._cycle_state_projection(source, cycle_id=cycle_id)
            state_raw = _json_bytes(state)
            if report.exists():
                # Full verification is cheap for stage JSON/md and catches a
                # post-publication edit before the DB backup is advanced.
                self.verify_cycle(cycle_id)
                state_path = self.cycle_dir(cycle_id) / "cycle_state.json"
                # Pre-state-projection closures remain explicitly legacy.  A
                # closure produced by this implementation, however, must keep
                # matching the complete terminal SQLite state byte-for-byte.
                if state_path.exists() and _regular_bytes(state_path) != state_raw:
                    raise CycleReplayError(
                        f"cycle {cycle_id} cycle_state 与 SQLite 终态漂移")
                continue
            reasoning_commits = state["reasoning"]["phase_commits"]
            restored_sqlite_truth_only = (
                status == "done"
                and state["reasoning"]["stage_event_id"] is None
                and state["reasoning"]["availability"]
                == "not_restored_sqlite_truth_only")
            if status == "done" and len(reasoning_commits) != 1:
                raise CycleReplayError(
                    f"done cycle {cycle_id} 缺唯一 Reasoning phase_commit，"
                    "拒绝状态假绿封口")
            if status == "done":
                self._assert_done_target_projection(state)
            if (status == "done"
                    and state["reasoning"]["stage_event_id"] is None
                    and not restored_sqlite_truth_only):
                raise CycleReplayError(
                    f"done cycle {cycle_id} 缺 reasoning stage event，"
                    "拒绝提前冻结不完整状态投影")
            _atomic_write(
                self.cycle_dir(cycle_id) / "cycle_state.json",
                state_raw, immutable=True)
            inferred_q = active_q
            if inferred_q is None:
                row = self._query_one(source,
                    "SELECT question_id FROM idea WHERE cycle_id=? ORDER BY id LIMIT 1", (ci,))
                if row is None:
                    row = self._query_one(source,
                        "SELECT question_id FROM answer WHERE cycle_id=? ORDER BY id LIMIT 1", (ci,))
                inferred_q = row[0] if row is not None else None
            self.finalize_cycle(
                cycle_id=cycle_id, status=status, route=route,
                question_id=f"q{inferred_q}" if inferred_q is not None else None,
                next_question_id=f"q{next_q}" if next_q is not None else None,
                next_intent=next_intent,
                restored_sqlite_truth_only=restored_sqlite_truth_only)
            sealed.append(cycle_id)
        return sealed

    def verify_cycle(self, cycle_id: str) -> Dict[str, Any]:
        """Verify the immutable inventory and reasoning-report promotion."""
        cycle = self.cycle_dir(cycle_id)
        manifest = json.loads(_regular_bytes(cycle / "replay_manifest.json").decode("utf-8"))
        if manifest.get("schema") != ARCHIVE_SCHEMA or manifest.get("cycle_id") != cycle_id:
            raise CycleReplayError(f"cycle {cycle_id} replay manifest 身份非法")
        expected_paths = set()
        for item in manifest.get("files", []):
            try:
                rel = _safe_artifact_name(item["path"])
                if rel.as_posix() in expected_paths:
                    raise CycleReplayError(
                        f"cycle {cycle_id} replay manifest 路径重复: {rel}")
                expected_paths.add(rel.as_posix())
                raw = _regular_bytes(cycle / Path(*rel.parts))
            except (KeyError, TypeError) as error:
                raise CycleReplayError(f"cycle {cycle_id} replay manifest 条目损坏") from error
            if len(raw) != item.get("bytes") or _sha256(raw) != item.get("sha256"):
                raise CycleReplayError(f"cycle {cycle_id} 回放资产 hash/size 漂移: {rel}")
        actual_paths = set()
        for path in cycle.rglob("*"):
            rel = path.relative_to(cycle).as_posix()
            if rel == "replay_manifest.json" or ".staging-" in path.name:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CycleReplayError(f"cycle {cycle_id} 回放闭包含非法文件类型: {rel}")
            actual_paths.add(rel)
        if actual_paths != expected_paths:
            raise CycleReplayError(
                f"cycle {cycle_id} replay inventory 漂移："
                f"missing={sorted(expected_paths - actual_paths)} "
                f"extra={sorted(actual_paths - expected_paths)}")
        coverage = manifest.get("coverage")
        if not isinstance(coverage, dict):
            raise CycleReplayError(f"cycle {cycle_id} replay coverage 非法")
        state_present = "cycle_state.json" in expected_paths
        declared_state = coverage.get("cycle_state")
        if declared_state is not None and declared_state is not state_present:
            raise CycleReplayError(
                f"cycle {cycle_id} replay cycle_state coverage 漂移")
        state = None
        if state_present:
            try:
                state = json.loads(
                    _regular_bytes(cycle / "cycle_state.json").decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise CycleReplayError(
                    f"cycle {cycle_id} cycle_state JSON 损坏") from error
            if (not isinstance(state, dict)
                    or state.get("schema") != STATE_SCHEMA
                    or state.get("cycle_id") != cycle_id
                    or not isinstance(state.get("targets"), list)
                    or not isinstance(state.get("reasoning"), dict)):
                raise CycleReplayError(
                    f"cycle {cycle_id} cycle_state 身份/结构非法")
            if manifest.get("status") == "done":
                self._assert_done_target_projection(state)
        report_manifest = json.loads(
            _regular_bytes(cycle / "cycle_report.manifest.json").decode("utf-8"))
        report = _regular_bytes(cycle / "cycle_report.md")
        if (_sha256(report) != report_manifest.get("cycle_report_sha256")
                or len(report) != report_manifest.get("cycle_report_bytes")):
            raise CycleReplayError(f"cycle {cycle_id} cycle_report 漂移")
        if report_manifest.get("report_kind") == "reasoning_md_promoted":
            event_id = report_manifest.get("source_event_id")
            source_md = _regular_bytes(
                cycle / "artifacts" / "history" / str(event_id) / "stage.md")
            if source_md != report:
                raise CycleReplayError(f"cycle {cycle_id} reasoning md 未原字节转正")
        if (manifest.get("status") == "done"
                and manifest.get("report_kind")
                == "restored_sqlite_truth_only"):
            restore_provenance = self._restore_provenance(cycle_id)
            if (manifest.get("source_event_id") is not None
                    or report_manifest.get("source_event_id") is not None
                    or report_manifest.get("source_md_sha256") is not None
                    or restore_provenance is None
                    or report_manifest.get("restore_provenance")
                    != restore_provenance):
                raise CycleReplayError(
                    f"done cycle {cycle_id} SQLite-only 恢复证据不闭合")
            self._assert_restored_state_projection(
                cycle_id=cycle_id, state=state,
                restore_provenance=restore_provenance)
        elif manifest.get("status") == "done":
            event_id = manifest.get("source_event_id")
            if (not isinstance(event_id, str)
                    or re.fullmatch(r"[0-9a-f]{64}", event_id) is None
                    or report_manifest.get("source_event_id") != event_id):
                raise CycleReplayError(
                    f"done cycle {cycle_id} 缺 exact reasoning source event")
            source_manifest = json.loads(_regular_bytes(
                cycle / "artifacts" / "history" / event_id /
                "manifest.json").decode("utf-8"))
            if (source_manifest.get("event_id") != event_id
                    or source_manifest.get("stage") != "reasoning"
                    or source_manifest.get("target_id") is not None):
                raise CycleReplayError(
                    f"done cycle {cycle_id} source event 非 targetless Reasoning")
        return manifest

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _query(source, sql: str, params: tuple = ()) -> List[tuple]:  # noqa: ANN001
        if hasattr(source, "query"):
            return list(source.query(sql, params))
        return list(source.execute(sql, params).fetchall())

    @classmethod
    def _query_one(cls, source, sql: str, params: tuple = ()):  # noqa: ANN001
        if hasattr(source, "query_one"):
            return source.query_one(sql, params)
        return source.execute(sql, params).fetchone()

    def _restore_provenance(self, cycle_id: str) -> Optional[Dict[str, Any]]:
        """Return narrow authority for cycles copied by a SQLite-only restore.

        The receipt reader is the same ownership/canonical-bytes trust boundary
        used by registered-path consumers.  This exception applies only through
        the receipt's frozen source cycle; any later local cycle still requires
        its own archived Reasoning stage event.
        """
        try:
            receipt = _read_restore_receipt(self.work_root)
        except RegisteredPathError as error:
            raise CycleReplayError(
                "SQLite-only restore receipt 无法验证") from error
        if receipt is None:
            return None
        source_cycle = receipt["source_cycle"]
        if int(cycle_id[1:]) > int(source_cycle[1:]):
            return None
        backup = receipt["backup"]
        backup_hash = backup.get("sha256")
        backup_bytes = backup.get("bytes")
        backup_path = backup.get("path")
        expected_path = (
            f"state/storage/backups/sha256/{backup_hash}.sqlite")
        if (not isinstance(backup_hash, str)
                or re.fullmatch(r"[0-9a-f]{64}", backup_hash) is None
                or not isinstance(backup_bytes, int)
                or isinstance(backup_bytes, bool)
                or backup_bytes < 1
                or backup_path != expected_path
                or receipt.get("continuation_mode") not in {
                    "legacy_adoption_on_first_start",
                    "import_materialization_restore_required",
                    "registered_asset_restore_required",
                }):
            raise CycleReplayError(
                "SQLite-only restore receipt backup/provenance 非法")
        return {
            "protocol": "sqlite-truth-only-restore-provenance-v1",
            "scope": "sqlite_truth_only",
            "source_cycle": source_cycle,
            "source_manifest_sha256": receipt["source_manifest_sha256"],
            "backup_sha256": backup_hash,
            "backup_bytes": backup_bytes,
            "continuation_mode": receipt["continuation_mode"],
        }

    @staticmethod
    def _assert_restored_state_projection(
            *, cycle_id: str, state: Any,
            restore_provenance: Mapping[str, Any]) -> None:
        cycle = state.get("cycle") if isinstance(state, dict) else None
        reasoning = state.get("reasoning") if isinstance(state, dict) else None
        commits = (
            reasoning.get("phase_commits")
            if isinstance(reasoning, dict) else None)
        valid_commit = (
            isinstance(commits, list) and len(commits) == 1
            and isinstance(commits[0], dict)
            and commits[0].get("stage") == "reasoning"
            and commits[0].get("target_id") is None)
        if (not isinstance(state, dict)
                or state.get("schema") != STATE_SCHEMA
                or state.get("cycle_id") != cycle_id
                or state.get("restore_provenance") != restore_provenance
                or not isinstance(cycle, dict)
                or cycle.get("status") != "done"
                or not isinstance(reasoning, dict)
                or reasoning.get("availability")
                != "not_restored_sqlite_truth_only"
                or reasoning.get("stage_event_id") is not None
                or not valid_commit):
            raise CycleReplayError(
                f"cycle {cycle_id} SQLite-only 恢复状态投影不闭合")

    @staticmethod
    def _assert_done_target_projection(state: Mapping[str, Any]) -> None:
        """Reject a normal done closure with an incomplete Bundle truth set."""
        cycle_id = str(state.get("cycle_id") or "cycle")
        targets = state.get("targets")
        decisions = state.get("scientific_decisions")
        if not isinstance(targets, list) or not isinstance(decisions, list):
            raise CycleReplayError(
                f"done cycle {cycle_id} target/科学状态投影非法")
        by_id = {
            item.get("id"): item for item in decisions
            if isinstance(item, dict)
        }
        execution_values = {
            "succeeded", "failed", "skipped", "engineering_blocked",
        }
        validity_values = {"valid", "invalid", "not_assessed"}
        outcome_values = {
            "supported", "refuted", "inconclusive", "unavailable",
        }
        pool_values = {"eligible", "ineligible"}
        for target in targets:
            if not isinstance(target, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target 投影非法")
            target_id = target.get("id")
            if target.get("status") not in _TERMINAL_TARGET_STATES:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} 非终态")
            commits = target.get("phase_commits")
            if not isinstance(commits, list) or len(commits) != 1:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "缺唯一 Bundle phase_commit")
            decision_ids = target.get("scientific_decision_ids")
            if not isinstance(decision_ids, list) or not decision_ids:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} 缺科学四轴终态")
            target_decisions = [by_id.get(item) for item in decision_ids]
            terminal_decisions = [
                item for item in target_decisions
                if isinstance(item, dict)
                and item.get("type") == "bundle_scientific_terminal"
            ]
            if terminal_decisions and (
                    len(terminal_decisions) != 1
                    or len(target_decisions) != 1):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "科学四轴 terminal/attempt 冲突")
            for decision_id in decision_ids:
                decision = by_id.get(decision_id)
                payload = (
                    decision.get("payload")
                    if isinstance(decision, dict) else None)
                kind = decision.get("type") if isinstance(decision, dict) else None
                expected_protocol = {
                    "bundle_scientific_contract":
                        "bundle-scientific-contract-v1",
                    "bundle_scientific_terminal":
                        "bundle-scientific-terminal-v1",
                }.get(kind)
                if (decision is None
                        or decision.get("actor") != "orchestrator"
                        or expected_protocol is None
                        or not isinstance(payload, dict)
                        or payload.get("protocol") != expected_protocol
                        or str(payload.get("build_target_id"))
                        != str(target_id)
                        or payload.get("execution_status")
                        not in execution_values
                        or payload.get("validity_status")
                        not in validity_values
                        or payload.get("scientific_outcome")
                        not in outcome_values
                        or payload.get("pool_eligibility")
                        not in pool_values):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "科学四轴 decision 非法")
                execution = payload["execution_status"]
                validity = payload["validity_status"]
                outcome = payload["scientific_outcome"]
                eligibility = payload["pool_eligibility"]
                if kind == "bundle_scientific_terminal":
                    terminal_fields = {
                        "protocol", "build_target_id", "target_status",
                        "failure_kind", "contract_hash", "execution_status",
                        "validity_status", "scientific_outcome",
                        "pool_eligibility",
                    }
                    expected_execution = {
                        "complete": "succeeded",
                        "failed": "failed",
                        "skipped": "skipped",
                        "engineering_blocked": "engineering_blocked",
                    }[target["status"]]
                    if (set(payload) != terminal_fields
                            or payload.get("target_status") != target["status"]
                            or payload.get("failure_kind")
                            != target.get("failure_kind")
                            or re.fullmatch(
                                r"[0-9a-f]{64}",
                                str(payload.get("contract_hash") or ""))
                            is None
                            or execution != expected_execution
                            or (validity, outcome, eligibility)
                            != ("not_assessed", "unavailable", "ineligible")):
                        raise CycleReplayError(
                            f"done cycle {cycle_id} target {target_id} "
                            "科学四轴 terminal 与 target 终态冲突")
                if ((execution != "succeeded"
                     and (validity, outcome, eligibility)
                     != ("not_assessed", "unavailable", "ineligible"))
                        or (validity == "invalid"
                            and (outcome != "unavailable"
                                 or eligibility != "ineligible"))
                        or (validity == "not_assessed"
                            and (outcome != "unavailable"
                                 or eligibility != "ineligible"))
                        or (eligibility == "eligible"
                            and (execution != "succeeded"
                                 or validity != "valid"
                                 or outcome == "unavailable"))):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "科学四轴互相冲突")
        CycleReplayArchive._assert_done_dag_projection(state)

    @staticmethod
    def _assert_done_dag_projection(state: Mapping[str, Any]) -> None:
        """Fail closed unless a v2 DAG is a complete self-checking closure."""
        dag = state.get("bundle_dag")
        if dag is None:
            # Cycles created before the additive DAG migration remain readable.
            return
        cycle_id = str(state.get("cycle_id") or "cycle")
        match = _CYCLE_RE.fullmatch(cycle_id)
        if match is None or not isinstance(dag, dict):
            raise CycleReplayError(
                f"done cycle {cycle_id} Bundle DAG 投影非法")
        ci = int(cycle_id[1:])
        fields = {
            "nodes", "dependencies", "source_requests", "worker_tasks",
            "resource_requests", "admissions", "terminal_reports",
            "skip_decisions", "worker_dispatches",
        }
        if any(not isinstance(dag.get(field), list) for field in fields):
            raise CycleReplayError(
                f"done cycle {cycle_id} Bundle DAG 闭包字段缺失")

        targets = state.get("targets")
        if not isinstance(targets, list):
            raise CycleReplayError(
                f"done cycle {cycle_id} target 投影非法")
        target_by_id: Dict[int, Mapping[str, Any]] = {}
        for target in targets:
            target_id = target.get("id") if isinstance(target, dict) else None
            if (isinstance(target_id, bool)
                    or not isinstance(target_id, int)
                    or target_id <= 0
                    or target_id in target_by_id):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target identity 重复/非法")
            target_by_id[target_id] = target

        node_by_id: Dict[int, Mapping[str, Any]] = {}
        id_by_key: Dict[str, int] = {}
        for node in dag["nodes"]:
            if not isinstance(node, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Bundle target node 非 object")
            target_id = node.get("target_id")
            target_key = node.get("target_key")
            if (isinstance(target_id, bool)
                    or not isinstance(target_id, int)
                    or target_id <= 0
                    or target_id in node_by_id
                    or node.get("cycle_id") != ci
                    or not isinstance(target_key, str)
                    or _SAFE_COMPONENT_RE.fullmatch(target_key) is None
                    or target_key in id_by_key
                    or not isinstance(node.get("declaration"), dict)):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Bundle target node 身份非法")
            node_by_id[target_id] = node
            id_by_key[target_key] = target_id
        if set(node_by_id) != set(target_by_id):
            raise CycleReplayError(
                f"done cycle {cycle_id} Bundle target nodes 不完整")

        expected_dependencies = set()
        expected_sources: Dict[Tuple[int, str], int] = {}
        expected_gpu: Dict[int, int] = {}
        for target_id, node in node_by_id.items():
            declaration = node["declaration"]
            target_key = node["target_key"]
            if declaration.get("target_key") != target_key:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "node/Plan target_key 冲突")
            depends_on = declaration.get("depends_on")
            source_inputs = declaration.get("published_source_inputs")
            gpu_count = declaration.get("gpu_count")
            if (not isinstance(depends_on, list)
                    or any(
                        not isinstance(key, str) or key not in id_by_key
                        for key in depends_on)
                    or len(depends_on) != len(set(depends_on))
                    or target_key in depends_on
                    or not isinstance(source_inputs, list)
                    or isinstance(gpu_count, bool)
                    or not isinstance(gpu_count, int)
                    or not 0 <= gpu_count <= 64):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "冻结 DAG declaration 非法")
            for upstream_key in depends_on:
                expected_dependencies.add(
                    (id_by_key[upstream_key], target_id))

            parent_key = declaration.get("parent_target_key")
            parent_ref = declaration.get("parent_baseline_ref")
            expected_parent_id = (
                None if parent_key is None else id_by_key.get(parent_key))
            if ((parent_key is not None and expected_parent_id is None)
                    or node.get("parent_target_id") != expected_parent_id
                    or node.get("parent_baseline_ref") != parent_ref):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "parent baseline closure 冲突")
            target = target_by_id[target_id]
            if target.get("target_kind") == "build":
                expected_domain_parent = None
                if expected_parent_id is not None:
                    expected_domain_parent = target_by_id[
                        expected_parent_id].get("baseline_id")
                elif parent_ref is not None:
                    if (
                        target.get("baseline_parent_id") is None
                        or target.get("baseline_parent_canonical_key")
                        != parent_ref
                    ):
                        raise CycleReplayError(
                            f"done cycle {cycle_id} target {target_id} "
                            "parent baseline_ref 无法解析")
                    expected_domain_parent = target.get(
                        "baseline_parent_id")
                if (
                    target.get("baseline_id") is None
                    or target.get("baseline_parent_id")
                    != expected_domain_parent
                ):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "领域 baseline parent closure 冲突")
            elif parent_key is not None or parent_ref is not None:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "非 build target 声明了 parent baseline")

            for source_input in source_inputs:
                if not isinstance(source_input, dict) or set(source_input) != {
                        "input_key", "target_key"}:
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "source declaration 非法")
                input_key = source_input["input_key"]
                upstream_key = source_input["target_key"]
                key = (target_id, input_key)
                if (not isinstance(input_key, str)
                        or _SAFE_COMPONENT_RE.fullmatch(input_key) is None
                        or upstream_key not in depends_on
                        or key in expected_sources):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "source declaration 身份非闭合")
                expected_sources[key] = id_by_key[upstream_key]
            expected_gpu[target_id] = gpu_count

        actual_dependencies = set()
        incoming: Dict[int, set[int]] = {
            target_id: set() for target_id in node_by_id}
        for edge in dag["dependencies"]:
            if not isinstance(edge, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} dependency edge 非 object")
            upstream = edge.get("upstream_target_id")
            downstream = edge.get("downstream_target_id")
            pair = (upstream, downstream)
            if (edge.get("cycle_id") != ci
                    or upstream not in node_by_id
                    or downstream not in node_by_id
                    or upstream == downstream
                    or pair in actual_dependencies):
                raise CycleReplayError(
                    f"done cycle {cycle_id} dependency edge 身份非法")
            actual_dependencies.add(pair)
            incoming[downstream].add(upstream)
        if actual_dependencies != expected_dependencies:
            raise CycleReplayError(
                f"done cycle {cycle_id} dependency edges 不完整/漂移")
        # The Plan declaration and the durable edge set must both remain a DAG.
        ready = sorted(
            target_id for target_id, parents in incoming.items()
            if not parents)
        visited = []
        while ready:
            target_id = ready.pop(0)
            visited.append(target_id)
            for downstream in sorted(incoming):
                if target_id in incoming[downstream]:
                    incoming[downstream].remove(target_id)
                    if (not incoming[downstream]
                            and downstream not in visited
                            and downstream not in ready):
                        ready.append(downstream)
                        ready.sort()
        if len(visited) != len(node_by_id):
            raise CycleReplayError(
                f"done cycle {cycle_id} dependency graph 含环")

        admission_by_target: Dict[int, Mapping[str, Any]] = {}
        for admission in dag["admissions"]:
            target_id = (
                admission.get("target_id")
                if isinstance(admission, dict) else None)
            if (target_id not in target_by_id
                    or admission.get("cycle_id") != ci
                    or target_id in admission_by_target):
                raise CycleReplayError(
                    f"done cycle {cycle_id} exact admission 身份非法")
            admission_by_target[target_id] = admission
        complete_targets = {
            target_id for target_id, target in target_by_id.items()
            if target.get("status") == "complete"
        }
        if set(admission_by_target) != complete_targets:
            raise CycleReplayError(
                f"done cycle {cycle_id} complete target exact admission 不完整")

        outgoing: Dict[int, set[int]] = {
            target_id: set() for target_id in node_by_id}
        for upstream, downstream in actual_dependencies:
            outgoing[upstream].add(downstream)
        dispatch_targets = set(admission_by_target)
        for task in dag["worker_tasks"]:
            if isinstance(task, dict) and isinstance(
                    task.get("target_id"), int):
                dispatch_targets.add(task["target_id"])
        for request in dag["source_requests"]:
            if (isinstance(request, dict)
                    and isinstance(
                        request.get("downstream_target_id"), int)
                    and request.get("binding") is not None):
                dispatch_targets.add(request["downstream_target_id"])
        for request in dag["resource_requests"]:
            if not isinstance(request, dict):
                continue
            target_id = request.get("target_id")
            leases = request.get("leases")
            if (isinstance(target_id, int)
                    and isinstance(leases, list) and leases):
                dispatch_targets.add(target_id)
        for target_id, target in target_by_id.items():
            if (target.get("run_ids")
                    or target.get("evaluation_attempt_ids")
                    or target.get("review_decision_ids")):
                dispatch_targets.add(target_id)

        dispatch_ids = set()
        purpose_pattern = re.compile(
            rf"bundle-worker-c{ci}-t([1-9][0-9]*)(?:-.+)?")
        for dispatch in dag["worker_dispatches"]:
            if not isinstance(dispatch, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Worker dispatch evidence 非法")
            runner_call_id = (
                dispatch.get("runner_call_id"))
            purpose = dispatch.get("purpose")
            purpose_match = (
                purpose_pattern.fullmatch(purpose)
                if isinstance(purpose, str) else None)
            target_id = (
                int(purpose_match.group(1))
                if purpose_match is not None else None)
            if (isinstance(runner_call_id, bool)
                    or not isinstance(runner_call_id, int)
                    or runner_call_id <= 0
                    or runner_call_id in dispatch_ids
                    or dispatch.get("cycle_id") != ci
                    or dispatch.get("phase") != "bundle"
                    or target_id not in target_by_id
                    or dispatch.get("target_id") != target_id
                    or dispatch.get("status")
                    not in {"created", "running", "success",
                            "failed", "aborted"}):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Worker dispatch evidence 非法")
            dispatch_ids.add(runner_call_id)
            dispatch_targets.add(target_id)

        proven_undispatched_skips = set()
        attributed_skips = set()
        skip_decision_ids = set()
        skip_roots = set()
        for decision in dag["skip_decisions"]:
            if not isinstance(decision, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} skip decision 非法")
            decision_id = (
                decision.get("id"))
            kind = decision.get("type")
            payload = decision.get("payload")
            if (isinstance(decision_id, bool)
                    or not isinstance(decision_id, int)
                    or decision_id <= 0
                    or decision_id in skip_decision_ids
                    or decision.get("actor") != "orchestrator"
                    or kind not in {
                        "bundle_descendant_skip",
                        "bundle_critical_early_exit",
                    }
                    or not isinstance(payload, dict)
                    or set(payload) != {
                        "failed_target_id", "failure_status",
                        "propagation", "skipped_target_ids",
                    }):
                raise CycleReplayError(
                    f"done cycle {cycle_id} skip decision 非法")
            failed_target_id = payload["failed_target_id"]
            skipped_target_ids = payload["skipped_target_ids"]
            failed = target_by_id.get(failed_target_id)
            decision_key = (kind, failed_target_id)
            if (isinstance(failed_target_id, bool)
                    or failed is None
                    or decision_key in skip_roots
                    or not isinstance(skipped_target_ids, list)
                    or any(
                        isinstance(item, bool) or not isinstance(item, int)
                        for item in skipped_target_ids)
                    or len(skipped_target_ids)
                    != len(set(skipped_target_ids))
                    or payload.get("failure_status")
                    != failed.get("status")
                    or failed_target_id in admission_by_target):
                raise CycleReplayError(
                    f"done cycle {cycle_id} skip root/targets 非法")
            skip_decision_ids.add(decision_id)
            skip_roots.add(decision_key)

            if kind == "bundle_descendant_skip":
                descendants = set()
                frontier = list(outgoing[failed_target_id])
                while frontier:
                    target_id = frontier.pop()
                    if target_id in descendants:
                        continue
                    descendants.add(target_id)
                    frontier.extend(outgoing[target_id])
                expected_skips = sorted(
                    descendants,
                    key=lambda item: (
                        target_by_id[item].get("seq"), item))
                if (failed.get("status") != "failed"
                        or bool(failed.get("critical"))
                        or payload.get("propagation") != "descendants"
                        or skipped_target_ids != expected_skips):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} descendant skip graph closure "
                        "冲突")
            else:
                expected_skips = sorted(
                    (
                        target_id
                        for target_id, target in target_by_id.items()
                        if target_id != failed_target_id
                        and target.get("status") == "skipped"
                    ),
                    key=lambda item: (
                        target_by_id[item].get("seq"), item))
                if (failed.get("status")
                        not in {"failed", "engineering_blocked"}
                        or (
                            failed.get("status") != "engineering_blocked"
                            and not bool(failed.get("critical"))
                        )
                        or payload.get("propagation")
                        != "critical_drain"
                        or skipped_target_ids != expected_skips):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} critical skip graph closure "
                        "冲突")

            if any(
                    target_by_id[target_id].get("status") != "skipped"
                    for target_id in skipped_target_ids):
                raise CycleReplayError(
                    f"done cycle {cycle_id} skip decision/target status 冲突")
            attributed_skips.update(skipped_target_ids)
            if kind == "bundle_descendant_skip":
                proven_undispatched_skips.update(
                    target_id for target_id in skipped_target_ids
                    if target_id not in dispatch_targets)

        skipped_targets = {
            target_id for target_id, target in target_by_id.items()
            if target.get("status") == "skipped"
        }
        if attributed_skips != skipped_targets:
            raise CycleReplayError(
                f"done cycle {cycle_id} skipped target 缺精确传播 decision")

        pool_decisions = state.get("pool_decisions")
        if not isinstance(pool_decisions, list):
            raise CycleReplayError(
                f"done cycle {cycle_id} publication decisions 投影非法")
        pool_by_id = {
            row.get("id"): row for row in pool_decisions
            if isinstance(row, dict)
        }
        for target_id, admission in admission_by_target.items():
            target = target_by_id[target_id]
            commits = target.get("phase_commits")
            exact_commit = (
                commits[0] if isinstance(commits, list)
                and len(commits) == 1 else None)
            decision = pool_by_id.get(admission.get(
                "publication_decision_id"))
            publication = (
                decision.get("payload")
                if isinstance(decision, dict) else None)
            expected_identity = (
                target.get("baseline_id"),
                target.get("variant_id"),
                target.get("evaluation_id"),
            )
            source_triple = (
                admission.get("source_ref"),
                admission.get("source_hash"),
                admission.get("source_hash_alg"),
            )
            if (exact_commit is None
                    or admission.get("phase_commit_id")
                    != exact_commit.get("id")
                    or admission.get("baseline_id") != expected_identity[0]
                    or admission.get("variant_id") != expected_identity[1]
                    or admission.get("evaluation_id") != expected_identity[2]
                    or admission.get("attempt_id")
                    not in target.get("evaluation_attempt_ids", [])
                    or not isinstance(decision, dict)
                    or decision.get("actor") != "gate"
                    or decision.get("type") != "pool_publication"
                    or not isinstance(publication, dict)
                    or publication.get("schema")
                    != "meta-research-pool-db-binding/v1"
                    or publication.get("manifest_ref")
                    != admission.get("manifest_ref")
                    or publication.get("manifest_hash")
                    != admission.get("manifest_hash")
                    or (
                        publication.get("baseline_id"),
                        publication.get("variant_id"),
                        publication.get("evaluation_id"),
                        publication.get("attempt_id"),
                    ) != (
                        admission.get("baseline_id"),
                        admission.get("variant_id"),
                        admission.get("evaluation_id"),
                        admission.get("attempt_id"),
                    )
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(admission.get("manifest_hash") or "")) is None
                    or not isinstance(source_triple[0], str)
                    or not source_triple[0]
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(source_triple[1] or "")) is None
                    or not isinstance(source_triple[2], str)
                    or not source_triple[2]):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "exact admission closure 冲突")

        actual_sources: Dict[Tuple[int, str], Mapping[str, Any]] = {}
        for request in dag["source_requests"]:
            if not isinstance(request, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} source request 非 object")
            downstream = request.get("downstream_target_id")
            upstream = request.get("upstream_target_id")
            input_key = request.get("input_key")
            key = (downstream, input_key)
            if (request.get("cycle_id") != ci
                    or downstream not in node_by_id
                    or upstream not in node_by_id
                    or not isinstance(input_key, str)
                    or key in actual_sources):
                raise CycleReplayError(
                    f"done cycle {cycle_id} source request 身份非法")
            actual_sources[key] = request
        if set(actual_sources) != set(expected_sources):
            raise CycleReplayError(
                f"done cycle {cycle_id} source input requests 不完整/漂移")
        for key, expected_upstream in expected_sources.items():
            request = actual_sources[key]
            binding = request.get("binding")
            downstream = key[0]
            upstream_admission = admission_by_target.get(expected_upstream)
            if (request.get("upstream_target_id") != expected_upstream):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {downstream} "
                    "source input binding 不完整/漂移")
            if binding is None and downstream in proven_undispatched_skips:
                continue
            if (not isinstance(binding, dict)
                    or upstream_admission is None
                    or binding.get("request_id") != request.get("id")
                    or binding.get("cycle_id") != ci
                    or binding.get("downstream_target_id") != downstream
                    or binding.get("input_key") != key[1]
                    or binding.get("upstream_target_id") != expected_upstream
                    or binding.get("upstream_admission_id")
                    != upstream_admission.get("id")
                    or binding.get("publication_decision_id")
                    != upstream_admission.get("publication_decision_id")
                    or binding.get("manifest_ref")
                    != upstream_admission.get("manifest_ref")
                    or binding.get("manifest_hash")
                    != upstream_admission.get("manifest_hash")
                    or binding.get("source_ref")
                    != upstream_admission.get("source_ref")
                    or binding.get("source_hash")
                    != upstream_admission.get("source_hash")
                    or binding.get("source_hash_alg")
                    != upstream_admission.get("source_hash_alg")):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {downstream} "
                    "source input binding 不完整/漂移")

        tasks_by_target: Dict[int, Dict[str, Mapping[str, Any]]] = {
            target_id: {} for target_id in target_by_id}
        provider_ids = set()
        for task in dag["worker_tasks"]:
            if not isinstance(task, dict):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Worker task 非 object")
            target_id = task.get("target_id")
            role = task.get("role")
            provider_id = task.get("provider_task_id")
            receipt_ref = task.get("receipt_ref")
            if (target_id not in target_by_id
                    or task.get("cycle_id") != ci
                    or role not in {"worker", "code_review", "result_review"}
                    or role in tasks_by_target[target_id]
                    or not isinstance(provider_id, str)
                    or not provider_id
                    or len(provider_id) > 4096
                    or provider_id in provider_ids
                    or not isinstance(receipt_ref, str)
                    or not receipt_ref
                    or len(receipt_ref) > 4096
                    or task.get("status")
                    not in {"completed", "failed", "cancelled"}):
                raise CycleReplayError(
                    f"done cycle {cycle_id} Worker task/receipt identity 非法")
            provider_ids.add(provider_id)
            tasks_by_target[target_id][role] = task
        for target_id, target in target_by_id.items():
            worker = tasks_by_target[target_id].get("worker")
            if (worker is None
                    and target_id in proven_undispatched_skips):
                continue
            if worker is None:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "缺 Worker task/receipt identity")
            if target.get("status") == "complete":
                expected_roles = {"worker", "code_review", "result_review"}
                if (set(tasks_by_target[target_id]) != expected_roles
                        or any(
                            task.get("status") != "completed"
                            for task in tasks_by_target[target_id].values())):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "Worker/review task 终态不闭合")

        review_decisions = state.get("review_decisions")
        review_by_id = {
            row.get("id"): row for row in review_decisions
            if isinstance(row, dict)
        } if isinstance(review_decisions, list) else {}
        for target_id in complete_targets:
            target = target_by_id[target_id]
            decision_ids = target.get("review_decision_ids")
            rows = [
                review_by_id.get(decision_id)
                for decision_id in decision_ids
            ] if isinstance(decision_ids, list) else []
            passed = {
                row.get("type")
                for row in rows
                if (isinstance(row, dict)
                    and row.get("actor") == "judge"
                    and isinstance(row.get("payload"), dict)
                    and str(row["payload"].get("build_target_id"))
                    == str(target_id)
                    and row["payload"].get("verdict") == "pass")
            }
            if not {
                    "bundle_code_review",
                    "bundle_result_review",
                    }.issubset(passed):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "code/result review closure 不完整")

        resource_by_target: Dict[int, Mapping[str, Any]] = {}
        for request in dag["resource_requests"]:
            target_id = (
                request.get("target_id")
                if isinstance(request, dict) else None)
            if (target_id not in target_by_id
                    or request.get("cycle_id") != ci
                    or target_id in resource_by_target
                    or request.get("gpu_count") != expected_gpu[target_id]
                    or isinstance(request.get("worker_slots"), bool)
                    or not isinstance(request.get("worker_slots"), int)
                    or request.get("worker_slots") < 1
                    or not isinstance(request.get("leases"), list)):
                raise CycleReplayError(
                    f"done cycle {cycle_id} GPU resource request 非法/漂移")
            resource_by_target[target_id] = request
        if set(resource_by_target) != set(target_by_id):
            raise CycleReplayError(
                f"done cycle {cycle_id} GPU resource requests 不完整")
        for target_id, request in resource_by_target.items():
            leases = request["leases"]
            gpu_count = expected_gpu[target_id]
            expected_lease_count = (
                0 if target_id in proven_undispatched_skips
                else gpu_count)
            if len(leases) != expected_lease_count:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "GPU lease closure 数量不完整")
            keys = set()
            hashes = set()
            for lease in leases:
                resource_key = (
                    lease.get("resource_key")
                    if isinstance(lease, dict) else None)
                contract_hash = (
                    lease.get("contract_hash")
                    if isinstance(lease, dict) else None)
                if (not isinstance(lease, dict)
                        or lease.get("target_id") != target_id
                        or lease.get("cycle_id") != ci
                        or lease.get("resource_kind") != "gpu"
                        or not isinstance(resource_key, str)
                        or not resource_key
                        or resource_key in keys
                        or not isinstance(contract_hash, str)
                        or not contract_hash
                        or lease.get("status") != "released"
                        or not isinstance(lease.get("released_at"), str)
                        or not lease.get("released_at")
                        or not isinstance(
                            lease.get("guardian_receipt_ref"), str)
                        or not lease.get("guardian_receipt_ref")):
                    raise CycleReplayError(
                        f"done cycle {cycle_id} target {target_id} "
                        "GPU lease/guardian closure 非法")
                keys.add(resource_key)
                hashes.add(contract_hash)
            if len(hashes) > 1:
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "GPU lease contract hash 不一致")

        reports_by_target: Dict[int, Mapping[str, Any]] = {}
        for report in dag["terminal_reports"]:
            target_id = (
                report.get("target_id")
                if isinstance(report, dict) else None)
            if (target_id not in target_by_id
                    or report.get("cycle_id") != ci
                    or target_id in reports_by_target):
                raise CycleReplayError(
                    f"done cycle {cycle_id} terminal report 身份非法")
            reports_by_target[target_id] = report
        if set(reports_by_target) != set(target_by_id):
            raise CycleReplayError(
                f"done cycle {cycle_id} required terminal reports 不完整")
        report_statuses = {
            "complete": {"complete"},
            "failed": {"failed", "replan_required"},
            "skipped": {"skipped"},
            "engineering_blocked": {"failed", "replan_required"},
        }
        summary_fields = {
            "protocol", "target_kind", "seq", "critical", "failure_kind",
            "metric_result_ids", "admitted",
        }
        for target_id, report in reports_by_target.items():
            target = target_by_id[target_id]
            summary = report.get("summary")
            metric_ids = (
                summary.get("metric_result_ids")
                if isinstance(summary, dict) else None)
            if (not isinstance(report.get("report_ref"), str)
                    or not report.get("report_ref")
                    or len(report["report_ref"]) > 4096
                    or re.fullmatch(
                        r"[0-9a-f]{64}",
                        str(report.get("report_hash") or "")) is None
                    or report.get("status")
                    not in report_statuses[target["status"]]
                    or not isinstance(summary, dict)
                    or set(summary) != summary_fields
                    or summary.get("protocol")
                    != "bundle-target-terminal-report-v1"
                    or summary.get("target_kind")
                    != target.get("target_kind")
                    or summary.get("seq") != target.get("seq")
                    or summary.get("critical") is not bool(
                        target.get("critical"))
                    or summary.get("failure_kind")
                    != target.get("failure_kind")
                    or not isinstance(metric_ids, list)
                    or len(metric_ids) > 128
                    or any(
                        isinstance(item, bool) or not isinstance(item, int)
                        or item <= 0 for item in metric_ids)
                    or summary.get("admitted")
                    is not (target_id in admission_by_target)
                    or len(json.dumps(
                        summary, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False
                    ).encode("utf-8")) > 128 * 1024):
                raise CycleReplayError(
                    f"done cycle {cycle_id} target {target_id} "
                    "terminal report closure 非法/非有界")

    @staticmethod
    def _state_decision(
            row: tuple, *, cycle_id: str) -> Dict[str, Any]:
        decision_id, question_id, actor, kind, raw, created_at = row
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as error:
            raise CycleReplayError(
                f"cycle {cycle_id} decision #{decision_id} ({kind}) JSON 损坏"
            ) from error
        if not isinstance(payload, dict):
            raise CycleReplayError(
                f"cycle {cycle_id} decision #{decision_id} ({kind}) "
                "payload 非 object")
        return {
            "id": int(decision_id),
            "question_id": question_id,
            "actor": actor,
            "type": kind,
            "created_at": created_at,
            "payload": payload,
        }

    @staticmethod
    def _decision_targets(
            decision: Mapping[str, Any], targets: Iterable[Mapping[str, Any]],
            *, category: str) -> List[int]:
        payload = decision["payload"]
        matched: List[int] = []
        for target in targets:
            target_id = target["id"]
            explicit = (
                payload.get("build_target_id")
                if payload.get("build_target_id") is not None
                else payload.get("target_id"))
            if explicit is not None:
                if str(explicit) == str(target_id):
                    matched.append(target_id)
                continue
            if category != "pool":
                continue
            # Pool decisions may carry broad baseline/variant identities as
            # provenance in addition to the actual attempt/evaluation/run
            # owner.  Once a stronger key is present, a mismatch must not fall
            # back to a shared broad key and attach the publication twice.
            owner_levels = (
                (("evaluation_attempt_id", "attempt_id"),
                 "evaluation_attempt_ids", None),
                (("evaluation_id",), "evaluation_ids", "evaluation_id"),
                (("run_id",), "run_ids", None),
                (("variant_id",), None, "variant_id"),
                (("baseline_id",), None, "baseline_id"),
            )
            for payload_fields, list_field, scalar_field in owner_levels:
                supplied = next((
                    payload.get(field) for field in payload_fields
                    if payload.get(field) is not None), None)
                if supplied is None:
                    continue
                owners = list(target.get(list_field) or []) if list_field else []
                if scalar_field and target.get(scalar_field) is not None:
                    owners.append(target[scalar_field])
                if any(str(supplied) == str(owner) for owner in owners):
                    matched.append(target_id)
                break
        return matched

    @staticmethod
    def _dag_plan_declaration(
            raw: Optional[str], *, cycle_id: str,
            target_id: int) -> Dict[str, Any]:
        """Extract only graph/resource facts from one frozen Plan slice."""
        try:
            plan = json.loads(raw) if isinstance(raw, str) else None
        except json.JSONDecodeError as error:
            raise CycleReplayError(
                f"cycle {cycle_id} target {target_id} plan_ref JSON 损坏"
            ) from error
        if not isinstance(plan, dict):
            raise CycleReplayError(
                f"cycle {cycle_id} target {target_id} 缺冻结 Plan declaration")

        target_key = plan.get("target_key")
        if (not isinstance(target_key, str)
                or _SAFE_COMPONENT_RE.fullmatch(target_key) is None):
            raise CycleReplayError(
                f"cycle {cycle_id} target {target_id} target_key 非法")

        depends_on = plan.get("depends_on", [])
        if (not isinstance(depends_on, list)
                or any(
                    not isinstance(item, str)
                    or _SAFE_COMPONENT_RE.fullmatch(item) is None
                    for item in depends_on)
                or len(depends_on) != len(set(depends_on))):
            raise CycleReplayError(
                f"cycle {cycle_id} target {target_id} depends_on 非法")

        parent_target_key = None
        parent_baseline_ref = None
        parent = plan.get("parent_baseline")
        if parent is not None:
            if not isinstance(parent, dict):
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} "
                    "parent_baseline 非法")
            if set(parent) == {"target_key"}:
                parent_target_key = parent["target_key"]
                if (not isinstance(parent_target_key, str)
                        or _SAFE_COMPONENT_RE.fullmatch(
                            parent_target_key) is None
                        or parent_target_key not in depends_on):
                    raise CycleReplayError(
                        f"cycle {cycle_id} target {target_id} "
                        "parent target 非法")
            elif set(parent) == {"baseline_ref"}:
                parent_baseline_ref = parent["baseline_ref"]
                if (not isinstance(parent_baseline_ref, str)
                        or not parent_baseline_ref
                        or len(parent_baseline_ref) > 4096):
                    raise CycleReplayError(
                        f"cycle {cycle_id} target {target_id} "
                        "parent baseline ref 非法")
            else:
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} "
                    "parent_baseline 身份非闭合")

        source_inputs = plan.get("published_source_inputs", [])
        if not isinstance(source_inputs, list):
            raise CycleReplayError(
                f"cycle {cycle_id} target {target_id} "
                "published_source_inputs 非数组")
        normalized_sources = []
        input_keys = set()
        for item in source_inputs:
            if not isinstance(item, dict) or set(item) != {
                    "input_key", "target_key"}:
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} source input 非法")
            input_key, upstream_key = (
                item["input_key"], item["target_key"])
            if (not isinstance(input_key, str)
                    or _SAFE_COMPONENT_RE.fullmatch(input_key) is None
                    or input_key in input_keys
                    or not isinstance(upstream_key, str)
                    or _SAFE_COMPONENT_RE.fullmatch(upstream_key) is None
                    or upstream_key not in depends_on):
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} "
                    "source input 身份非闭合")
            input_keys.add(input_key)
            normalized_sources.append({
                "input_key": input_key,
                "target_key": upstream_key,
            })

        resources = plan.get("resources")
        if resources is None:
            legacy_gpu = plan.get("gpu_required", False)
            if not isinstance(legacy_gpu, bool):
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} "
                    "gpu_required 非 bool")
            gpu_count = 1 if legacy_gpu else 0
        else:
            if not isinstance(resources, dict) or set(resources) != {
                    "gpu_count"}:
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} "
                    "resources 非精确 gpu_count declaration")
            gpu_count = resources["gpu_count"]
            if (isinstance(gpu_count, bool)
                    or not isinstance(gpu_count, int)
                    or not 0 <= gpu_count <= 64):
                raise CycleReplayError(
                    f"cycle {cycle_id} target {target_id} gpu_count 非法")

        return {
            "target_key": target_key,
            "depends_on": list(depends_on),
            "parent_target_key": parent_target_key,
            "parent_baseline_ref": parent_baseline_ref,
            "published_source_inputs": normalized_sources,
            "gpu_count": gpu_count,
        }

    def _bundle_dag_projection(
            self, source, *, cycle_id: str,
            targets: List[Mapping[str, Any]],
            plan_ref_by_target: Mapping[int, Optional[str]]
            ) -> Optional[Dict[str, Any]]:
        """Return the complete bounded v2 Bundle graph closure, if present."""
        table = self._query_one(
            source,
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='bundle_target_node'")
        if table is None:
            return None
        ci = int(cycle_id[1:])

        node_rows = self._query(
            source,
            "SELECT target_id,cycle_id,target_key,parent_target_id,"
            "parent_baseline_ref,registered_at "
            "FROM bundle_target_node WHERE cycle_id=? "
            "ORDER BY target_id",
            (ci,))
        nodes = []
        for row in node_rows:
            target_id = int(row[0])
            nodes.append({
                "target_id": target_id,
                "cycle_id": int(row[1]),
                "target_key": row[2],
                "parent_target_id": row[3],
                "parent_baseline_ref": row[4],
                "registered_at": row[5],
                "declaration": self._dag_plan_declaration(
                    plan_ref_by_target.get(target_id),
                    cycle_id=cycle_id,
                    target_id=target_id),
            })

        dependencies = [{
            "id": int(row[0]),
            "cycle_id": int(row[1]),
            "upstream_target_id": int(row[2]),
            "downstream_target_id": int(row[3]),
            "created_at": row[4],
        } for row in self._query(
            source,
            "SELECT id,cycle_id,upstream_target_id,downstream_target_id,"
            "created_at FROM bundle_target_dependency WHERE cycle_id=? "
            "ORDER BY downstream_target_id,upstream_target_id,id",
            (ci,))]

        binding_by_request: Dict[int, Dict[str, Any]] = {}
        for row in self._query(
                source,
                "SELECT id,request_id,cycle_id,downstream_target_id,input_key,"
                "upstream_target_id,upstream_admission_id,"
                "publication_decision_id,manifest_ref,manifest_hash,"
                "source_ref,source_hash,source_hash_alg,bound_at "
                "FROM bundle_source_binding WHERE cycle_id=? "
                "ORDER BY downstream_target_id,input_key,id",
                (ci,)):
            request_id = int(row[1])
            if request_id in binding_by_request:
                raise CycleReplayError(
                    f"cycle {cycle_id} source request {request_id} "
                    "存在多个 binding")
            binding_by_request[request_id] = {
                "id": int(row[0]),
                "request_id": request_id,
                "cycle_id": int(row[2]),
                "downstream_target_id": int(row[3]),
                "input_key": row[4],
                "upstream_target_id": int(row[5]),
                "upstream_admission_id": int(row[6]),
                "publication_decision_id": int(row[7]),
                "manifest_ref": row[8],
                "manifest_hash": row[9],
                "source_ref": row[10],
                "source_hash": row[11],
                "source_hash_alg": row[12],
                "bound_at": row[13],
            }
        source_requests = []
        for row in self._query(
                source,
                "SELECT id,cycle_id,downstream_target_id,input_key,"
                "upstream_target_id,created_at "
                "FROM bundle_source_request WHERE cycle_id=? "
                "ORDER BY downstream_target_id,input_key,id",
                (ci,)):
            request_id = int(row[0])
            source_requests.append({
                "id": request_id,
                "cycle_id": int(row[1]),
                "downstream_target_id": int(row[2]),
                "input_key": row[3],
                "upstream_target_id": int(row[4]),
                "created_at": row[5],
                "binding": binding_by_request.pop(request_id, None),
            })
        if binding_by_request:
            raise CycleReplayError(
                f"cycle {cycle_id} 存在无 request 的 source binding")

        admissions = [{
            "id": int(row[0]),
            "target_id": int(row[1]),
            "cycle_id": int(row[2]),
            "phase_commit_id": int(row[3]),
            "publication_decision_id": int(row[4]),
            "manifest_ref": row[5],
            "manifest_hash": row[6],
            "baseline_id": int(row[7]),
            "variant_id": int(row[8]),
            "evaluation_id": int(row[9]),
            "attempt_id": int(row[10]),
            "source_ref": row[11],
            "source_hash": row[12],
            "source_hash_alg": row[13],
            "admitted_at": row[14],
        } for row in self._query(
            source,
            "SELECT id,target_id,cycle_id,phase_commit_id,"
            "publication_decision_id,manifest_ref,manifest_hash,"
            "baseline_id,variant_id,evaluation_id,attempt_id,"
            "source_ref,source_hash,source_hash_alg,admitted_at "
            "FROM bundle_target_admission WHERE cycle_id=? "
            "ORDER BY target_id,id",
            (ci,))]

        worker_tasks = [{
            "id": int(row[0]),
            "target_id": int(row[1]),
            "cycle_id": int(row[2]),
            "role": row[3],
            "provider_task_id": row[4],
            "status": row[5],
            "receipt_ref": row[6],
            "created_at": row[7],
            "updated_at": row[8],
        } for row in self._query(
            source,
            "SELECT id,build_target_id,cycle_id,role,provider_task_id,"
            "status,receipt_ref,created_at,updated_at "
            "FROM bundle_worker_task WHERE cycle_id=? "
            "ORDER BY build_target_id,role,id",
            (ci,))]

        leases_by_target: Dict[int, List[Dict[str, Any]]] = {}
        for row in self._query(
                source,
                "SELECT id,build_target_id,cycle_id,resource_kind,"
                "resource_key,contract_hash,status,acquired_at,released_at,"
                "guardian_receipt_ref "
                "FROM bundle_resource_lease WHERE cycle_id=? "
                "ORDER BY build_target_id,resource_key,id",
                (ci,)):
            target_id = int(row[1])
            leases_by_target.setdefault(target_id, []).append({
                "id": int(row[0]),
                "target_id": target_id,
                "cycle_id": int(row[2]),
                "resource_kind": row[3],
                "resource_key": row[4],
                "contract_hash": row[5],
                "status": row[6],
                "acquired_at": row[7],
                "released_at": row[8],
                "guardian_receipt_ref": row[9],
            })
        resource_requests = []
        for row in self._query(
                source,
                "SELECT build_target_id,cycle_id,gpu_count,worker_slots,"
                "created_at FROM bundle_resource_request WHERE cycle_id=? "
                "ORDER BY build_target_id",
                (ci,)):
            target_id = int(row[0])
            resource_requests.append({
                "target_id": target_id,
                "cycle_id": int(row[1]),
                "gpu_count": int(row[2]),
                "worker_slots": int(row[3]),
                "created_at": row[4],
                "leases": leases_by_target.pop(target_id, []),
            })
        if leases_by_target:
            raise CycleReplayError(
                f"cycle {cycle_id} 存在无 resource request 的 lease")

        terminal_reports = []
        for row in self._query(
                source,
                "SELECT build_target_id,cycle_id,report_ref,report_hash,"
                "status,summary_json,created_at "
                "FROM bundle_terminal_report WHERE cycle_id=? "
                "ORDER BY build_target_id",
                (ci,)):
            try:
                summary = json.loads(row[5])
            except (TypeError, json.JSONDecodeError) as error:
                raise CycleReplayError(
                    f"cycle {cycle_id} target {row[0]} "
                    "terminal report summary JSON 损坏") from error
            if not isinstance(summary, dict):
                raise CycleReplayError(
                    f"cycle {cycle_id} target {row[0]} "
                    "terminal report summary 非 object")
            terminal_reports.append({
                "target_id": int(row[0]),
                "cycle_id": int(row[1]),
                "report_ref": row[2],
                "report_hash": row[3],
                "status": row[4],
                "summary": summary,
                "created_at": row[6],
            })

        skip_decisions = [
            self._state_decision(row, cycle_id=cycle_id)
            for row in self._query(
                source,
                "SELECT id,question_id,actor,type,payload_json,created_at "
                "FROM decision WHERE cycle_id=? AND type IN ("
                "'bundle_descendant_skip','bundle_critical_early_exit'"
                ") ORDER BY id",
                (ci,))
        ]

        # A target may have arbitrarily many repair turns.  Replay only needs
        # bounded proof that dispatch happened, so retain one representative
        # runner_call per graph node and reject every malformed/unknown Worker
        # purpose separately.
        worker_dispatches = []
        valid_purpose_clauses = []
        valid_purpose_params: List[str] = []
        for node in nodes:
            target_id = int(node["target_id"])
            base = f"bundle-worker-c{ci}-t{target_id}"
            suffix_glob = base + "-?*"
            valid_purpose_clauses.append(
                "(purpose=? OR purpose GLOB ?)")
            valid_purpose_params.extend((base, suffix_glob))
            row = self._query_one(
                source,
                "SELECT id,cycle_id,phase,purpose,status "
                "FROM runner_call WHERE cycle_id=? AND phase='bundle' "
                "AND (purpose=? OR purpose GLOB ?) ORDER BY id LIMIT 1",
                (ci, base, suffix_glob))
            if row is not None:
                worker_dispatches.append({
                    "runner_call_id": int(row[0]),
                    "cycle_id": int(row[1]),
                    "phase": row[2],
                    "purpose": row[3],
                    "status": row[4],
                    "target_id": target_id,
                })
        if valid_purpose_clauses:
            malformed = self._query_one(
                source,
                "SELECT id,purpose FROM runner_call "
                "WHERE cycle_id=? AND phase='bundle' AND purpose GLOB ? "
                "AND NOT (" + " OR ".join(valid_purpose_clauses) + ") "
                "ORDER BY id LIMIT 1",
                (
                    ci, f"bundle-worker-c{ci}-t*",
                    *valid_purpose_params,
                ))
            if malformed is not None:
                raise CycleReplayError(
                    f"cycle {cycle_id} Worker dispatch purpose 非法: "
                    f"rc{malformed[0]}")

        has_dag_facts = any((
            nodes, dependencies, source_requests, admissions, worker_tasks,
            resource_requests, terminal_reports, skip_decisions,
            worker_dispatches,
        ))
        if not has_dag_facts:
            return None
        return {
            "nodes": nodes,
            "dependencies": dependencies,
            "source_requests": source_requests,
            "worker_tasks": worker_tasks,
            "resource_requests": resource_requests,
            "admissions": admissions,
            "terminal_reports": terminal_reports,
            "skip_decisions": skip_decisions,
            "worker_dispatches": worker_dispatches,
        }

    def _cycle_state_projection(
            self, source, *, cycle_id: str) -> Dict[str, Any]:
        """Project terminal SQLite facts without inventing scientific prose."""
        ci = int(cycle_id[1:])
        cycle = self._query_one(
            source,
            "SELECT status,route,active_question_id,failure_kind,"
            "next_question_id,next_intent,started_at,finished_at "
            "FROM cycle WHERE id=?",
            (ci,))
        if cycle is None:
            raise CycleReplayError(f"cycle {cycle_id} SQLite 行缺失")
        target_rows = self._query(
            source,
            "SELECT id,question_id,target_kind,seq,critical,status,"
            "failure_kind,baseline_id,variant_id,evaluation_id,eval_action,"
            "attempt_purpose,evaluation_source,eval_key,plan_ref,"
            "(SELECT parent_id FROM baseline "
            " WHERE baseline.id=build_target.baseline_id),"
            "(SELECT canonical_key FROM baseline "
            " WHERE baseline.id=build_target.baseline_id),"
            "(SELECT parent.canonical_key FROM baseline child "
            " JOIN baseline parent ON parent.id=child.parent_id "
            " WHERE child.id=build_target.baseline_id) "
            "FROM build_target WHERE cycle_id=? ORDER BY seq,id",
            (ci,))
        targets: List[Dict[str, Any]] = []
        target_by_id: Dict[int, Dict[str, Any]] = {}
        plan_ref_by_target: Dict[int, Optional[str]] = {}
        fields = (
            "id", "question_id", "target_kind", "seq", "critical", "status",
            "failure_kind", "baseline_id", "variant_id", "evaluation_id",
            "eval_action", "attempt_purpose", "evaluation_source", "eval_key",
            "plan_ref", "baseline_parent_id", "baseline_canonical_key",
            "baseline_parent_canonical_key",
        )
        for row in target_rows:
            target = dict(zip(fields, row))
            target["id"] = int(target["id"])
            plan_ref_by_target[target["id"]] = target.pop("plan_ref")
            target["phase_commits"] = []
            target["scientific_decision_ids"] = []
            target["review_decision_ids"] = []
            target["pool_decision_ids"] = []
            target["run_ids"] = []
            target["evaluation_ids"] = (
                [] if target.get("evaluation_id") is None
                else [target["evaluation_id"]])
            target["evaluation_attempt_ids"] = []
            targets.append(target)
            target_by_id[target["id"]] = target

        for run_id, target_id in self._query(
                source,
                "SELECT r.id,r.build_target_id FROM run r "
                "JOIN build_target bt ON bt.id=r.build_target_id "
                "WHERE bt.cycle_id=? ORDER BY r.id",
                (ci,)):
            if target_id in target_by_id:
                target_by_id[target_id]["run_ids"].append(int(run_id))
        for evaluation_id, target_id in self._query(
                source,
                "SELECT e.id,e.build_target_id FROM evaluation e "
                "JOIN build_target bt ON bt.id=e.build_target_id "
                "WHERE bt.cycle_id=? ORDER BY e.id",
                (ci,)):
            if target_id in target_by_id:
                values = target_by_id[target_id]["evaluation_ids"]
                if evaluation_id not in values:
                    values.append(int(evaluation_id))
        for attempt_id, evaluation_id, target_id in self._query(
                source,
                "SELECT ea.id,ea.evaluation_id,ea.build_target_id "
                "FROM evaluation_attempt ea "
                "JOIN build_target bt ON bt.id=ea.build_target_id "
                "WHERE bt.cycle_id=? ORDER BY ea.id",
                (ci,)):
            if target_id in target_by_id:
                target = target_by_id[target_id]
                target["evaluation_attempt_ids"].append(int(attempt_id))
                if evaluation_id not in target["evaluation_ids"]:
                    target["evaluation_ids"].append(int(evaluation_id))

        commit_rows = self._query(
            source,
            "SELECT id,stage,target_id,artifact_hash,committed_at "
            "FROM phase_commit WHERE cycle_id=? ORDER BY id",
            (ci,))
        commits = [{
            "id": int(row[0]), "stage": row[1], "target_id": row[2],
            "artifact_hash": row[3], "committed_at": row[4],
        } for row in commit_rows]
        for commit in commits:
            target_id = commit["target_id"]
            if commit["stage"] == "bundle" and target_id in target_by_id:
                target_by_id[target_id]["phase_commits"].append(commit)

        placeholders = ",".join("?" for _ in sorted(_STATE_DECISION_TYPES))
        decision_rows = self._query(
            source,
            "SELECT id,question_id,actor,type,payload_json,created_at "
            f"FROM decision WHERE cycle_id=? AND type IN ({placeholders}) "
            "ORDER BY id",
            (ci, *sorted(_STATE_DECISION_TYPES)))
        decisions = [
            self._state_decision(row, cycle_id=cycle_id)
            for row in decision_rows
        ]
        scientific = [
            item for item in decisions
            if item["type"] in _SCIENTIFIC_DECISION_TYPES
        ]
        reviews = [
            item for item in decisions
            if item["type"] in _REVIEW_DECISION_TYPES
        ]
        pools = [
            item for item in decisions
            if item["type"] in _POOL_DECISION_TYPES
        ]
        summaries = [
            item for item in decisions
            if item["type"] == "runtime_cycle_summary"
        ]
        for category, rows in (
                ("scientific", scientific),
                ("review", reviews),
                ("pool", pools)):
            field = f"{category}_decision_ids"
            for decision in rows:
                for target_id in self._decision_targets(
                        decision, targets, category=category):
                    target_by_id[target_id][field].append(decision["id"])

        reasoning_event = self._latest_stage_event(
            self.cycle_dir(cycle_id), "reasoning")
        restore_provenance = self._restore_provenance(cycle_id)
        bundle_dag = self._bundle_dag_projection(
            source,
            cycle_id=cycle_id,
            targets=targets,
            plan_ref_by_target=plan_ref_by_target,
        )
        return {
            "schema": STATE_SCHEMA,
            "cycle_id": cycle_id,
            "cycle": {
                "status": cycle[0], "route": cycle[1],
                "active_question_id": cycle[2],
                "failure_kind": cycle[3],
                "next_question_id": cycle[4],
                "next_intent": cycle[5],
                "started_at": cycle[6],
                "finished_at": cycle[7],
            },
            "targets": targets,
            "phase_commits": commits,
            "scientific_decisions": scientific,
            "review_decisions": reviews,
            "pool_decisions": pools,
            "bundle_dag": bundle_dag,
            "restore_provenance": restore_provenance,
            "reasoning": {
                "availability": (
                    "archived" if reasoning_event is not None
                    else "not_restored_sqlite_truth_only"
                    if restore_provenance is not None
                    else "missing"),
                "stage_event_id": (
                    reasoning_event[0] if reasoning_event is not None else None),
                "phase_commits": [
                    item for item in commits
                    if item["stage"] == "reasoning"
                    and item["target_id"] is None
                ],
                "summary_decisions": summaries,
            },
        }

    @staticmethod
    def _write_compatibility_aliases(cycle: Path, *, stage: str, target: Optional[str],
                                     purpose: str,
                                     encoded: Iterable[Tuple[PurePosixPath, bytes]],
                                     md_raw: bytes) -> None:
        artifacts = cycle / "artifacts"
        # The flat aliases follow the original StubGate layout and make the
        # common stage contract obvious to operators.  Nested bundle source is
        # kept only in by-stage/history to avoid cross-target path collisions.
        if purpose != stage:
            return
        for rel, raw in encoded:
            if len(rel.parts) != 1:
                continue
            name = f"{target}.{rel.name}" if target else rel.name
            _atomic_write(artifacts / name, raw)
        md_name = f"{target}.{stage}.md" if target else f"{stage}.md"
        _atomic_write(artifacts / md_name, md_raw)

    @staticmethod
    def _assert_no_symlink_descendant(
            root: Path, path: Path, *, label: str) -> None:
        """Reject every symlink/non-directory component below a trusted root."""
        try:
            relative = path.relative_to(root)
        except ValueError as error:
            raise CycleReplayError(f"{label} 越界: {path}") from error
        try:
            root_info = root.lstat()
        except OSError as error:
            raise CycleReplayError(f"{label} 根不可读取: {root}") from error
        if not stat.S_ISDIR(root_info.st_mode) or stat.S_ISLNK(root_info.st_mode):
            raise CycleReplayError(f"{label} 根不是 no-follow 目录: {root}")
        current = root
        for index, part in enumerate(relative.parts):
            current = current / part
            try:
                info = current.lstat()
            except OSError as error:
                raise CycleReplayError(f"{label} 路径不可读取: {current}") from error
            final = index == len(relative.parts) - 1
            if stat.S_ISLNK(info.st_mode):
                raise CycleReplayError(f"{label} 路径含 symlink: {current}")
            if final:
                if not stat.S_ISREG(info.st_mode):
                    raise CycleReplayError(f"{label} 终点不是常规文件: {current}")
            elif not stat.S_ISDIR(info.st_mode):
                raise CycleReplayError(f"{label} 父级不是目录: {current}")

    def _registered_runtime_submission(
            self, *, cycle_id: str, stage: str, target_id: Optional[str],
            purpose: str, pack_hash: Optional[str], runner_call_id: Optional[int],
            logical: PurePosixPath, ref: ManagedArtifactRef,
            submission_ref: Optional[str],
            submission_artifact_hash: Optional[str]) -> str:
        """Prove one managed ref belongs to an MCP-accepted quest submission.

        Merely living below ``runtime/stage-submissions`` is insufficient.  The
        immutable receipt must be indexed by this quest's SQL writer, hash to
        the indexed identity, bind the exact stage/target/ContextPack/call, and
        list this logical managed file with the same path+hash+size.
        """
        if self.submission_registry is None:
            raise CycleReplayError(
                "runtime 托管阶段产物缺 quest-local MCP 提交登记权威")
        if not isinstance(submission_ref, str) or not submission_ref:
            raise CycleReplayError("runtime 托管阶段产物缺 MCP submission_ref")
        try:
            artifact_hash = normalize_sha256(
                submission_artifact_hash,
                field="stage_submission_artifact_hash")
        except ArtifactCapabilityError as error:
            raise CycleReplayError("runtime 托管阶段产物缺合法 artifact hash") from error

        receipt_path = Path(submission_ref)
        if (not receipt_path.is_absolute()
                or receipt_path != Path(os.path.normpath(submission_ref))):
            raise CycleReplayError("MCP stage submission receipt 须为规范绝对路径")
        storage_root = self.work_root / "runtime" / "stage-submissions"
        expected_target = f"t{target_id}" if target_id is not None else "stage"
        try:
            receipt_relative = receipt_path.relative_to(storage_root)
        except ValueError as error:
            raise CycleReplayError(
                "MCP stage submission receipt 逃逸当前 quest file-manager") from error
        if (len(receipt_relative.parts) != 5
                or receipt_relative.parts[:3] != (cycle_id, stage, expected_target)
                or _SAFE_COMPONENT_RE.fullmatch(receipt_relative.parts[3]) is None
                or receipt_relative.parts[4] != "submission.json"):
            raise CycleReplayError(
                f"MCP stage submission receipt 路径布局与阶段身份不符: {receipt_path}")
        self._assert_no_symlink_descendant(
            self.work_root, receipt_path, label="MCP stage submission receipt")

        cycle_no = int(cycle_id[1:])
        rows = self._query(
            self.submission_registry,
            "SELECT json_extract(payload_json,'$.submission_hash'),"
            "json_extract(payload_json,'$.artifact_hash'),"
            "json_extract(payload_json,'$.stage'),"
            "json_extract(payload_json,'$.target_id'),"
            "json_extract(payload_json,'$.purpose') "
            "FROM decision WHERE cycle_id=? AND actor='agent' "
            "AND type='runtime_stage_submission' AND json_valid(payload_json) "
            "AND json_extract(payload_json,'$.protocol')="
            "'runtime-stage-submission-index-v1' "
            "AND json_extract(payload_json,'$.submission_ref')=? "
            "AND json_extract(payload_json,'$.artifact_hash')=?",
            (cycle_no, str(receipt_path), artifact_hash))
        if len(rows) != 1:
            raise CycleReplayError(
                "runtime 托管阶段产物未由当前 quest MCP 唯一登记")
        (receipt_hash, indexed_artifact_hash, indexed_stage,
         indexed_target, indexed_purpose) = rows[0]
        if (indexed_artifact_hash != artifact_hash or indexed_stage != stage
                or (indexed_target if indexed_target is not None else None) != target_id
                or not isinstance(indexed_purpose, str)):
            raise CycleReplayError("MCP stage submission SQL 索引身份漂移")
        try:
            receipt_hash = normalize_sha256(
                receipt_hash, field="stage_submission_receipt_hash")
        except ArtifactCapabilityError as error:
            raise CycleReplayError("MCP stage submission SQL 回执 hash 非法") from error

        try:
            raw = read_artifact_bytes(
                receipt_path, expected_hash=receipt_hash,
                max_bytes=4 * 1024 * 1024,
                label="cycle replay runtime stage submission receipt")
            receipt = json.loads(raw.decode("utf-8"))
        except (ArtifactCapabilityError, UnicodeDecodeError,
                json.JSONDecodeError) as error:
            raise CycleReplayError(
                "MCP stage submission 回执 path+hash 身份非法") from error
        if (not isinstance(receipt, dict)
                or receipt.get("protocol") != "runtime-stage-submission-v1"
                or receipt.get("cycle_id") != cycle_id
                or receipt.get("stage") != stage
                or receipt.get("target_id") != target_id
                or receipt.get("purpose") != indexed_purpose
                or receipt.get("pack_hash") != pack_hash
                or receipt.get("artifact_hash") != artifact_hash):
            raise CycleReplayError("MCP stage submission 回执阶段/上下文身份漂移")
        if not (indexed_purpose == purpose
                or indexed_purpose.startswith(purpose + "-n")):
            raise CycleReplayError("MCP stage submission purpose 与回放事件不绑定")
        if runner_call_id is not None:
            call = self._query_one(
                self.submission_registry,
                "SELECT phase,purpose,status FROM runner_call "
                "WHERE id=? AND cycle_id=?",
                (runner_call_id, cycle_no))
            if (call is None or call[0] != stage or call[1] != indexed_purpose
                    or call[2] not in ("running", "success")):
                raise CycleReplayError(
                    "MCP stage submission 未绑定当前 runner_call")

        entries = receipt.get("files")
        md_entry = receipt.get("md")
        if not isinstance(entries, list) or len(entries) > 1024:
            raise CycleReplayError("MCP stage submission files 清单非法")
        descriptor_entries: List[Dict[str, Any]] = []
        matches: List[Mapping[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                raise CycleReplayError("MCP stage submission file entry 非 object")
            try:
                descriptor_item = {key: item[key] for key in (
                    "name", "kind", "size_bytes", "sha256")}
            except KeyError as error:
                raise CycleReplayError(
                    "MCP stage submission file entry 缺身份字段") from error
            descriptor_entries.append(descriptor_item)
            if item.get("name") == logical.as_posix():
                matches.append(item)
        descriptor_md = None
        if md_entry is not None:
            if not isinstance(md_entry, dict):
                raise CycleReplayError("MCP stage submission md entry 非 object")
            try:
                descriptor_md = {key: md_entry[key] for key in (
                    "size_bytes", "sha256")}
            except KeyError as error:
                raise CycleReplayError(
                    "MCP stage submission md entry 缺身份字段") from error
        computed_artifact_hash = "sha256:" + _sha256(_json_bytes({
            "files": descriptor_entries, "md": descriptor_md,
        }, pretty=False))
        if computed_artifact_hash != artifact_hash:
            raise CycleReplayError("MCP stage submission artifact manifest hash 漂移")
        if len(matches) != 1:
            raise CycleReplayError(
                f"MCP stage submission 未唯一登记托管文件: {logical}")
        entry = matches[0]
        try:
            entry_hash = normalize_sha256(
                entry.get("sha256"), field=f"stage submission {logical}.sha256")
            ref_hash = normalize_sha256(
                ref.sha256, field=f"managed ref {logical}.sha256")
        except ArtifactCapabilityError as error:
            raise CycleReplayError(
                f"MCP stage submission 托管文件 hash 非法: {logical}") from error
        path = Path(ref.path)
        if (entry.get("kind") != "managed" or entry.get("path") != str(path)
                or entry.get("size_bytes") != ref.size_bytes
                or entry_hash != ref_hash):
            raise CycleReplayError(
                f"MCP stage submission 托管文件身份与回执不符: {logical}")
        try:
            path.relative_to(receipt_path.parent)
        except ValueError as error:
            raise CycleReplayError(
                f"MCP stage submission 托管文件逃逸其提交目录: {logical}") from error
        self._assert_no_symlink_descendant(
            self.work_root, path,
            label=f"MCP stage submission managed {logical}")
        return path.relative_to(self.work_root).as_posix()

    def _managed_artifact_entry(
            self, cycle_id: str, logical: PurePosixPath,
            ref: ManagedArtifactRef, *, stage: str,
            target_id: Optional[str], purpose: str,
            pack_hash: Optional[str], runner_call_id: Optional[int],
            stage_submission_ref: Optional[str],
            stage_submission_artifact_hash: Optional[str]) -> Dict[str, Any]:
        path = Path(ref.path)
        if not path.is_absolute() or path != Path(os.path.normpath(ref.path)):
            raise CycleReplayError("托管阶段产物 path 须为规范绝对路径")
        cycle = self.cycle_dir(cycle_id)
        expected_root = cycle / "artifacts" / "managed-files"
        try:
            path.relative_to(expected_root)
            managed_ref = path.relative_to(self.work_root).as_posix()
        except ValueError as error:
            runtime_root = self.work_root / "runtime" / "stage-submissions"
            try:
                path.relative_to(runtime_root)
            except ValueError:
                raise CycleReplayError(
                    f"托管阶段产物不在受信 cycle/MCP 文件管理区: {path}") from error
            managed_ref = self._registered_runtime_submission(
                cycle_id=cycle_id, stage=stage, target_id=target_id,
                purpose=purpose, pack_hash=pack_hash,
                runner_call_id=runner_call_id, logical=logical, ref=ref,
                submission_ref=stage_submission_ref,
                submission_artifact_hash=stage_submission_artifact_hash)
        try:
            with open_artifact(
                    path, expected_hash=ref.sha256, expected_size=ref.size_bytes,
                    label=f"cycle replay managed artifact {logical}") as capability:
                capability.verify_unchanged()
                capability.verify_path_binding()
                digest = capability.identity.content_hash.removeprefix("sha256:")
                size = capability.identity.size_bytes
        except ArtifactCapabilityError as error:
            raise CycleReplayError(
                f"托管阶段产物 path+hash 身份非法: {logical}: {error}") from error
        return {
            "path": logical.as_posix(), "sha256": digest, "bytes": size,
            "storage": "managed_ref", "managed_ref": managed_ref,
        }

    def _persist_handoff(self, cycle: Path, *, event_id: str, stage: str,
                         target: Optional[str], purpose: str, pack_hash: Optional[str],
                         file_names: Iterable[str], md: str) -> int:
        marker = f"<!-- replay-event:{event_id} -->"
        existing_max = 0
        for candidate in sorted(cycle.glob("handoff-*.md")):
            match = _HANDOFF_RE.fullmatch(candidate.name)
            if match is None:
                continue
            no = int(match.group(1))
            existing_max = max(existing_max, no)
            raw = _regular_bytes(candidate).decode("utf-8")
            if raw.startswith(marker + "\n"):
                self._repair_handoff_manifest(cycle, no, event_id, candidate)
                return no
        no = existing_max + 1
        file_names = list(file_names)
        body = [
            marker,
            "# 阶段交接",
            "",
            f"- 轮次：{cycle.name}",
            f"- 阶段：{stage}",
            f"- 调用：{purpose}",
            f"- 目标：{target or '（无）'}",
            f"- 上下文包：{pack_hash or '（未绑定）'}",
            f"- 已归档产物：{', '.join(file_names)}",
            "",
            "## 工人摘要",
            "",
            md if md.strip() else "（本 turn 未提供独立 md；结构化产物已完整归档。）",
            "",
        ]
        path = cycle / f"handoff-{no}.md"
        _atomic_write(path, "\n".join(body).encode("utf-8"), immutable=True)
        self._repair_handoff_manifest(cycle, no, event_id, path)
        return no

    @staticmethod
    def _handoff_no_for_event(cycle: Path, event_id: str) -> Optional[int]:
        marker = f"<!-- replay-event:{event_id} -->\n".encode("utf-8")
        for candidate in sorted(cycle.glob("handoff-*.md")):
            match = _HANDOFF_RE.fullmatch(candidate.name)
            if match is not None and _regular_bytes(candidate).startswith(marker):
                return int(match.group(1))
        return None

    @staticmethod
    def _repair_handoff_manifest(cycle: Path, no: int, event_id: str, path: Path) -> None:
        raw = _regular_bytes(path)
        payload = {
            "schema": ARTIFACT_SCHEMA,
            "handoff_no": no,
            "event_id": event_id,
            "path": path.name,
            "sha256": _sha256(raw),
            "bytes": len(raw),
        }
        _atomic_write(cycle / f"handoff-{no}.manifest.json", _json_bytes(payload), immutable=True)

    @staticmethod
    def _latest_stage_event(cycle: Path, stage: str):
        pointer = cycle / "artifacts" / f"{stage}.latest.json"
        if not pointer.exists():
            candidates = sorted(
                (cycle / "artifacts").glob(f"{stage}.*.latest.json"))
            targetless = []
            for candidate in candidates:
                value = json.loads(
                    _regular_bytes(candidate).decode("utf-8"))
                if (value.get("stage") == stage
                        and value.get("target_id") is None):
                    targetless.append((candidate, value))
            if not targetless:
                return None
            if len(targetless) != 1:
                raise CycleReplayError(
                    f"{cycle.name} {stage} latest 指针不唯一: "
                    f"{[item[0].name for item in targetless]}")
            pointer, value = targetless[0]
        else:
            value = json.loads(_regular_bytes(pointer).decode("utf-8"))
        event_id = value.get("event_id")
        if not isinstance(event_id, str) or re.fullmatch(r"[0-9a-f]{64}", event_id) is None:
            raise CycleReplayError(f"{cycle.name} {stage}.latest event_id 非法")
        event = cycle / "artifacts" / "history" / event_id
        manifest = json.loads(_regular_bytes(event / "manifest.json").decode("utf-8"))
        if manifest.get("event_id") != event_id or manifest.get("stage") != stage:
            raise CycleReplayError(f"{cycle.name} {stage} latest/history 身份漂移")
        return event_id, manifest, _regular_bytes(event / "stage.md")

    @staticmethod
    def _mechanical_report(*, cycle_id: str, status: str, route: Optional[str],
                           question_id: Optional[str], next_question_id: Optional[str],
                           next_intent: Optional[str], reasoning_present: bool) -> str:
        return (
            f"# 轮次 {cycle_id} 报告\n\n"
            "本报告由编排器根据终态元数据机械生成；它不是 reasoning 模型正文。\n\n"
            f"- 终态：{status}\n"
            f"- 路由：{route or '（无研究路由）'}\n"
            f"- 本轮问题：{question_id or '（无）'}\n"
            f"- 下一问题：{next_question_id or '（无）'}\n"
            f"- 下一意图：{next_intent or '（无）'}\n"
            f"- reasoning 结构化 turn：{'已归档但 md 为空' if reasoning_present else '未发生'}\n"
        )

    @staticmethod
    def _restored_sqlite_report(
            *, cycle_id: str, status: str, route: Optional[str],
            question_id: Optional[str],
            restore_provenance: Mapping[str, Any]) -> str:
        return (
            f"# 轮次 {cycle_id} SQLite-only 恢复说明\n\n"
            "该工作根来自已验证的 SQLite-only 快照；原始 Reasoning stage "
            "文件不在快照范围内，未随快照恢复。\n\n"
            f"- 数据库终态：{status}\n"
            f"- 路由：{route or '（无研究路由）'}\n"
            f"- 本轮问题：{question_id or '（无）'}\n"
            f"- 快照源终点：{restore_provenance['source_cycle']}\n"
            f"- SQLite 备份 SHA-256：{restore_provenance['backup_sha256']}\n"
            "- 数据库中现存的 Reasoning phase_commit 与结构化 summary "
            "已原样投影到 cycle_state.json；缺失项不会补写。\n"
            "- 本说明不重建、改写或补写原始科研正文。\n"
        )

    @staticmethod
    def _closure_manifest(cycle: Path, *, cycle_id: str, status: str,
                          route: Optional[str], report_kind: str,
                          source_event: Optional[str]) -> Dict[str, Any]:
        rows = []
        for path in sorted(cycle.rglob("*"), key=lambda item: item.relative_to(cycle).as_posix()):
            rel = path.relative_to(cycle).as_posix()
            if rel == "replay_manifest.json" or ".staging-" in path.name:
                continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                continue
            if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise CycleReplayError(f"回放闭包含非普通文件: {path}")
            raw = _regular_bytes(path)
            rows.append({"path": rel, "sha256": _sha256(raw), "bytes": len(raw)})
        has_context = any(row["path"].startswith("context_pack/") for row in rows)
        has_artifacts = any(row["path"].startswith("artifacts/") for row in rows)
        has_handoff = any(_HANDOFF_RE.fullmatch(row["path"]) for row in rows)
        has_cycle_state = any(row["path"] == "cycle_state.json" for row in rows)
        return {
            "schema": ARCHIVE_SCHEMA,
            "cycle_id": cycle_id,
            "status": status,
            "route": route,
            "report_kind": report_kind,
            "source_event_id": source_event,
            "coverage": {
                "context_pack": has_context,
                "stage_artifacts": has_artifacts,
                "handoff": has_handoff,
                "cycle_report": True,
                "cycle_state": has_cycle_state,
                "legacy_incomplete": not (
                    has_context and has_artifacts and has_handoff
                    and has_cycle_state),
            },
            "files": rows,
        }
