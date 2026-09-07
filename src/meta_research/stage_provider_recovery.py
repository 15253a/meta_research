"""Reconcile signed stopped operations without changing frozen provider bindings."""

from meta_research.idea_skill import (
    IdeaSkillUnavailable,
    _provider_hard_ceiling_checkpoint,
    _write_exit_marker,
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
