from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from meta_research.owners.common import canonical_hash
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    ProviderSupervisorError,
    read_transport_key_for_operation,
    read_verified_exit_receipt,
)


_INVOCATION_HASH = re.compile(r"[0-9a-f]{64}")
_MAX_CACHED_STREAMS = 4
_SOURCE_READ_BYTES = 1024 * 1024
_PAGE_MIN_BYTES = 4
_PAGE_MAX_BYTES = 256 * 1024


class TargetRawOutputUnavailable(RuntimeError):
    """The private stdout view is unavailable; Target execution is unaffected."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TargetRawOutputPage:
    operation_ref: str
    transport_invocation_hash: str
    stream_ref: str
    status: str
    text: str
    offset: int
    next_offset: int
    mapped_bytes: int
    source_bytes: int
    has_more: bool
    source_caught_up: bool
    root_native_session_ref: str | None
    exact: bool = True
    unredacted: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": "meta-research/target-raw-output-page/v1",
            "operation_ref": self.operation_ref,
            "transport_invocation_hash": self.transport_invocation_hash,
            "stream_ref": self.stream_ref,
            "status": self.status,
            "text": self.text,
            "offset": self.offset,
            "next_offset": self.next_offset,
            "mapped_bytes": self.mapped_bytes,
            "source_bytes": self.source_bytes,
            "has_more": self.has_more,
            "source_caught_up": self.source_caught_up,
            "root_native_session_ref": self.root_native_session_ref,
            "exact": self.exact,
            "unredacted": self.unredacted,
        }


@dataclass
class _MappingState:
    source_identity: tuple[int, int] | None = None
    source_offset: int = 0
    source_buffer: bytes = b""
    root_native_session_ref: str | None = None
    mapped: bytearray = field(default_factory=bytearray)
    failure_code: str | None = None


class TargetRawOutputStore:
    """Page the exact private Codex stdout JSONL on demand.

    The reader is observer-only. It validates the bound root stream, but does
    not summarize or discard Provider events. It never edits the spool or an
    Agent workspace, and any read failure is isolated from the Target Run. The
    bounded LRU avoids retaining every historical stream in memory.
    """

    def __init__(self, supervisor_workspace: Path) -> None:
        self._workspace = supervisor_workspace.resolve()
        self._operation_bindings: dict[str, tuple[str, str]] = {}
        self._states: OrderedDict[str, _MappingState] = OrderedDict()
        self._lock = threading.RLock()

    def bind_operation(
        self,
        operation_ref: str,
        transport_invocation_hash: str,
        *,
        family: str,
    ) -> None:
        if not operation_ref or len(operation_ref) > 128:
            raise TargetRawOutputUnavailable("target_raw_output_operation_invalid")
        if _INVOCATION_HASH.fullmatch(transport_invocation_hash) is None:
            raise TargetRawOutputUnavailable("target_raw_output_binding_invalid")
        if family != "codex":
            raise TargetRawOutputUnavailable("target_raw_output_family_unsupported")
        binding = (transport_invocation_hash, family)
        with self._lock:
            existing = self._operation_bindings.get(operation_ref)
            if existing is not None and existing != binding:
                raise TargetRawOutputUnavailable(
                    "target_raw_output_binding_conflict"
                )
            self._operation_bindings[operation_ref] = binding

    def bind_verified_transport_receipt(
        self,
        operation_ref: str,
        receipt: object,
    ) -> None:
        """Recover one display binding only from its signed private spool.

        The receipt embedded in a Harness profile is an index, not authority.
        Re-reading the signed supervisor exit also rechecks the prompt, schema,
        stdout, and result hashes before the mapping becomes visible again.
        """

        indexed_receipt = receipt
        if (
            isinstance(receipt, dict)
            and receipt.get("provider_operation_ref") == operation_ref
            and set(receipt)
            == {
                "provider_operation_ref",
                "schema_ref",
                "spool_ref",
                "transport_invocation_hash",
                "supervisor_receipt_hash",
                "termination_reason",
                "provider_returncode",
            }
        ):
            indexed_receipt = {
                key: value
                for key, value in receipt.items()
                if key != "provider_operation_ref"
            }
        verified = self.verify_signed_transport_receipt(
            operation_ref,
            indexed_receipt,
        )
        self.bind_operation(
            operation_ref,
            cast(str, verified["transport_invocation_hash"]),
            family="codex",
        )

    def verify_signed_transport_receipt(
        self,
        operation_ref: str,
        receipt: object,
    ) -> dict[str, object]:
        """Re-open and verify the exact signed supervisor exit receipt.

        This read-only seam is also used by the Target recovery Owner.  The
        persisted Harness receipt remains only an index into the private spool;
        the signed exit envelope and every bound artifact hash are checked on
        every recovery-history read.
        """

        if (
            not isinstance(operation_ref, str)
            or not operation_ref
            or len(operation_ref) > 128
            or not isinstance(receipt, dict)
            or set(receipt)
            != {
                "schema_ref",
                "spool_ref",
                "transport_invocation_hash",
                "supervisor_receipt_hash",
                "termination_reason",
                "provider_returncode",
            }
            or receipt.get("schema_ref")
            != "meta-research/harness-provider-transport-receipt/v1"
            or not isinstance(receipt.get("transport_invocation_hash"), str)
            or not isinstance(receipt.get("supervisor_receipt_hash"), str)
            or not isinstance(receipt.get("termination_reason"), str)
            or not isinstance(receipt.get("provider_returncode"), int)
            or isinstance(receipt.get("provider_returncode"), bool)
        ):
            raise TargetRawOutputUnavailable(
                "target_raw_output_receipt_invalid"
            )
        invocation_hash = cast(str, receipt["transport_invocation_hash"])
        if (
            _INVOCATION_HASH.fullmatch(invocation_hash) is None
            or receipt.get("spool_ref") != "provider-spool:" + invocation_hash
            or _INVOCATION_HASH.fullmatch(
                cast(str, receipt["supervisor_receipt_hash"])
            )
            is None
        ):
            raise TargetRawOutputUnavailable(
                "target_raw_output_receipt_invalid"
            )
        directory = self._source_path(invocation_hash).parent
        try:
            _key_path, key = read_transport_key_for_operation(directory)
            signed_receipt, envelope = read_verified_exit_receipt(
                directory / "supervisor-exit.json",
                key=key,
                invocation_hash=invocation_hash,
                prompt_path=directory / "prompt.txt",
                schema_path=directory / "output-schema.json",
                stdout_path=directory / "stdout.jsonl",
                result_path=directory / "last-message.json",
                expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
            )
        except (OSError, ProviderSupervisorError) as error:
            raise TargetRawOutputUnavailable(
                "target_raw_output_receipt_unavailable"
            ) from error
        if (
            canonical_hash(envelope) != receipt["supervisor_receipt_hash"]
            or signed_receipt.get("termination_reason")
            != receipt["termination_reason"]
            or signed_receipt.get("returncode")
            != receipt["provider_returncode"]
        ):
            raise TargetRawOutputUnavailable(
                "target_raw_output_receipt_invalid"
            )
        return cast(dict[str, object], dict(receipt))

    def query(
        self,
        operation_ref: str,
        *,
        after: int = 0,
        limit: int = 64 * 1024,
        expected_native_session_ref: str | None = None,
        terminal: bool = False,
    ) -> TargetRawOutputPage:
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise TargetRawOutputUnavailable("target_raw_output_cursor_invalid")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not _PAGE_MIN_BYTES <= limit <= _PAGE_MAX_BYTES
        ):
            raise TargetRawOutputUnavailable("target_raw_output_limit_invalid")
        with self._lock:
            binding = self._operation_bindings.get(operation_ref)
            if binding is None:
                raise TargetRawOutputUnavailable(
                    "target_raw_output_binding_unavailable"
                )
            invocation_hash, family = binding
            if family != "codex":
                raise TargetRawOutputUnavailable(
                    "target_raw_output_family_unsupported"
                )
            state = self._state(invocation_hash)
            source_path = self._source_path(invocation_hash)
            source_size = self._advance(
                state,
                source_path,
                expected_native_session_ref=expected_native_session_ref,
                final=terminal,
            )
            if state.failure_code is not None:
                raise TargetRawOutputUnavailable(state.failure_code)
            if after > len(state.mapped):
                raise TargetRawOutputUnavailable(
                    "target_raw_output_cursor_stale"
                )
            if (
                after < len(state.mapped)
                and state.mapped[after] & 0b1100_0000 == 0b1000_0000
            ):
                raise TargetRawOutputUnavailable(
                    "target_raw_output_cursor_invalid"
                )
            end = min(len(state.mapped), after + limit)
            while end > after:
                try:
                    text = bytes(state.mapped[after:end]).decode("utf-8")
                except UnicodeDecodeError:
                    end -= 1
                    continue
                break
            else:
                text = ""
                end = after
            if after < len(state.mapped) and end == after:
                # A server-issued cursor is always a UTF-8 boundary and four
                # bytes can hold every scalar value, so a non-empty mapping
                # must advance. Never issue a legal zero-progress page.
                raise TargetRawOutputUnavailable(
                    "target_raw_output_cursor_invalid"
                )
            source_caught_up = state.source_offset >= source_size
            has_more = end < len(state.mapped) or not source_caught_up
            return TargetRawOutputPage(
                operation_ref=operation_ref,
                transport_invocation_hash=invocation_hash,
                stream_ref="target-raw-output:" + invocation_hash,
                status=("complete" if terminal and source_caught_up else "live"),
                text=text,
                offset=after,
                next_offset=end,
                mapped_bytes=len(state.mapped),
                source_bytes=source_size,
                has_more=has_more,
                source_caught_up=source_caught_up,
                root_native_session_ref=state.root_native_session_ref,
            )

    def _state(self, invocation_hash: str) -> _MappingState:
        state = self._states.pop(invocation_hash, None)
        if state is None:
            state = _MappingState()
        self._states[invocation_hash] = state
        while len(self._states) > _MAX_CACHED_STREAMS:
            self._states.popitem(last=False)
        return state

    def _source_path(self, invocation_hash: str) -> Path:
        provider_root = (self._workspace / "provider-operations").resolve()
        source = (
            provider_root
            / invocation_hash[:2]
            / invocation_hash
            / "stdout.jsonl"
        )
        resolved_source = source.resolve()
        if not resolved_source.is_relative_to(provider_root):
            raise TargetRawOutputUnavailable("target_raw_output_path_invalid")
        return resolved_source

    def _advance(
        self,
        state: _MappingState,
        source_path: Path,
        *,
        expected_native_session_ref: str | None,
        final: bool,
    ) -> int:
        try:
            stat = source_path.stat()
        except FileNotFoundError:
            return 0
        except OSError as error:
            raise TargetRawOutputUnavailable(
                "target_raw_output_source_unavailable"
            ) from error
        identity = (int(stat.st_dev), int(stat.st_ino))
        if state.source_identity is None:
            state.source_identity = identity
        elif state.source_identity != identity or stat.st_size < state.source_offset:
            state.failure_code = "target_raw_output_source_changed"
            return int(stat.st_size)
        remaining = min(
            _SOURCE_READ_BYTES,
            max(0, int(stat.st_size) - state.source_offset),
        )
        if remaining:
            try:
                with source_path.open("rb") as stream:
                    stream.seek(state.source_offset)
                    chunk = stream.read(remaining)
            except OSError as error:
                raise TargetRawOutputUnavailable(
                    "target_raw_output_source_unavailable"
                ) from error
            state.source_offset += len(chunk)
            state.source_buffer += chunk
        lines = state.source_buffer.split(b"\n")
        state.source_buffer = lines.pop()
        unterminated: bytes | None = None
        if final and state.source_offset >= int(stat.st_size) and state.source_buffer:
            unterminated = state.source_buffer
            state.source_buffer = b""
        for line in lines:
            self._consume_line(
                state,
                line,
                expected_native_session_ref=expected_native_session_ref,
                terminated=True,
            )
            if state.failure_code is not None:
                break
        if unterminated is not None and state.failure_code is None:
            self._consume_line(
                state,
                unterminated,
                expected_native_session_ref=expected_native_session_ref,
                terminated=False,
            )
        return int(stat.st_size)

    @staticmethod
    def _consume_line(
        state: _MappingState,
        line: bytes,
        *,
        expected_native_session_ref: str | None,
        terminated: bool,
    ) -> None:
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            state.failure_code = "target_raw_output_event_invalid"
            return
        if not isinstance(value, dict):
            state.failure_code = "target_raw_output_event_invalid"
            return
        event = cast(dict[str, object], value)
        if state.root_native_session_ref is None:
            native_ref = event.get("thread_id")
            if (
                event.get("type") != "thread.started"
                or event.get("parent_thread_id") not in (None, "")
                or not isinstance(native_ref, str)
                or not native_ref
            ):
                state.failure_code = "target_raw_output_root_identity_invalid"
                return
            state.root_native_session_ref = native_ref
        if (
            expected_native_session_ref is not None
            and state.root_native_session_ref is not None
            and state.root_native_session_ref != expected_native_session_ref
        ):
            state.failure_code = "target_raw_output_root_identity_mismatch"
            return
        state.mapped.extend(line)
        if terminated:
            state.mapped.extend(b"\n")


__all__ = [
    "TargetRawOutputPage",
    "TargetRawOutputStore",
    "TargetRawOutputUnavailable",
]
