"""Reconcile signed stopped operations without changing frozen provider bindings."""

import hashlib

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.provider_supervisor import (
    ProviderSupervisorError, read_transport_key_for_operation, read_verified_exit_receipt,
)
from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    _provider_hard_ceiling_checkpoint,
    _write_exit_marker,
    _verified_operation_inputs, _read_spool_text, _verified_native_session,
)
from meta_research.stage_root_observations import (
    StageRootObservationError,
    StageRootObservationReader,
)


class StoppedStageProviderRecovery:
    """Resolve only the current Owner-bound operation and verify its terminal seal."""

    def __init__(self, reader: StageRootObservationReader) -> None:
        self._reader = reader

    def __call__(self, run_ref: str) -> dict[str, object] | None:
        try:
            scope = self._reader._validated_scope(
                self._reader._scope_lookup(run_ref), run_ref=run_ref
            )
            if scope["status"] != "running" or scope["operation_ref"] is None:
                return None
            operation = self._reader._resolve_operation(scope)
            if operation is None:
                return None
            directory, invocation_hash, _phase, _session = operation
            if not (directory / "supervisor-exit.json").is_file():
                return None
            marker = _write_exit_marker(directory, invocation_hash=invocation_hash)
            if marker.get("termination_reason") != "stopped":
                return None
            return _provider_hard_ceiling_checkpoint(marker)
        except (StageRootObservationError, IdeaSkillUnavailable):
            # Missing, stale, or unverified evidence cannot authorize a fence.
            return None

    def read_for_operator_resume(self, run_ref: str) -> dict[str, object] | None:
        """Verify a paused primary's exact stopped transport without replaying it."""
        try:
            scope = self._reader._validated_scope(
                self._reader._scope_lookup(run_ref), run_ref=run_ref
            )
            if (scope["status"] != "completed"
                    or scope["run_kind"] != "reasoning_stage"
                    or scope["unit_kind"] != "reasoning_primary"):
                return None
            operation = self._reader._resolve_operation(scope)
            if operation is None:
                raise OwnerConflict("operator_stop_checkpoint_unavailable")
            directory, invocation_hash, phase, expected_native = operation
            if phase != "primary":
                raise OwnerConflict("operator_stop_checkpoint_invalid")
            receipt_path = directory / "supervisor-exit.json"
            if not receipt_path.exists():
                raise OwnerConflict("operator_stop_checkpoint_unavailable")
            _, _, limits = _verified_operation_inputs(directory, invocation_hash=invocation_hash)
            _, key = read_transport_key_for_operation(directory)
            receipt, envelope = read_verified_exit_receipt(
                receipt_path, key=key, invocation_hash=invocation_hash,
                prompt_path=directory / "prompt.txt",
                schema_path=directory / "output-schema.json",
                stdout_path=directory / "stdout.jsonl",
                result_path=directory / "last-message.json",
            )
            if receipt["termination_reason"] != "stopped":
                return None
            stdout = _read_spool_text(directory / "stdout.jsonl", limits.stream_max_bytes)
            if hashlib.sha256(stdout.encode("utf-8")).hexdigest() != receipt["stdout_file_hash"]:
                raise OwnerConflict("operator_stop_checkpoint_invalid")
            native = _verified_native_session(stdout, expected=expected_native)
            return {
                "schema_ref": "meta-research/operator-stopped-primary/v1",
                "scope": scope,
                "native_session_ref": native,
                "invocation_hash": invocation_hash,
                "supervisor_receipt_hash": canonical_hash(envelope),
                "provider_exit": receipt,
            }
        except (StageRootObservationError, IdeaSkillUnavailable, ProviderSupervisorError,
                OSError, UnicodeError, ValueError) as error:
            raise OwnerConflict("operator_stop_checkpoint_invalid") from error
