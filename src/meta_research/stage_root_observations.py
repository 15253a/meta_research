"""Read-only, redacted observations for one formal Stage root provider."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from meta_research.harness_adapters import (
    CodexRootCommandOutputProjector,
    HarnessAdapterUnavailable,
    RootCommandOutputProjection,
)
from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    read_transport_envelope,
    read_transport_key_for_operation,
)


_STAGE_WORKSPACES = {
    "idea_stage": "idea-skill-provider",
    "plan_stage": "plan-skill-provider",
    "bundle_stage": "bundle-skill-provider",
    "reasoning_stage": "reasoning-skill-provider",
}
_STAGE_UNIT_KINDS = {
    "idea_stage": {"idea_primary", "idea_review"},
    "plan_stage": {"plan_primary", "plan_review"},
    "bundle_stage": {"bundle_primary", "bundle_review"},
    "reasoning_stage": {"reasoning_primary", "reasoning_review"},
}
_STAGE_ROOT_OBSERVATION_SCHEMA = "meta-research/stage-root-observation/v1"
_STAGE_ROOT_STREAM_SCHEMA = "meta-research/stage-root-observation-stream/v1"
_CODEX_PROVIDER_OPERATION_SCHEMA = "meta-research/codex-provider-operation/v3"
_MAX_PAGE_BYTES = 256 * 1024
_MAX_RAW_READ_BYTES = 8 * 1024 * 1024
_MAX_JSONL_LINE_BYTES = 1024 * 1024
_MAX_INVOCATION_BYTES = 128 * 1024
_RAW_PAGE_MIN_BYTES = 4
_RAW_PAGE_MAX_BYTES = 256 * 1024


class StageRootObservationError(Exception):
    """A read-only Stage observation cannot be resolved safely."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class StageRootObservation:
    event_ref: str
    cursor: str
    sequence: int
    kind: Literal["command_output", "output_gap"]
    stream: Literal["stdout"]
    text: str
    recorded_at: float
    redacted: bool
    truncated: bool
    dropped_bytes: int = 0
    dropped_events: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "event_ref": self.event_ref,
            "cursor": self.cursor,
            "sequence": self.sequence,
            "kind": self.kind,
            "stream": self.stream,
            "text": self.text,
            "recorded_at": self.recorded_at,
            "redacted": self.redacted,
            "truncated": self.truncated,
            "dropped_bytes": self.dropped_bytes,
            "dropped_events": self.dropped_events,
        }


@dataclass(frozen=True)
class StageRootObservationPage:
    run_ref: str
    run_kind: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    fence_ref: str
    phase: str | None
    native_session_ref: str | None
    stream_ref: str
    status: str
    availability: Literal["ready", "waiting", "limited"]
    items: tuple[StageRootObservation, ...]
    next_cursor: str | None
    head_cursor: str | None
    has_more: bool
    source_limited: bool
    observation_only: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "run_ref": self.run_ref,
            "run_kind": self.run_kind,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "fence_ref": self.fence_ref,
            "phase": self.phase,
            "native_session_ref": self.native_session_ref,
            "stream_ref": self.stream_ref,
            "status": self.status,
            "availability": self.availability,
            "items": [item.as_dict() for item in self.items],
            "next_cursor": self.next_cursor,
            "head_cursor": self.head_cursor,
            "has_more": self.has_more,
            "source_limited": self.source_limited,
            "observation_only": self.observation_only,
        }


@dataclass(frozen=True)
class StageRawOutputPage:
    run_ref: str
    run_kind: str
    attempt_ref: str
    attempt_generation: int
    root_session_ref: str
    fence_ref: str
    operation_ref: str | None
    phase: str | None
    native_session_ref: str | None
    transport_invocation_hash: str | None
    stream_ref: str
    status: str
    text: str
    offset: int
    next_offset: int
    source_bytes: int
    has_more: bool
    source_caught_up: bool
    exact: bool = True
    unredacted: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": "meta-research/stage-raw-output-page/v1",
            "run_ref": self.run_ref,
            "run_kind": self.run_kind,
            "attempt_ref": self.attempt_ref,
            "attempt_generation": self.attempt_generation,
            "root_session_ref": self.root_session_ref,
            "fence_ref": self.fence_ref,
            "operation_ref": self.operation_ref,
            "phase": self.phase,
            "native_session_ref": self.native_session_ref,
            "transport_invocation_hash": self.transport_invocation_hash,
            "stream_ref": self.stream_ref,
            "status": self.status,
            "text": self.text,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "source_bytes": self.source_bytes,
            "has_more": self.has_more,
            "source_caught_up": self.source_caught_up,
            "exact": self.exact,
            "unredacted": self.unredacted,
        }


StageRootObservationScopeLookup = Callable[[str], dict[str, object] | None]


class StageRootObservationReader:
    """Read a signed Stage spool through redacted and explicit private views."""

    def __init__(
        self,
        data_root: Path,
        *,
        scope_lookup: StageRootObservationScopeLookup,
    ) -> None:
        self._data_root = data_root
        self._scope_lookup = scope_lookup

    def query(
        self,
        run_ref: str,
        *,
        after_cursor: str | None = None,
        limit: int = 128,
    ) -> StageRootObservationPage:
        if (
            not isinstance(run_ref, str)
            or not run_ref
            or len(run_ref) > 256
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 256
        ):
            raise StageRootObservationError("stage_root_observation_query_invalid")
        scope = self._validated_scope(self._scope_lookup(run_ref), run_ref=run_ref)
        unit_ref = scope["unit_ref"]
        operation_ref = scope["operation_ref"]
        if unit_ref is None or operation_ref is None:
            if after_cursor is not None:
                raise StageRootObservationError(
                    "stage_root_observation_cursor_stale"
                )
            return self._empty_page(scope)

        operation = self._resolve_operation(scope)
        if operation is None:
            if after_cursor is not None:
                raise StageRootObservationError(
                    "stage_root_observation_cursor_stale"
                )
            return self._empty_page(scope)
        directory, invocation_hash, phase, sealed_native_session_ref = operation
        stdout_path = directory / "stdout.jsonl"
        if not stdout_path.exists():
            if after_cursor is not None:
                raise StageRootObservationError(
                    "stage_root_observation_cursor_stale"
                )
            return self._empty_page(scope, phase=phase)
        if stdout_path.is_symlink() or not stdout_path.is_file():
            raise StageRootObservationError("stage_root_observation_spool_unavailable")
        try:
            stat = stdout_path.stat()
        except OSError as error:
            raise StageRootObservationError(
                "stage_root_observation_spool_unavailable"
            ) from error
        file_identity = f"{stat.st_dev}:{stat.st_ino}"
        stream_ref = _stream_ref(scope, phase=phase)
        after = _decode_cursor(
            after_cursor,
            expected_stream_ref=stream_ref,
            expected_invocation_hash=invocation_hash,
            expected_file_identity=file_identity,
        )
        observations, native_session_ref, source_limited = self._project_stdout(
            stdout_path,
            stream_ref=stream_ref,
            invocation_hash=invocation_hash,
            file_identity=file_identity,
            recorded_at=float(stat.st_mtime),
            sealed_native_session_ref=sealed_native_session_ref,
        )
        if after is not None and observations and after > observations[-1].sequence:
            raise StageRootObservationError("stage_root_observation_cursor_stale")
        available = [
            observation
            for observation in observations
            if after is None or observation.sequence > after
        ]
        items: list[StageRootObservation] = []
        page_bytes = 0
        for observation in available:
            encoded_bytes = len(observation.text.encode("utf-8"))
            if len(items) >= limit or page_bytes + encoded_bytes > _MAX_PAGE_BYTES:
                break
            items.append(observation)
            page_bytes += encoded_bytes
        has_more = len(items) < len(available)
        return StageRootObservationPage(
            run_ref=scope["run_ref"],
            run_kind=scope["run_kind"],
            attempt_ref=scope["attempt_ref"],
            attempt_generation=scope["attempt_generation"],
            root_session_ref=scope["root_session_ref"],
            fence_ref=scope["fence_ref"],
            phase=phase,
            native_session_ref=native_session_ref,
            stream_ref=stream_ref,
            status=_observation_status(scope["status"]),
            availability="limited" if source_limited else "ready",
            items=tuple(items),
            next_cursor=items[-1].cursor if items else after_cursor,
            head_cursor=(
                None if not observations else observations[-1].cursor
            ),
            has_more=has_more,
            source_limited=source_limited,
        )

    def query_raw(
        self,
        run_ref: str,
        *,
        after: int = 0,
        limit: int = 64 * 1024,
    ) -> StageRawOutputPage:
        """Return exact private Provider stdout for the current Stage unit."""

        if (
            not isinstance(run_ref, str)
            or not run_ref
            or len(run_ref) > 256
            or not isinstance(after, int)
            or isinstance(after, bool)
            or after < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not _RAW_PAGE_MIN_BYTES <= limit <= _RAW_PAGE_MAX_BYTES
        ):
            raise StageRootObservationError("stage_raw_output_query_invalid")
        scope = self._validated_scope(self._scope_lookup(run_ref), run_ref=run_ref)
        if scope["unit_ref"] is None or scope["operation_ref"] is None:
            if after:
                raise StageRootObservationError("stage_raw_output_cursor_stale")
            return self._empty_raw_page(scope)
        operation = self._resolve_operation(scope)
        if operation is None:
            if after:
                raise StageRootObservationError("stage_raw_output_cursor_stale")
            return self._empty_raw_page(scope)
        directory, invocation_hash, phase, sealed_native_session_ref = operation
        stdout_path = directory / "stdout.jsonl"
        if not stdout_path.exists():
            if after:
                raise StageRootObservationError("stage_raw_output_cursor_stale")
            return self._empty_raw_page(
                scope,
                operation_ref=_scope_text(scope, "operation_ref"),
                phase=phase,
                invocation_hash=invocation_hash,
                native_session_ref=sealed_native_session_ref,
            )
        if stdout_path.is_symlink() or not stdout_path.is_file():
            raise StageRootObservationError("stage_root_observation_spool_unavailable")
        try:
            stat = stdout_path.stat()
            source_bytes = int(stat.st_size)
            file_identity = f"{stat.st_dev}:{stat.st_ino}"
            native_session_ref = _raw_root_session_ref(
                stdout_path,
                sealed_native_session_ref=sealed_native_session_ref,
            )
            if after > source_bytes:
                raise StageRootObservationError("stage_raw_output_cursor_stale")
            with stdout_path.open("rb") as stream:
                stream.seek(after)
                raw = stream.read(min(limit, source_bytes - after))
        except StageRootObservationError:
            raise
        except OSError as error:
            raise StageRootObservationError(
                "stage_root_observation_spool_unavailable"
            ) from error
        text, consumed = _decode_raw_page(raw)
        if raw and consumed == 0:
            raise StageRootObservationError("stage_raw_output_cursor_invalid")
        next_offset = after + consumed
        return StageRawOutputPage(
            run_ref=_scope_text(scope, "run_ref"),
            run_kind=_scope_text(scope, "run_kind"),
            attempt_ref=_scope_text(scope, "attempt_ref"),
            attempt_generation=_scope_generation(scope),
            root_session_ref=_scope_text(scope, "root_session_ref"),
            fence_ref=_scope_text(scope, "fence_ref"),
            operation_ref=_scope_text(scope, "operation_ref"),
            phase=phase,
            native_session_ref=native_session_ref,
            transport_invocation_hash=invocation_hash,
            stream_ref=_raw_stream_ref(
                scope,
                phase=phase,
                invocation_hash=invocation_hash,
                file_identity=file_identity,
            ),
            status=_observation_status(_scope_text(scope, "status")),
            text=text,
            offset=after,
            next_offset=next_offset,
            source_bytes=source_bytes,
            has_more=next_offset < source_bytes,
            source_caught_up=next_offset >= source_bytes,
        )

    def _empty_raw_page(
        self,
        scope: dict[str, object],
        *,
        operation_ref: str | None = None,
        phase: str | None = None,
        invocation_hash: str | None = None,
        native_session_ref: str | None = None,
    ) -> StageRawOutputPage:
        return StageRawOutputPage(
            run_ref=_scope_text(scope, "run_ref"),
            run_kind=_scope_text(scope, "run_kind"),
            attempt_ref=_scope_text(scope, "attempt_ref"),
            attempt_generation=_scope_generation(scope),
            root_session_ref=_scope_text(scope, "root_session_ref"),
            fence_ref=_scope_text(scope, "fence_ref"),
            operation_ref=operation_ref,
            phase=phase,
            native_session_ref=native_session_ref,
            transport_invocation_hash=invocation_hash,
            stream_ref=_raw_stream_ref(
                scope,
                phase=phase,
                invocation_hash=invocation_hash,
                file_identity=None,
            ),
            status=_observation_status(_scope_text(scope, "status")),
            text="",
            offset=0,
            next_offset=0,
            source_bytes=0,
            has_more=False,
            source_caught_up=True,
        )

    def _empty_page(
        self, scope: dict[str, object], *, phase: str | None = None
    ) -> StageRootObservationPage:
        return StageRootObservationPage(
            run_ref=_scope_text(scope, "run_ref"),
            run_kind=_scope_text(scope, "run_kind"),
            attempt_ref=_scope_text(scope, "attempt_ref"),
            attempt_generation=_scope_generation(scope),
            root_session_ref=_scope_text(scope, "root_session_ref"),
            fence_ref=_scope_text(scope, "fence_ref"),
            phase=phase,
            native_session_ref=None,
            stream_ref=_stream_ref(scope, phase=phase),
            status=_observation_status(_scope_text(scope, "status")),
            availability="waiting",
            items=(),
            next_cursor=None,
            head_cursor=None,
            has_more=False,
            source_limited=False,
        )

    def _resolve_operation(
        self, scope: dict[str, object]
    ) -> tuple[Path, str, str, str | None] | None:
        run_kind = _scope_text(scope, "run_kind")
        workspace_name = _STAGE_WORKSPACES.get(run_kind)
        if workspace_name is None:
            raise StageRootObservationError("stage_root_observation_run_unsupported")
        operation_ref = _scope_text(scope, "operation_ref")
        workspace = self._data_root / workspace_name
        provider_operations = workspace / "provider-operations"
        operation_root = provider_operations / canonical_hash({"job_ref": operation_ref})
        if not workspace.exists() or not provider_operations.exists():
            return None
        if not _safe_directory(workspace) or not _safe_directory(provider_operations):
            raise StageRootObservationError("stage_root_observation_spool_unavailable")
        if not operation_root.exists():
            return None
        if not _safe_directory(operation_root):
            raise StageRootObservationError("stage_root_observation_spool_unavailable")
        operation_name = scope.get("operation_name")
        if isinstance(operation_name, str):
            directory = operation_root / operation_name
            if not directory.exists():
                return None
            if directory.is_symlink() or not directory.is_dir():
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                )
            candidates = (directory,)
        else:
            try:
                # Rolling Bundle operations intentionally retain every prior
                # phase under one logical job. unit_ref is a deterministic,
                # persisted physical-turn identity, so use it to select a
                # directory before opening any private invocation or stdout.
                candidates = tuple(
                    path
                    for path in operation_root.iterdir()
                    if not path.is_symlink()
                    and path.is_dir()
                    and _operation_phase_matches_scope(scope, path.name)
                )
            except OSError as error:
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                ) from error
            if not candidates:
                return None
            if len(candidates) != 1:
                raise StageRootObservationError(
                    "stage_root_observation_spool_ambiguous"
                )
        candidates_with_invocations: list[tuple[Path, str, str, str | None]] = []
        for directory in candidates:
            invocation_path = directory / "invocation.json"
            key_path = provider_operations / ".transport-seal.key"
            try:
                invocation_too_large = (
                    invocation_path.stat().st_size > _MAX_INVOCATION_BYTES
                )
            except OSError as error:
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                ) from error
            if (
                invocation_path.is_symlink()
                or not invocation_path.is_file()
                or invocation_too_large
            ):
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                )
            if key_path.is_symlink() or not key_path.is_file():
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                )
            try:
                _key_path, key = read_transport_key_for_operation(directory)
                invocation = read_transport_envelope(invocation_path, key)
            except (OSError, ProviderSupervisorError) as error:
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                ) from error
            phase = invocation.get("operation_name")
            native_session_ref = invocation.get("native_session_ref")
            if (
                invocation.get("schema_ref") != _CODEX_PROVIDER_OPERATION_SCHEMA
                or invocation.get("job_ref") != operation_ref
                or not isinstance(phase, str)
                or not phase
                or phase != directory.name
                or (
                    native_session_ref is not None
                    and (
                        not isinstance(native_session_ref, str)
                        or not native_session_ref
                    )
                )
            ):
                raise StageRootObservationError(
                    "stage_root_observation_spool_unavailable"
                )
            candidates_with_invocations.append(
                (
                    directory,
                    canonical_hash(invocation),
                    phase,
                    native_session_ref,
                )
            )
        matching = [
            candidate
            for candidate in candidates_with_invocations
            if _operation_phase_matches_scope(scope, candidate[2])
        ]
        if not matching:
            return None
        if len(matching) != 1:
            raise StageRootObservationError("stage_root_observation_spool_ambiguous")
        return matching[0]

    def _project_stdout(
        self,
        stdout_path: Path,
        *,
        stream_ref: str,
        invocation_hash: str,
        file_identity: str,
        recorded_at: float,
        sealed_native_session_ref: str | None,
    ) -> tuple[tuple[StageRootObservation, ...], str | None, bool]:
        projector = CodexRootCommandOutputProjector(
            known_root_native_session_ref=sealed_native_session_ref,
            require_explicit_root_actor=True,
        )
        observations: list[StageRootObservation] = []
        observation_sequence = 0
        raw_bytes = 0
        source_limited = False
        first_record = True
        try:
            with stdout_path.open("rb") as stream:
                while raw_bytes < _MAX_RAW_READ_BYTES:
                    remaining = _MAX_RAW_READ_BYTES - raw_bytes
                    raw_line = stream.readline(
                        min(_MAX_JSONL_LINE_BYTES + 1, remaining + 1)
                    )
                    if not raw_line:
                        break
                    raw_bytes += len(raw_line)
                    if (
                        len(raw_line) > _MAX_JSONL_LINE_BYTES
                        or not raw_line.endswith(b"\n")
                    ):
                        source_limited = True
                        break
                    try:
                        event = json.loads(raw_line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        source_limited = True
                        break
                    if not isinstance(event, dict):
                        source_limited = True
                        break
                    if first_record:
                        first_record = False
                        if (
                            event.get("type") != "thread.started"
                            or event.get("parent_thread_id") not in (None, "")
                            or not isinstance(event.get("thread_id"), str)
                            or not event["thread_id"]
                        ):
                            # The Codex JSON protocol begins a durable root
                            # invocation with its root thread. A source that
                            # starts elsewhere cannot establish a safe root
                            # identity, so never project later unscoped output.
                            source_limited = True
                            break
                    projection = projector.project(event)
                    if projection is not None:
                        observation_sequence += 1
                        observations.append(
                            _observation_from_projection(
                                projection,
                                sequence=observation_sequence,
                                stream_ref=stream_ref,
                                invocation_hash=invocation_hash,
                                file_identity=file_identity,
                                recorded_at=recorded_at,
                            )
                        )
                    if event.get("type") == "turn.completed":
                        projection = projector.finish()
                        if projection is not None:
                            observation_sequence += 1
                            observations.append(
                                _observation_from_projection(
                                    projection,
                                    sequence=observation_sequence,
                                    stream_ref=stream_ref,
                                    invocation_hash=invocation_hash,
                                    file_identity=file_identity,
                                    recorded_at=recorded_at,
                                )
                            )
                if raw_bytes >= _MAX_RAW_READ_BYTES:
                    source_limited = True
        except (OSError, HarnessAdapterUnavailable) as error:
            raise StageRootObservationError(
                "stage_root_observation_spool_unavailable"
            ) from error
        return (
            tuple(observations),
            projector.root_native_session_ref,
            source_limited or projector.unscoped_command_events_omitted,
        )

    def _validated_scope(
        self, value: dict[str, object] | None, *, run_ref: str
    ) -> dict[str, object]:
        if value is None:
            raise StageRootObservationError("stage_root_observation_run_not_found")
        required = {
            "run_ref",
            "run_kind",
            "attempt_ref",
            "attempt_generation",
            "root_session_ref",
            "fence_ref",
            "status",
            "unit_ref",
            "operation_ref",
            "unit_kind",
            "operation_name",
        }
        if not isinstance(value, dict) or set(value) != required:
            raise StageRootObservationError("stage_root_observation_scope_invalid")
        if (
            value.get("run_ref") != run_ref
            or value.get("run_kind") not in _STAGE_WORKSPACES
            or any(
                not isinstance(value.get(field), str) or not value[field]
                for field in (
                    "run_ref",
                    "attempt_ref",
                    "root_session_ref",
                    "fence_ref",
                    "status",
                )
            )
            or not isinstance(value.get("attempt_generation"), int)
            or isinstance(value.get("attempt_generation"), bool)
            or value["attempt_generation"] < 1
        ):
            raise StageRootObservationError("stage_root_observation_scope_invalid")
        unit_values = (
            value.get("unit_ref"),
            value.get("operation_ref"),
            value.get("unit_kind"),
        )
        operation_name = value.get("operation_name")
        if any(item is None for item in unit_values):
            if any(item is not None for item in unit_values) or operation_name is not None:
                raise StageRootObservationError("stage_root_observation_scope_invalid")
        elif (
            any(not isinstance(item, str) or not item for item in unit_values)
            or value["unit_kind"] not in _STAGE_UNIT_KINDS[value["run_kind"]]
            or (
                operation_name is not None
                and (
                    not isinstance(operation_name, str)
                    or not operation_name
                )
            )
        ):
            raise StageRootObservationError("stage_root_observation_scope_invalid")
        return dict(value)


def _safe_directory(path: Path) -> bool:
    try:
        return not path.is_symlink() and path.is_dir()
    except OSError:
        return False


def _scope_text(scope: dict[str, object], field: str) -> str:
    value = scope[field]
    assert isinstance(value, str) and value
    return value


def _scope_generation(scope: dict[str, object]) -> int:
    value = scope["attempt_generation"]
    assert isinstance(value, int) and not isinstance(value, bool) and value >= 1
    return value


def _stream_ref(scope: dict[str, object], *, phase: str | None) -> str:
    return "stage-root-stream:" + canonical_hash(
        {
            "schema_ref": _STAGE_ROOT_STREAM_SCHEMA,
            "run_ref": _scope_text(scope, "run_ref"),
            "attempt_ref": _scope_text(scope, "attempt_ref"),
            "attempt_generation": _scope_generation(scope),
            "root_session_ref": _scope_text(scope, "root_session_ref"),
            "fence_ref": _scope_text(scope, "fence_ref"),
            "unit_ref": scope.get("unit_ref"),
            "operation_ref": scope.get("operation_ref"),
            "phase": phase,
        }
    )


def _operation_phase_matches_scope(scope: dict[str, object], phase: str) -> bool:
    """Bind a signed provider phase to the exact current Owner unit.

    Stable Stage invocations expose their phase directly through the Owner
    scope. Bundle's rolling work intentionally shares one logical operation
    across phase directories, so its persisted physical unit_ref is the
    authority instead of directory order or mtime.
    """

    operation_name = scope.get("operation_name")
    if isinstance(operation_name, str):
        return phase == operation_name
    if (
        scope.get("run_kind") != "bundle_stage"
        or scope.get("unit_kind") != "bundle_review"
    ):
        return False
    return _scope_text(scope, "unit_ref") == _bundle_rolling_unit_ref(
        operation_ref=_scope_text(scope, "operation_ref"),
        operation_name=phase,
        attempt_ref=_scope_text(scope, "attempt_ref"),
    )


def _bundle_rolling_unit_ref(
    *, operation_ref: str, operation_name: str, attempt_ref: str
) -> str:
    return "provider_unit_" + canonical_hash(
        {
            "operation_ref": operation_ref,
            "operation_name": operation_name,
            "attempt_ref": attempt_ref,
        }
    )[:64]


def _observation_from_projection(
    projection: RootCommandOutputProjection,
    *,
    sequence: int,
    stream_ref: str,
    invocation_hash: str,
    file_identity: str,
    recorded_at: float,
) -> StageRootObservation:
    cursor = _encode_cursor(
        stream_ref,
        invocation_hash=invocation_hash,
        file_identity=file_identity,
        sequence=sequence,
    )
    return StageRootObservation(
        event_ref="stage_root_observation:"
        + canonical_hash(
            {
                "stream_ref": stream_ref,
                "sequence": sequence,
                "kind": projection.kind,
                "text": projection.text,
            }
        ),
        cursor=cursor,
        sequence=sequence,
        kind=projection.kind,
        stream=projection.stream,
        text=projection.text,
        recorded_at=recorded_at,
        redacted=projection.redacted,
        truncated=projection.truncated,
        dropped_bytes=projection.dropped_bytes,
        dropped_events=projection.dropped_events,
    )


def _encode_cursor(
    stream_ref: str,
    *,
    invocation_hash: str,
    file_identity: str,
    sequence: int,
) -> str:
    payload = {
        "stream_ref": stream_ref,
        "invocation_hash": invocation_hash,
        "file_identity": file_identity,
        "sequence": sequence,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).decode("ascii").rstrip("=")
    return encoded + "." + canonical_hash(payload)


def _decode_cursor(
    value: str | None,
    *,
    expected_stream_ref: str,
    expected_invocation_hash: str,
    expected_file_identity: str,
) -> int | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 512:
        raise StageRootObservationError("stage_root_observation_cursor_invalid")
    encoded, separator, digest = value.partition(".")
    if not separator or not encoded or not digest:
        raise StageRootObservationError("stage_root_observation_cursor_invalid")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StageRootObservationError(
            "stage_root_observation_cursor_invalid"
        ) from error
    sequence = payload.get("sequence") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {"stream_ref", "invocation_hash", "file_identity", "sequence"}
        or payload.get("stream_ref") != expected_stream_ref
        or payload.get("invocation_hash") != expected_invocation_hash
        or payload.get("file_identity") != expected_file_identity
        or canonical_hash(payload) != digest
        or not isinstance(sequence, int)
        or isinstance(sequence, bool)
        or sequence < 0
    ):
        raise StageRootObservationError("stage_root_observation_cursor_invalid")
    return sequence


def _observation_status(run_status: str) -> str:
    return {
        "running": "live",
        "suspended": "waiting",
        "suspended_fenced": "replaced",
        "completed": "terminal",
        "failed": "terminal",
    }.get(run_status, "waiting")


def _raw_root_session_ref(
    stdout_path: Path,
    *,
    sealed_native_session_ref: str | None,
) -> str | None:
    try:
        with stdout_path.open("rb") as stream:
            first_line = stream.readline(_MAX_JSONL_LINE_BYTES + 1)
    except OSError as error:
        raise StageRootObservationError(
            "stage_root_observation_spool_unavailable"
        ) from error
    if not first_line:
        return sealed_native_session_ref
    if (
        len(first_line) > _MAX_JSONL_LINE_BYTES
        or not first_line.endswith(b"\n")
    ):
        raise StageRootObservationError("stage_root_observation_spool_unavailable")
    try:
        event = json.loads(first_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StageRootObservationError(
            "stage_root_observation_spool_unavailable"
        ) from error
    native_session_ref = event.get("thread_id") if isinstance(event, dict) else None
    if (
        not isinstance(event, dict)
        or event.get("type") != "thread.started"
        or event.get("parent_thread_id") not in (None, "")
        or not isinstance(native_session_ref, str)
        or not native_session_ref
        or (
            sealed_native_session_ref is not None
            and native_session_ref != sealed_native_session_ref
        )
    ):
        raise StageRootObservationError("stage_root_observation_spool_unavailable")
    return native_session_ref


def _decode_raw_page(raw: bytes) -> tuple[str, int]:
    end = len(raw)
    while end > 0:
        try:
            return raw[:end].decode("utf-8"), end
        except UnicodeDecodeError as error:
            end = error.start if error.start > 0 else end - 1
    return "", 0


def _raw_stream_ref(
    scope: dict[str, object],
    *,
    phase: str | None,
    invocation_hash: str | None,
    file_identity: str | None,
) -> str:
    return "stage-raw-output:" + canonical_hash(
        {
            "run_ref": scope.get("run_ref"),
            "attempt_ref": scope.get("attempt_ref"),
            "attempt_generation": scope.get("attempt_generation"),
            "unit_ref": scope.get("unit_ref"),
            "phase": phase,
            "invocation_hash": invocation_hash,
            "file_identity": file_identity,
        }
    )
