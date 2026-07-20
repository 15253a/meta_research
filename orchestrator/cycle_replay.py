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


ARCHIVE_SCHEMA = "meta-research-cycle-replay/v1"
CONTEXT_SCHEMA = "meta-research-context-pack-archive/v1"
ARTIFACT_SCHEMA = "meta-research-stage-artifact-archive/v1"
REPORT_SCHEMA = "meta-research-cycle-report/v1"
TERMINAL_STATES = ("done", "failed", "aborted")

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
                       next_intent: Optional[str] = None) -> Dict[str, Any]:
        """Seal one terminal research cycle and promote the reasoning report.

        A normal reasoning output with non-empty ``md`` is copied without even
        newline normalization.  Mechanical/failed/legacy cycles receive an
        explicitly labelled orchestrator report instead of fabricated model
        prose.
        """
        self.owner_guard()
        with self._lock:
            if status not in TERMINAL_STATES:
                raise CycleReplayError(f"cycle 尚非终态，不能封口: {cycle_id}/{status}")
            cycle = self.cycle_dir(cycle_id)
            _ensure_dir(cycle)
            reasoning = self._latest_stage_event(cycle, "reasoning")
            source_event = None
            if reasoning is not None:
                source_event, _event_manifest, md_raw = reasoning
            else:
                md_raw = b""
            if md_raw.strip():
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
            if report.exists():
                # Full verification is cheap for stage JSON/md and catches a
                # post-publication edit before the DB backup is advanced.
                self.verify_cycle(cycle_id)
                continue
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
                next_intent=next_intent)
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
            return None
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
                "legacy_incomplete": not (has_context and has_artifacts and has_handoff),
            },
            "files": rows,
        }
