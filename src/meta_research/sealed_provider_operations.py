"""Verify frozen provider files and their signed transport results.

This module never launches provider work or decides Owner recovery. Native turn
reconciliation remains in native_codex_bridge; receipt signing and validation
remain in provider_supervisor.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
from typing import cast

from meta_research.codex_runtime import CODEX_LOCKED_VERSION
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import (
    PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES, ProviderSupervisorError,
    read_transport_key_for_operation, read_verified_exit_receipt,
    read_transport_envelope, write_exit_receipt,
)
from meta_research.quest_drafting import PROVIDER_STREAM_MAX_BYTES, _text_exceeds_limit

PROVIDER_OPERATION_SCHEMA = "meta-research/codex-provider-operation/v3"
_COMPLETION_ENVELOPE_MAX_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ProviderTransportLimits:
    """Immutable byte contract owned by one Codex Skill adapter."""

    prompt_max_bytes: int
    stream_max_bytes: int
    result_max_bytes: int

    def __post_init__(self) -> None:
        for value in (
            self.prompt_max_bytes,
            self.stream_max_bytes,
            self.result_max_bytes,
        ):
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 < value <= PROVIDER_SUPERVISOR_MAX_CONTENT_BYTES
            ):
                raise ValueError("provider_transport_limits_invalid")

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_max_bytes": self.prompt_max_bytes,
            "stream_max_bytes": self.stream_max_bytes,
            "result_max_bytes": self.result_max_bytes,
        }


class ProviderOperationUnavailable(RuntimeError):
    """The production Skill Adapter could not return a verifiable result."""

    def __init__(
        self,
        code: str,
        *,
        recovery_checkpoint: dict[str, object] | None = None,
        rejected_candidate: dict[str, object] | None = None,
        rejected_native_session_ref: str | None = None,
        rejected_detail_code: str | None = None,
        rejected_feedback: tuple[str, ...] = (),
        native_session_ref: str | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.recovery_checkpoint = (
            None if recovery_checkpoint is None else dict(recovery_checkpoint)
        )
        self.rejected_candidate = (
            None if rejected_candidate is None else dict(rejected_candidate)
        )
        self.rejected_native_session_ref = rejected_native_session_ref
        self.rejected_detail_code = rejected_detail_code
        self.rejected_feedback = rejected_feedback
        self.native_session_ref = native_session_ref or rejected_native_session_ref


def _reject_non_finite_json_constant(_value: str) -> object:
    """Keep non-standard JSON numbers on the raw-candidate rejection path."""

    raise ValueError("non_finite_json_constant")


def _write_exclusive(path: Path, value: str) -> bool:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def operation_transport_limits(
    invocation: dict[str, object],
) -> ProviderTransportLimits:
    if invocation.get("schema_ref") != PROVIDER_OPERATION_SCHEMA:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    try:
        return ProviderTransportLimits(
            prompt_max_bytes=cast(int, invocation.get("prompt_max_bytes")),
            stream_max_bytes=cast(int, invocation.get("stream_max_bytes")),
            result_max_bytes=cast(int, invocation.get("result_max_bytes")),
        )
    except ValueError as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error


def read_sealed_invocation(
    path: Path,
    *,
    key: bytes,
) -> dict[str, object]:
    """Authenticate the stored payload without granting current execution."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
    if not isinstance(envelope, dict):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    invocation = envelope.get("payload")
    seal = envelope.get("seal")
    if not isinstance(invocation, dict) or not isinstance(seal, str):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    typed_invocation = cast(dict[str, object], invocation)
    expected_seal = hmac.new(
        key,
        canonical_json(typed_invocation).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    transport_mode = typed_invocation.get("transport_mode")
    operation_transport_limits(typed_invocation)
    if (
        set(envelope) != {"payload", "seal"}
        or not hmac.compare_digest(seal, expected_seal)
        or typed_invocation.get("schema_ref") != PROVIDER_OPERATION_SCHEMA
        or transport_mode not in {"durable_supervisor", "unreconciled_runner"}
    ):
        raise ProviderOperationUnavailable("codex_operation_identity_conflict")
    return typed_invocation


def read_invocation(
    path: Path,
    *,
    key: bytes,
    expected_base: dict[str, object],
) -> dict[str, object]:
    typed_invocation = read_sealed_invocation(path, key=key)
    expected_keys = {*expected_base, "transport_mode"}
    identity_fields = set(expected_base).difference(
        {
            "schema_ref",
            "prompt_max_bytes",
            "stream_max_bytes",
            "result_max_bytes",
        }
    )
    if (
        set(typed_invocation) != expected_keys
        or any(
            typed_invocation.get(name) != expected_base[name]
            for name in identity_fields
        )
    ):
        raise ProviderOperationUnavailable("codex_operation_identity_conflict")
    return typed_invocation


def _write_durable(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as destination:
            destination.write(value)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_exit_marker(
    directory: Path,
    *,
    invocation_hash: str,
) -> dict[str, object]:
    prompt_hash, output_schema_hash, transport_limits = verify_operation_inputs(
        directory,
        invocation_hash=invocation_hash,
    )
    receipt_path = directory / "supervisor-exit.json"
    if not receipt_path.exists():
        raise ProviderOperationUnavailable("codex_operation_reconciliation_pending")
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        receipt, envelope = read_verified_exit_receipt(
            receipt_path,
            key=transport_key,
            invocation_hash=invocation_hash,
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
        )
        stdout = read_spool_text(
            directory / "stdout.jsonl", transport_limits.stream_max_bytes
        )
    except (OSError, ProviderSupervisorError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
    result_path = directory / "last-message.json"
    termination_reason = cast(str, receipt["termination_reason"])
    provider_returncode = cast(int, receipt["returncode"])
    effective_returncode = (
        provider_returncode
        if termination_reason == "completed"
        else {
            "timeout": 124,
            "stopped": 143,
            "output_limit": 125,
            "descendant_process": 126,
            "launch_failed": 127,
        }[termination_reason]
    )
    marker: dict[str, object] = {
        "schema_ref": "meta-research/codex-provider-exit/v1",
        "invocation_hash": invocation_hash,
        "returncode": effective_returncode,
        "provider_returncode": provider_returncode,
        "termination_reason": termination_reason,
        "prompt_hash": prompt_hash,
        "output_schema_hash": output_schema_hash,
        "stdout_hash": canonical_hash(stdout),
        "result_file_hash": receipt["result_file_hash"],
        "supervisor_receipt_hash": canonical_hash(envelope),
    }
    encoded = canonical_json(marker)
    exit_path = directory / "exit.json"
    if not _write_exclusive(exit_path, encoded):
        try:
            persisted = exit_path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
        if persisted != encoded:
            raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    if marker["returncode"] == 0 and not result_path.is_file():
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    if termination_reason == "completed" and effective_returncode != 0:
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            detail = (event if event.get("type") == "error" else event.get("error")
                      if event.get("type") == "turn.failed" else None)
            message = detail.get("message") if isinstance(detail, dict) else None
            if isinstance(message, str) and message.replace("’", "'").startswith("You've hit your usage limit."):
                # Derived only after the signed terminal stdout is verified;
                # keep the existing sealed exit marker bytes unchanged.
                return {**marker, "failure_code": "codex_usage_limit"}
    return marker


def _write_local_exit_receipt(
    directory: Path,
    *,
    invocation_hash: str,
    returncode: int,
    input_bytes: int,
) -> None:
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        write_exit_receipt(
            directory / "supervisor-exit.json",
            key=transport_key,
            invocation_hash=invocation_hash,
            prompt_path=directory / "prompt.txt",
            schema_path=directory / "output-schema.json",
            stdout_path=directory / "stdout.jsonl",
            result_path=directory / "last-message.json",
            returncode=returncode,
            input_bytes=input_bytes,
        )
    except (OSError, ProviderSupervisorError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error


def _verified_success_exit(
    directory: Path,
    *,
    invocation_hash: str,
) -> dict[str, object]:
    marker = write_exit_marker(
        directory,
        invocation_hash=invocation_hash,
    )
    return _require_success_exit(marker)


def _require_success_exit(marker: dict[str, object]) -> dict[str, object]:
    returncode = marker.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    hard_ceiling = _provider_hard_ceiling_error(marker)
    if hard_ceiling is not None:
        raise hard_ceiling
    if returncode != 0:
        raise ProviderOperationUnavailable("codex_operation_failed")
    return marker


def _provider_hard_ceiling_error(
    marker: dict[str, object],
) -> ProviderOperationUnavailable | None:
    termination_reason = marker.get("termination_reason")
    returncode = marker.get("returncode")
    if not isinstance(returncode, int) or isinstance(returncode, bool):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    failure_code = "codex_usage_limit" if marker.get("failure_code") == "codex_usage_limit" else {
        "timeout": "codex_operation_timeout",
        "output_limit": "codex_operation_output_limit",
    }.get(termination_reason)
    if (
        failure_code is None
        and returncode != 0
        and termination_reason != "stopped"
    ):
        # Every signed terminal non-zero exit is a hard ceiling for this exact
        # provider operation, except a cooperative stop requested by runtime
        # control.  The more specific supervisor reason remains in the
        # checkpoint; the public failure code stays backward-compatible.
        failure_code = "codex_operation_failed"
    if failure_code is None:
        return None
    return ProviderOperationUnavailable(
        failure_code,
        recovery_checkpoint=hard_ceiling_checkpoint(marker),
    )


def hard_ceiling_checkpoint(
    marker: dict[str, object],
) -> dict[str, object]:
    evidence_fields = (
        "invocation_hash",
        "prompt_hash",
        "output_schema_hash",
        "stdout_hash",
        "supervisor_receipt_hash",
    )
    if any(
        not isinstance(marker.get(field), str) or not marker[field]
        for field in evidence_fields
    ):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    checkpoint = {
        "schema_ref": "meta-research/provider-hard-ceiling/v1",
        "termination_reason": marker.get("termination_reason"),
        **{field: marker[field] for field in evidence_fields},
        "result_file_hash": marker.get("result_file_hash"),
    }
    return checkpoint


def native_session_unavailable(directory, marker, native_session_ref):
    """Accept only a sealed, exact native no-rollout response from the locked CLI."""
    proof_path = directory / "native-resume-rejected.json"
    if (native_session_ref is None or marker.get("termination_reason") != "completed"
            or marker.get("returncode") != 70 or not proof_path.is_file()):
        return None
    try:
        _path, key = read_transport_key_for_operation(directory)
        proof = read_transport_envelope(proof_path, key)
        error = proof.get("error")
        expected_message = "no rollout found for thread id " + native_session_ref
        provider_argv = read_transport_envelope(directory / "native-provider-argv.json", key)["argv"]
        if (proof.get("supervisor_invocation_hash") != marker.get("invocation_hash")
                or proof.get("provider_argv_hash") != canonical_hash(provider_argv)
                or proof.get("method") != "thread/resume" or proof.get("thread_id") != native_session_ref
                or proof.get("provider_version_output") != "codex-cli " + CODEX_LOCKED_VERSION
                or not isinstance(error, dict) or type(error.get("code")) is not int
                or error["code"] != -32600 or error.get("message") != expected_message):
            return None
        stream = read_spool_text(directory / "stdout.jsonl", PROVIDER_STREAM_MAX_BYTES)
        if canonical_hash(stream) != marker.get("stdout_hash"):
            raise ProviderOperationUnavailable("codex_operation_spool_invalid")
        observed = [json.loads(line) for line in stream.splitlines() if line.strip()]
        event = {"type": "meta_research.native_resume_rejected", **proof}
        if proof.get("source") == "exec":
            # The result destination is a supervisor-owned FIFO. Only its final
            # signed empty-result hash proves no result bytes were produced.
            if (type(proof.get("exec_returncode")) is not int or proof["exec_returncode"] != 1
                or type(proof.get("exec_stdout_bytes")) is not int
                or proof["exec_stdout_bytes"] != 0
                or proof.get("exec_stderr") != "Error: thread/resume: thread/resume failed: " + expected_message + " (code -32600)\n"
                or marker.get("result_file_hash") != hashlib.sha256(b"").hexdigest()
                or observed != [event]):
                return None
        elif "source" in proof:
            return None
        if not any(record == event for record in observed):
            raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    except (OSError, ValueError, KeyError, TypeError, ProviderSupervisorError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
    checkpoint = {**hard_ceiling_checkpoint(marker),
        "schema_ref": "meta-research/provider-native-session-unavailable/v1",
        "native_session_ref": native_session_ref, "error_code": error["code"],
        "error_response_hash": canonical_hash(proof)}
    return ProviderOperationUnavailable("codex_native_session_unavailable", recovery_checkpoint=checkpoint,
                                native_session_ref=native_session_ref)


def _provider_terminal_contract_checkpoint(
    marker: dict[str, object], failure_code: str, detail_code: str
) -> dict[str, object]:
    checkpoint = hard_ceiling_checkpoint(marker)
    if checkpoint.get("termination_reason") != "completed":
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    return {
        **checkpoint,
        "schema_ref": "meta-research/provider-terminal-contract-failure/v1",
        "contract_failure_code": failure_code,
        "contract_failure_detail_code": detail_code,
    }


def read_business_input(
    directory: Path,
    invocation: dict[str, object],
    transport_limits: ProviderTransportLimits,
) -> dict[str, object]:
    try:
        text = read_spool_text(directory / "business-input.json", transport_limits.prompt_max_bytes)
        body = json.loads(text)
        if (not isinstance(body, dict) or canonical_json(body) != text
                or hashlib.sha256(text.encode("utf-8")).hexdigest() != invocation.get("operation_input_hash")):
            raise ProviderOperationUnavailable("codex_operation_spool_invalid")
        return body
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error


def verify_operation_inputs(
    directory: Path,
    *,
    invocation_hash: str,
) -> tuple[str, str, ProviderTransportLimits]:
    try:
        _key_path, transport_key = read_transport_key_for_operation(directory)
        envelope = json.loads(
            (directory / "invocation.json").read_text(encoding="utf-8")
        )
        if not isinstance(envelope, dict) or not isinstance(
            envelope.get("payload"), dict
        ):
            raise ProviderOperationUnavailable("codex_operation_spool_invalid")
        invocation = cast(dict[str, object], envelope["payload"])
        transport_limits = operation_transport_limits(invocation)
        prompt = read_spool_text(
            directory / "prompt.txt", transport_limits.prompt_max_bytes
        )
        schema_text = read_spool_text(
            directory / "output-schema.json", transport_limits.result_max_bytes
        )
        schema = json.loads(schema_text)
        if "operation_input_hash" in invocation:
            read_business_input(directory, invocation, transport_limits)
    except ProviderOperationUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProviderSupervisorError) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
    if (
        not isinstance(envelope, dict)
        or not isinstance(schema, dict)
        or not isinstance(envelope.get("payload"), dict)
        or not isinstance(envelope.get("seal"), str)
        or set(envelope) != {"payload", "seal"}
        or not hmac.compare_digest(
            cast(str, envelope["seal"]),
            hmac.new(
                transport_key,
                canonical_json(envelope["payload"]).encode("utf-8"),
                hashlib.sha256,
            ).hexdigest(),
        )
        or canonical_hash(envelope["payload"]) != invocation_hash
        or invocation.get("schema_ref")
        != PROVIDER_OPERATION_SCHEMA
        or invocation.get("prompt_hash") != (prompt_hash := canonical_hash(prompt))
        or invocation.get("output_schema_hash") != (schema_hash := canonical_hash(schema))
    ):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    return prompt_hash, schema_hash, transport_limits


def verify_native_session(stdout: str, *, expected: str | None) -> str:
    observed_refs: list[str] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "thread.started":
            continue
        observed = event.get("thread_id")
        if not isinstance(observed, str) or not observed:
            raise ProviderOperationUnavailable("codex_native_session_missing")
        observed_refs.append(observed)
    if not observed_refs:
        raise ProviderOperationUnavailable("codex_native_session_missing")
    unique_refs = set(observed_refs)
    if len(unique_refs) != 1:
        raise ProviderOperationUnavailable("codex_native_session_mismatch")
    observed = observed_refs[0]
    if expected is not None and observed != expected:
        raise ProviderOperationUnavailable("codex_native_session_mismatch")
    return observed


def _recover_failed_native_session(
    stdout: str,
    *,
    expected: str | None,
) -> str | None:
    """Recover only an unambiguous root Session from a failed provider turn."""

    if not isinstance(stdout, str):
        return None
    try:
        return verify_native_session(stdout, expected=expected)
    except ProviderOperationUnavailable:
        return None


def _with_failed_native_session(
    error: ProviderOperationUnavailable,
    *,
    native_session_ref: str | None,
) -> ProviderOperationUnavailable:
    if native_session_ref is None or error.native_session_ref is not None:
        return error
    return ProviderOperationUnavailable(
        error.code,
        recovery_checkpoint=error.recovery_checkpoint,
        rejected_candidate=error.rejected_candidate,
        rejected_native_session_ref=error.rejected_native_session_ref,
        rejected_detail_code=error.rejected_detail_code,
        rejected_feedback=error.rejected_feedback,
        native_session_ref=native_session_ref,
    )


def _write_completed_operation(
    directory: Path,
    *,
    invocation_hash: str,
    decoded: dict[str, object],
    native_session_ref: str | None,
    transport_limits: ProviderTransportLimits,
) -> None:
    exit_marker = _verified_success_exit(
        directory,
        invocation_hash=invocation_hash,
    )
    stdout = read_spool_text(
        directory / "stdout.jsonl", transport_limits.stream_max_bytes
    )
    verified_session = verify_native_session(
        stdout,
        expected=native_session_ref,
    )
    completion = {
        "schema_ref": "meta-research/codex-provider-operation-result/v1",
        "invocation_hash": invocation_hash,
        "result": decoded,
        "result_hash": canonical_hash(decoded),
        "stdout_hash": canonical_hash(stdout),
        "exit_hash": canonical_hash(exit_marker),
        "native_session_ref": verified_session,
    }
    _write_durable(directory / "completed.json", canonical_json(completion))


def read_completed_operation(
    directory: Path,
    *,
    invocation_hash: str,
    native_session_ref: str | None,
    transport_limits: ProviderTransportLimits,
) -> tuple[dict[str, object], str | None, str]:
    # A sealed native transport may stop while its actual turn remains
    # unresolved. Reconcile that exact turn; never promote transport failure
    # into permission to launch the same external work again.
    marker = write_exit_marker(directory, invocation_hash=invocation_hash)
    native_failure = native_session_unavailable(directory, marker, native_session_ref)
    if native_failure is not None:
        raise native_failure
    if ((marker.get("returncode") == 75
            or (marker.get("returncode") == 1 and marker.get("termination_reason") == "completed"))
            and (directory / "native-turn-start.json").is_file()):
        from meta_research.native_codex_bridge import reconcile_native_operation, NativeTerminalTurnError
        try:
            recovered = reconcile_native_operation(directory)
        except NativeTerminalTurnError as error:
            proof = error.checkpoint
            if (proof.get("supervisor_invocation_hash") != marker.get("invocation_hash")
                    or proof.get("native_session_ref") != error.native_session_ref
                    or proof.get("native_turn_status") not in {"failed", "interrupted"}
                    or native_session_ref is not None and error.native_session_ref != native_session_ref):
                raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
            checkpoint = {**hard_ceiling_checkpoint(marker), **proof}
            raise ProviderOperationUnavailable("codex_operation_failed", recovery_checkpoint=checkpoint,
                native_session_ref=error.native_session_ref) from error
        except subprocess.TimeoutExpired as error:
            raise ProviderOperationUnavailable("codex_operation_reconciliation_pending") from error
        except (OSError, ValueError, KeyError, TypeError, ProviderSupervisorError) as error:
            raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
        if recovered is None:
            raise ProviderOperationUnavailable("codex_operation_reconciliation_pending")
        final_text, recovered_session, stdout = recovered
        if native_session_ref is not None and recovered_session != native_session_ref:
            raise ProviderOperationUnavailable("codex_native_session_mismatch")
        if _text_exceeds_limit(final_text, transport_limits.result_max_bytes):
            raise ProviderOperationUnavailable("codex_output_too_large")
        try:
            decoded = json.loads(final_text, parse_constant=_reject_non_finite_json_constant)
        except (ValueError, TypeError) as error:
            raise ProviderOperationUnavailable("codex_output_invalid") from error
        if not isinstance(decoded, dict):
            raise ProviderOperationUnavailable("codex_output_invalid")
        return decoded, recovered_session, stdout
    try:
        exit_marker = _require_success_exit(marker)
    except ProviderOperationUnavailable as error:
        if error.recovery_checkpoint is None:
            raise
        recovered_session = None
        try:
            failed_stdout = read_spool_text(
                directory / "stdout.jsonl",
                transport_limits.stream_max_bytes,
            )
        except (OSError, UnicodeDecodeError, ProviderOperationUnavailable):
            pass
        else:
            recovered_session = _recover_failed_native_session(
                failed_stdout,
                expected=native_session_ref,
            )
        enriched = _with_failed_native_session(
            error,
            native_session_ref=recovered_session,
        )
        if enriched is error:
            raise
        raise enriched from error
    completion_path = directory / "completed.json"
    if not completion_path.exists():
        stdout_path = directory / "stdout.jsonl"
        result_path = directory / "last-message.json"
        if not stdout_path.exists() or not result_path.exists():
            raise ProviderOperationUnavailable("codex_operation_reconciliation_pending")
        try:
            stdout = read_spool_text(
                stdout_path, transport_limits.stream_max_bytes
            )
            decoded = json.loads(
                _read_result_text(
                    result_path,
                    result_max_bytes=transport_limits.result_max_bytes,
                ),
                parse_constant=_reject_non_finite_json_constant,
            )
        except (
            OSError,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ProviderOperationUnavailable("codex_output_invalid") from error
        if not isinstance(decoded, dict):
            raise ProviderOperationUnavailable("codex_output_invalid")
        recovered_session = verify_native_session(
            stdout,
            expected=native_session_ref,
        )
        _write_completed_operation(
            directory,
            invocation_hash=invocation_hash,
            decoded=cast(dict[str, object], decoded),
            native_session_ref=recovered_session,
            transport_limits=transport_limits,
        )
    try:
        completion = json.loads(
            read_spool_text(
                completion_path,
                transport_limits.result_max_bytes
                + _COMPLETION_ENVELOPE_MAX_BYTES,
            )
        )
        stdout = read_spool_text(
            directory / "stdout.jsonl", transport_limits.stream_max_bytes
        )
        persisted_result = json.loads(
            _read_result_text(
                directory / "last-message.json",
                result_max_bytes=transport_limits.result_max_bytes,
            ),
            parse_constant=_reject_non_finite_json_constant,
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as error:
        raise ProviderOperationUnavailable("codex_operation_spool_invalid") from error
    if not isinstance(completion, dict) or not isinstance(persisted_result, dict):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    decoded = completion.get("result")
    recovered_session = completion.get("native_session_ref")
    observed_session = verify_native_session(
        stdout,
        expected=native_session_ref,
    )
    if (
        not isinstance(decoded, dict)
        or completion.get("schema_ref")
        != "meta-research/codex-provider-operation-result/v1"
        or completion.get("invocation_hash") != invocation_hash
        or completion.get("result_hash") != canonical_hash(decoded)
        or persisted_result != decoded
        or completion.get("stdout_hash") != canonical_hash(stdout)
        or completion.get("exit_hash") != canonical_hash(exit_marker)
        or recovered_session != observed_session
    ):
        raise ProviderOperationUnavailable("codex_operation_spool_invalid")
    return (
        cast(dict[str, object], decoded),
        cast(str | None, recovered_session),
        stdout,
    )


def read_spool_text(path: Path, limit: int) -> str:
    if path.stat().st_size > limit:
        raise ProviderOperationUnavailable("codex_output_too_large")
    with path.open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise ProviderOperationUnavailable("codex_output_too_large")
    return value.decode("utf-8")


def _read_result_text(path: Path, *, result_max_bytes: int) -> str:
    if path.stat().st_size > result_max_bytes:
        raise ProviderOperationUnavailable("codex_output_too_large")
    with path.open("rb") as source:
        value = source.read(result_max_bytes + 1)
    if len(value) > result_max_bytes:
        raise ProviderOperationUnavailable("codex_output_too_large")
    return value.decode("utf-8")
