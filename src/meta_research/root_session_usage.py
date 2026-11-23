"""Signed pointers and bounded usage projections for one root Session.

Provider observations are written beside one durable operation.  This module
adds an immutable pointer in the same provider workspace so a later reader can
join several operations without scanning a retired spool or trusting a path
alone.  The projection deliberately keeps unknown first-prompt, child-session,
team and billing scopes explicit.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    read_transport_envelope,
    read_transport_key_for_workspace,
    transport_file_sha256,
    write_transport_envelope,
)


ROOT_SESSION_OBSERVATION_SCHEMA = "meta-research/root-session-observation/v1"
ROOT_SESSION_USAGE_SCHEMA = "meta-research/root-session-usage/v1"
_MAX_INDEX_ENTRIES = 2048
_MAX_COMPACTION_ENTRIES = 512
_CACHE_SEMANTICS_CONTEXT_OCCUPANCY = (
    "cached tokens still occupy context; raw provider accounting preserved"
)


class RootSessionUsageError(ValueError):
    """A signed root-session observation pointer cannot be trusted."""


def _text(value: object, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise RootSessionUsageError(code)
    return value


def _workspace_for_operation(directory: Path) -> Path:
    try:
        operation_root = next(
            parent for parent in (directory, *directory.parents)
            if parent.name == "provider-operations"
        )
    except StopIteration as error:
        raise RootSessionUsageError("root_session_workspace_invalid") from error
    workspace = operation_root.parent
    if not workspace.is_dir() or workspace.is_symlink():
        raise RootSessionUsageError("root_session_workspace_invalid")
    return workspace


def _root_index_directory(workspace: Path, root_session_ref: str) -> Path:
    digest = hashlib.sha256(root_session_ref.encode("utf-8")).hexdigest()
    return workspace / "provider-observations" / "root-sessions" / digest


def _entry_identity(operation_ref: str, invocation_hash: str) -> str:
    return hashlib.sha256(
        (operation_ref + "\0" + invocation_hash).encode("utf-8")
    ).hexdigest()


def _index_path(workspace: Path, root_session_ref: str, operation_ref: str,
                invocation_hash: str) -> Path:
    return _root_index_directory(workspace, root_session_ref) / (
        _entry_identity(operation_ref, invocation_hash) + ".json"
    )


def _gap_path(workspace: Path, root_session_ref: str, operation_ref: str,
              invocation_hash: str) -> Path:
    return _root_index_directory(workspace, root_session_ref) / (
        _entry_identity(operation_ref, invocation_hash) + ".gap.json"
    )


def _relative_path(workspace: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError as error:
        raise RootSessionUsageError("root_session_observation_path_invalid") from error
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise RootSessionUsageError("root_session_observation_path_invalid")
    return relative.as_posix()


def _archived_observation_path(workspace: Path, path: Path) -> Path | None:
    """Return the durable archive path for a retired operation observation."""

    try:
        relative = path.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if (
        len(parts) != 4
        or parts[0] != "provider-operations"
        or parts[3] != "call-observation.json"
    ):
        return None
    archive = workspace / "provider-observations" / parts[1] / (
        parts[2] + "-call-observation.json"
    )
    try:
        archive.resolve().relative_to(workspace.resolve())
    except ValueError:
        return None
    return archive


def _observation_native_ref(observation: dict[str, object]) -> str | None:
    native = observation.get("native_session")
    if not isinstance(native, dict):
        return None
    value = native.get("session_id")
    return value if isinstance(value, str) and value else None


def _scoped_root_target(
    operation_scope: dict[str, object] | None,
) -> tuple[str, str] | None:
    if not isinstance(operation_scope, dict):
        return None
    root_session_ref = operation_scope.get("root_session_ref")
    operation_ref = operation_scope.get("operation_ref") or operation_scope.get("job_ref")
    if (
        not isinstance(root_session_ref, str)
        or not root_session_ref
        or not isinstance(operation_ref, str)
        or not operation_ref
    ):
        return None
    return root_session_ref, operation_ref


def write_root_session_observation_index(
    directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    observation: dict[str, object],
    operation_scope: dict[str, object] | None,
) -> dict[str, object] | None:
    """Write one idempotent signed pointer for a scoped operation.

    Missing scope is intentionally reported as no index.  Older operations
    remain readable through their existing sidecar and are not assigned a
    guessed root identity.
    """

    scoped = _scoped_root_target(operation_scope)
    if scoped is None:
        return None
    root_session_ref, operation_ref = scoped
    if not isinstance(invocation_hash, str) or len(invocation_hash) != 64:
        raise RootSessionUsageError("root_session_invocation_hash_invalid")
    observation_path = directory / "call-observation.json"
    if not observation_path.is_file() or observation_path.is_symlink():
        raise RootSessionUsageError("root_session_observation_missing")
    workspace = _workspace_for_operation(directory)
    scope = cast(dict[str, object], operation_scope)
    payload: dict[str, object] = {
        "schema_ref": ROOT_SESSION_OBSERVATION_SCHEMA,
        "root_session_ref": root_session_ref,
        "run_ref": scope.get("run_ref"),
        "attempt_ref": scope.get("attempt_ref"),
        "fence_ref": scope.get("fence_ref"),
        "operation_ref": operation_ref,
        "invocation_hash": invocation_hash,
        "observation_path": _relative_path(workspace, observation_path),
        "observation_sha256": transport_file_sha256(observation_path),
        "native_session_ref": _observation_native_ref(observation),
        "model_ref": scope.get("model_ref"),
        "prompt_utf8_bytes": observation.get("prompt_utf8_bytes"),
        "limited": bool(observation.get("limited", False)),
    }
    target = _index_path(workspace, root_session_ref, operation_ref, invocation_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        write_transport_envelope(target, payload, key)
    except ProviderSupervisorError as error:
        raise RootSessionUsageError("root_session_observation_index_conflict") from error
    _gap_path(workspace, root_session_ref, operation_ref, invocation_hash).unlink(missing_ok=True)
    (directory / "root-session-observation-gap.json").unlink(missing_ok=True)
    return {
        "status": "available",
        "schema_ref": ROOT_SESSION_OBSERVATION_SCHEMA,
        "path": _relative_path(workspace, target),
        "sha256": transport_file_sha256(target),
    }


def write_root_session_observation_gap(
    directory: Path,
    *,
    key: bytes,
    invocation_hash: str,
    operation_scope: dict[str, object] | None,
    failure_code: str,
) -> Path:
    """Record a signed root-session observation gap in the root index or operation dir."""

    payload: dict[str, object] = {
        "schema_ref": "meta-research/root-session-observation-unavailable/v1",
        "invocation_hash": invocation_hash,
        "status": "unavailable",
        "failure_code": failure_code or "root_session_observation_failed",
        "owner": "harness",
        "next_action": "reconcile_root_session_observation_index_before_using_cross_operation_usage",
    }
    scoped = _scoped_root_target(operation_scope)
    if scoped is not None and isinstance(invocation_hash, str) and len(invocation_hash) == 64:
        root_session_ref, operation_ref = scoped
        try:
            workspace = _workspace_for_operation(directory)
        except (RootSessionUsageError, OSError):
            workspace = None
        if workspace is not None:
            target = _gap_path(workspace, root_session_ref, operation_ref, invocation_hash)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_transport_envelope(
                target,
                {**payload, "root_session_ref": root_session_ref, "operation_ref": operation_ref},
                key,
            )
            return target
    fallback = directory / "root-session-observation-gap.json"
    write_transport_envelope(fallback, payload, key)
    return fallback


def _validate_pointer(pointer: dict[str, object], root_session_ref: str) -> tuple[str, str]:
    if pointer.get("schema_ref") != ROOT_SESSION_OBSERVATION_SCHEMA:
        raise RootSessionUsageError("root_session_observation_index_invalid")
    if pointer.get("root_session_ref") != root_session_ref:
        raise RootSessionUsageError("root_session_observation_scope_invalid")
    operation_ref = _text(pointer.get("operation_ref"), "root_session_operation_ref_invalid")
    invocation_hash = _text(pointer.get("invocation_hash"), "root_session_invocation_hash_invalid")
    if len(invocation_hash) != 64:
        raise RootSessionUsageError("root_session_invocation_hash_invalid")
    observation_path = pointer.get("observation_path")
    if not isinstance(observation_path, str) or not observation_path:
        raise RootSessionUsageError("root_session_observation_path_invalid")
    return operation_ref, invocation_hash


def read_root_session_observations(
    workspace: Path, root_session_ref: str,
) -> list[dict[str, object]]:
    """Read and verify all immutable pointers for one workspace/root pair."""

    _text(root_session_ref, "root_session_ref_invalid")
    index_directory = _root_index_directory(workspace, root_session_ref)
    if not index_directory.exists():
        return []
    if index_directory.is_symlink() or not index_directory.is_dir():
        raise RootSessionUsageError("root_session_observation_index_invalid")
    gap_paths: list[Path] = []
    paths: list[Path] = []
    for path in sorted(index_directory.glob("*.json"), key=lambda entry: entry.name):
        if path.name.endswith(".gap.json"):
            gap_paths.append(path)
        else:
            paths.append(path)
    if gap_paths:
        for gap_path in gap_paths:
            if gap_path.is_symlink() or not gap_path.is_file():
                raise RootSessionUsageError("root_session_observation_index_invalid")
        raise RootSessionUsageError("root_session_observation_unavailable")
    if len(paths) > _MAX_INDEX_ENTRIES:
        raise RootSessionUsageError("root_session_observation_index_limited")
    key = read_transport_key_for_workspace(workspace)
    observations: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise RootSessionUsageError("root_session_observation_index_invalid")
        try:
            pointer = read_transport_envelope(path, key)
        except ProviderSupervisorError as error:
            raise RootSessionUsageError("root_session_observation_index_invalid") from error
        operation_ref, invocation_hash = _validate_pointer(pointer, root_session_ref)
        identity = (operation_ref, invocation_hash)
        if identity in identities:
            raise RootSessionUsageError("root_session_observation_duplicate")
        identities.add(identity)
        raw_path = pointer.get("observation_path")
        if not isinstance(raw_path, str):
            raise RootSessionUsageError("root_session_observation_path_invalid")
        raw_observation_path = workspace / raw_path
        if raw_observation_path.is_symlink():
            raise RootSessionUsageError("root_session_observation_path_invalid")
        observation_path = raw_observation_path.resolve()
        if not observation_path.is_relative_to(workspace.resolve()):
            raise RootSessionUsageError("root_session_observation_path_invalid")
        if not observation_path.is_file() or observation_path.is_symlink():
            archived_path = _archived_observation_path(workspace, observation_path)
            if (
                archived_path is None
                or not archived_path.is_file()
                or archived_path.is_symlink()
            ):
                raise RootSessionUsageError("root_session_observation_unavailable")
            observation_path = archived_path
        if pointer.get("observation_sha256") != transport_file_sha256(observation_path):
            raise RootSessionUsageError("root_session_observation_hash_mismatch")
        observation = read_transport_envelope(observation_path, key)
        if observation.get("invocation_hash") != invocation_hash:
            raise RootSessionUsageError("root_session_observation_identity_invalid")
        ledger = observation.get("session_usage_ledger")
        binding = ledger.get("operation_binding") if isinstance(ledger, dict) else None
        if not isinstance(binding, dict) or binding.get("root_session_ref") != root_session_ref:
            raise RootSessionUsageError("root_session_observation_scope_invalid")
        if binding.get("operation_ref") not in {None, operation_ref}:
            raise RootSessionUsageError("root_session_observation_identity_invalid")
        observations.append({"pointer": pointer, "observation": observation})
    return observations


def aggregate_root_session_usage(
    root_session_ref: str, observations: list[dict[str, object]],
) -> dict[str, object]:
    """Deduplicate observed native response records across one root Session."""

    responses: dict[tuple[str, str], dict[str, int]] = {}
    response_order: dict[tuple[str, str], tuple[int, int]] = {}
    native_refs: set[str] = set()
    context_windows: set[int] = set()
    compactions: list[dict[str, object]] = []
    compaction_keys: set[tuple[str, int, int]] = set()
    compaction_count_complete = True
    cache_complete = True
    response_order_complete = True
    response_order_conflicts: set[tuple[str, str]] = set()
    response_order_positions: dict[tuple[str, int, int], tuple[str, str]] = {}
    response_order_position_conflict = False
    context_windows_seen = False
    context_windows_complete = True
    native_session_unavailable = False
    native_sample_incomplete = False
    cache_semantics: set[str] = set()
    compaction_sources_seen = False
    limited = False
    for item in observations:
        pointer = item.get("pointer")
        observation = item.get("observation")
        if not isinstance(pointer, dict) or not isinstance(observation, dict):
            raise RootSessionUsageError("root_session_observation_invalid")
        limited = limited or bool(pointer.get("limited")) or bool(observation.get("limited"))
        native = observation.get("native_session")
        native_ref = native.get("session_id") if isinstance(native, dict) else None
        native_status = native.get("status") if isinstance(native, dict) else None
        if (
            not isinstance(native, dict)
            or native_status not in {None, "available"}
            or not isinstance(native_ref, str)
            or not native_ref
        ):
            native_session_unavailable = True
            response_order_complete = False
            compaction_count_complete = False
            limited = True
        if isinstance(native, dict) and native.get("limited") is True:
            native_sample_incomplete = True
            response_order_complete = False
            compaction_count_complete = False
            limited = True
        if isinstance(native_ref, str) and native_ref:
            native_refs.add(native_ref)
        native_windows = native.get("model_context_windows") if isinstance(native, dict) else None
        if isinstance(native_windows, list):
            context_windows_seen = context_windows_seen or bool(native_windows)
            if not native_windows:
                context_windows_complete = False
            for window in native_windows:
                if type(window) is int and window > 0:
                    context_windows.add(window)
                else:
                    limited = True
                    context_windows_complete = False
        elif native_windows is not None:
            limited = True
            context_windows_complete = False
        else:
            context_windows_complete = False
        native_compactions = native.get("compactions") if isinstance(native, dict) else None
        if isinstance(native_compactions, list):
            compaction_sources_seen = True
            for compaction in native_compactions:
                if not isinstance(compaction, dict):
                    limited = True
                    compaction_count_complete = False
                    continue
                sequence = compaction.get("source_sequence")
                offset = compaction.get("source_byte_offset")
                timestamp = compaction.get("timestamp")
                if timestamp is not None and not isinstance(timestamp, str):
                    compaction_count_complete = False
                    limited = True
                if not (
                    isinstance(native_ref, str)
                    and native_ref
                    and type(sequence) is int
                    and sequence >= 0
                    and type(offset) is int
                    and offset >= 0
                ):
                    compaction_count_complete = False
                    limited = True
                if (
                    isinstance(native_ref, str)
                    and type(sequence) is int
                    and sequence >= 0
                    and type(offset) is int
                    and offset >= 0
                ):
                    identity = (native_ref, sequence, offset)
                    if identity in compaction_keys:
                        continue
                    compaction_keys.add(identity)
                if len(compactions) >= _MAX_COMPACTION_ENTRIES:
                    limited = True
                    compaction_count_complete = False
                    break
                compactions.append({
                    "native_session_ref": native_ref,
                    **{
                        key: compaction[key]
                        for key in ("source_byte_offset", "source_sequence", "timestamp")
                        if key in compaction
                    },
                })
        elif isinstance(native, dict):
            compaction_count_complete = False
        native_responses = native.get("model_responses") if isinstance(native, dict) else None
        if not isinstance(native_responses, list):
            response_order_complete = False
            continue
        if not native_responses:
            response_order_complete = False
        for response in native_responses:
            if not isinstance(response, dict):
                limited = True
                response_order_complete = False
                continue
            response_ref = response.get("response_id")
            if not isinstance(native_ref, str) or not native_ref or not isinstance(response_ref, str) or not response_ref:
                limited = True
                response_order_complete = False
                continue
            values = {
                key: response[key]
                for key in ("input_tokens", "output_tokens", "cached_input_tokens")
                if type(response.get(key)) is int and response[key] >= 0
            }
            if "input_tokens" not in values or "output_tokens" not in values:
                limited = True
                response_order_complete = False
                continue
            if "cached_input_tokens" not in values:
                limited = True
                cache_complete = False
            semantics = response.get("cache_semantics")
            if isinstance(semantics, str) and semantics:
                cache_semantics.add(semantics)
            identity = (native_ref, response_ref)
            prior = responses.get(identity)
            if prior is not None:
                for key in ("input_tokens", "output_tokens"):
                    if prior.get(key) != values.get(key):
                        raise RootSessionUsageError("root_session_usage_conflict")
                prior_cached = prior.get("cached_input_tokens")
                current_cached = values.get("cached_input_tokens")
                if (
                    prior_cached is not None
                    and current_cached is not None
                    and prior_cached != current_cached
                ):
                    raise RootSessionUsageError("root_session_usage_conflict")
                values = {
                    **prior,
                    **values,
                }
            responses[identity] = values
            sequence = response.get("source_sequence")
            offset = response.get("source_byte_offset")
            if type(sequence) is int and sequence >= 0 and type(offset) is int and offset >= 0:
                prior_order = response_order.get(identity)
                if prior_order is None:
                    response_order[identity] = (sequence, offset)
                    position_identity = response_order_positions.get(
                        (native_ref, sequence, offset)
                    )
                    if position_identity is None:
                        response_order_positions[(native_ref, sequence, offset)] = identity
                    elif position_identity != identity:
                        response_order_position_conflict = True
                        response_order_complete = False
                        limited = True
                elif prior_order != (sequence, offset):
                    response_order_conflicts.add(identity)
                    response_order_complete = False
                    limited = True
            else:
                limited = True
                response_order_complete = False
    input_tokens = sum(item["input_tokens"] for item in responses.values())
    output_tokens = sum(item["output_tokens"] for item in responses.values())
    cached_tokens = (
        sum(item["cached_input_tokens"] for item in responses.values())
        if cache_complete
        else None
    )
    multiple_native_sessions = len(native_refs) > 1
    if multiple_native_sessions:
        limited = True
    first_request_input: int | None = None
    first_request_status = "unknown_response_order"
    if (
        responses
        and len(native_refs) == 1
        and len(response_order) == len(responses)
        and response_order_complete
        and not response_order_conflicts
        and not response_order_position_conflict
        and not native_session_unavailable
        and not native_sample_incomplete
    ):
        first_identity = min(response_order, key=response_order.__getitem__)
        first_request_input = responses[first_identity]["input_tokens"]
        first_request_status = "observed_native_prefix_order"
    peak_request_input = max(
        (item["input_tokens"] for item in responses.values()), default=None
    )
    compactions.sort(
        key=lambda item: (
            item.get("native_session_ref")
            if isinstance(item.get("native_session_ref"), str)
            else "",
            item.get("source_sequence")
            if type(item.get("source_sequence")) is int
            else 2**63 - 1,
            item.get("source_byte_offset")
            if type(item.get("source_byte_offset")) is int
            else 2**63 - 1,
            item.get("timestamp") if isinstance(item.get("timestamp"), str) else "",
        )
    )
    return {
        "schema_ref": ROOT_SESSION_USAGE_SCHEMA,
        "root_session_ref": root_session_ref,
        "status": (
            "partial_native_session_unavailable"
            if native_session_unavailable
            else "partial_multiple_native_sessions"
            if multiple_native_sessions
            else "observed" if responses else "unknown"
        ),
        "operation_count": len(observations),
        "native_session_refs": sorted(native_refs),
        "deduplicated_response_count": len(responses),
        "cumulative_input_tokens_observed": (
            input_tokens
            if responses and not multiple_native_sessions and not native_session_unavailable
            else None
        ),
        "cumulative_output_tokens_observed": (
            output_tokens
            if responses and not multiple_native_sessions and not native_session_unavailable
            else None
        ),
        "cumulative_cached_input_tokens_observed": (
            cached_tokens
            if responses and not multiple_native_sessions and not native_session_unavailable
            else None
        ),
        "first_model_request_input_tokens_observed": first_request_input,
        "first_model_request_status": first_request_status,
        "peak_single_request_input_tokens_observed": (
            peak_request_input
            if responses and not multiple_native_sessions and not native_session_unavailable
            else None
        ),
        "peak_cached_prefix_tokens_observed": (
            max(
                (item["cached_input_tokens"] for item in responses.values()),
                default=None,
            )
            if (
                responses
                and cache_complete
                and not multiple_native_sessions
                and not native_session_unavailable
            )
            else None
        ),
        "cached_prefix_semantics": (
            sorted(cache_semantics)
            if cache_semantics
            else "unknown_provider_cache_scope"
        ),
        "model_context_windows_observed": sorted(context_windows),
        "model_context_windows_status": (
            "observed"
            if context_windows_seen
            and context_windows_complete
            and not multiple_native_sessions
            and not native_session_unavailable
            and not native_sample_incomplete
            else "unknown"
        ),
        "compactions_observed": compactions,
        "compaction_count_observed": len(compactions),
        "compaction_count_status": (
            "unknown_no_compaction_source"
            if not compaction_sources_seen
            else "partial_multiple_native_sessions"
            if multiple_native_sessions
            else "unknown_native_session_unavailable"
            if native_session_unavailable
            else "observed_exact"
            if compaction_count_complete
            else "lower_bound"
        ),
        "cached_prefix_counts_toward_context_window": (
            True
            if (
                responses
                and cache_complete
                and cache_semantics == {_CACHE_SEMANTICS_CONTEXT_OCCUPANCY}
            )
            else None
        ),
        "first_business_prompt": {
            "status": "unknown_without_explicit_operation_order",
            "utf8_bytes": None,
        },
        "child_sessions": {"status": "unknown_child_usage_not_indexed"},
        "team_total": {"status": "unknown_cross_root_aggregation_not_indexed"},
        "billing": {"status": "unknown_provider_pricing_not_bound"},
        "limited": limited,
        "cumulative_semantics_verified": False,
    }


__all__ = [
    "ROOT_SESSION_OBSERVATION_SCHEMA",
    "ROOT_SESSION_USAGE_SCHEMA",
    "RootSessionUsageError",
    "aggregate_root_session_usage",
    "read_root_session_observations",
    "write_root_session_observation_gap",
    "write_root_session_observation_index",
]
