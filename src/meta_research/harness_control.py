"""Durable mechanical cancellation for native Harness provider operations."""

from __future__ import annotations

import re
from pathlib import Path

from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ProviderSupervisorError,
    SupervisorFileLock,
    ensure_transport_key,
    read_transport_envelope,
    read_supervisor_request,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervisor_request_never_started,
    write_supervisor_stop_request,
)


class HarnessCancellationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DurableHarnessOperationCanceller:
    """Cancel one exact sealed Harness effect and verify its terminal boundary."""

    _READY_SCHEMA = "meta-research/provider-supervisor-ready/v2"

    def __init__(self, workspace: Path) -> None:
        if not workspace.is_absolute() or workspace.is_symlink():
            raise HarnessCancellationError("harness_cancel_workspace_invalid")
        try:
            self._workspace = workspace.resolve()
            _key_path, self._key = ensure_transport_key(self._workspace)
        except (OSError, ProviderSupervisorError) as error:
            raise HarnessCancellationError(
                "harness_cancel_transport_invalid"
            ) from error

    def cancel_operation(self, invocation_hash: str) -> bool:
        if (
            type(invocation_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", invocation_hash) is None
        ):
            raise HarnessCancellationError("harness_cancel_identity_invalid")
        transport_invocation_hash = self._resolve_transport_invocation_hash(
            invocation_hash
        )
        operation = (
            self._workspace
            / "provider-operations"
            / transport_invocation_hash[:2]
            / transport_invocation_hash
        )
        if not operation.is_dir() or operation.is_symlink():
            raise HarnessCancellationError("provider_terminal_proof_missing")
        try:
            request = read_supervisor_request(
                operation / "supervisor-request.json",
                self._key,
            )
            if (
                request.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA_V2
                or request.get("invocation_hash") != transport_invocation_hash
            ):
                raise ProviderSupervisorError(
                    "provider_supervisor_request_invalid"
                )
            receipt_path = operation / "supervisor-exit.json"
            proof_error = None
            if receipt_path.is_file():
                try:
                    self._verify_terminal(operation, transport_invocation_hash)
                except (OSError, ProviderSupervisorError) as error:
                    proof_error = error
                else:
                    return True
            write_supervisor_stop_request(
                operation / "supervisor-stop.json",
                key=self._key,
                invocation_hash=transport_invocation_hash,
            )
            ready_path = operation / "supervisor-ready.json"
            if not ready_path.is_file():
                if supervisor_request_never_started(
                    operation,
                    key=self._key,
                    invocation_hash=transport_invocation_hash,
                    request_schema=SUPERVISOR_REQUEST_SCHEMA_V2,
                ):
                    return True
                if (
                    (operation / "provider-started.json").exists()
                    and not ready_path.is_file()
                ):
                    proof_error = ProviderSupervisorError(
                        "provider_supervisor_ready_invalid"
                    )
            else:
                try:
                    request_supervisor_stop(
                        operation,
                        key=self._key,
                        invocation_hash=transport_invocation_hash,
                        ready_schema=self._READY_SCHEMA,
                    )
                except (OSError, ProviderSupervisorError) as error:
                    proof_error = error
            lock = SupervisorFileLock(operation / "supervisor.lock")
            if not lock.acquire(blocking=False):
                if proof_error is not None:
                    raise HarnessCancellationError(
                        "provider_terminal_proof_missing"
                    ) from proof_error
                return False
            try:
                if receipt_path.is_file():
                    try:
                        self._verify_terminal(operation, transport_invocation_hash)
                    except (OSError, ProviderSupervisorError) as error:
                        proof_error = error
                    else:
                        return True
            finally:
                lock.release()
            from meta_research.streaming_provider_supervisor import reconcile_rebooted_supervisor
            try:
                reconcile_rebooted_supervisor(operation / "supervisor-request.json")
            except (OSError, ProviderSupervisorError) as error:
                proof_error = error
            # The free lock diagnoses a missing supervisor; it cannot prove
            # descendants stopped. Preserve any verification error as its cause.
            raise HarnessCancellationError("provider_terminal_proof_missing") from proof_error
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise HarnessCancellationError(
                "harness_cancel_transport_invalid"
            ) from error

    def _resolve_transport_invocation_hash(self, owner_or_transport_hash: str) -> str:
        binding_path = (
            self._workspace
            / "owner-invocation-bindings"
            / owner_or_transport_hash[:2]
            / f"{owner_or_transport_hash}.json"
        )
        if not binding_path.exists():
            return owner_or_transport_hash
        if binding_path.is_symlink() or binding_path.parent.is_symlink():
            raise HarnessCancellationError("harness_cancel_binding_invalid")
        try:
            binding = read_transport_envelope(binding_path, self._key)
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise HarnessCancellationError(
                "harness_cancel_binding_invalid"
            ) from error
        transport_hash = binding.get("transport_invocation_hash")
        if (
            set(binding)
            != {
                "schema_ref",
                "owner_invocation_hash",
                "transport_invocation_hash",
                "provider_operation_ref",
                "family",
            }
            or binding.get("schema_ref")
            != "meta-research/harness-operation-binding/v1"
            or binding.get("owner_invocation_hash") != owner_or_transport_hash
            or type(transport_hash) is not str
            or re.fullmatch(r"[0-9a-f]{64}", transport_hash) is None
            or type(binding.get("provider_operation_ref")) is not str
            or not binding["provider_operation_ref"]
            or len(binding["provider_operation_ref"]) > 128
            or binding.get("family") not in {"codex", "claude"}
        ):
            raise HarnessCancellationError("harness_cancel_binding_invalid")
        return transport_hash

    def _verify_terminal(self, operation: Path, invocation_hash: str) -> None:
        read_verified_exit_receipt(
            operation / "supervisor-exit.json",
            key=self._key,
            invocation_hash=invocation_hash,
            prompt_path=operation / "prompt.txt",
            schema_path=operation / "output-schema.json",
            stdout_path=operation / "stdout.jsonl",
            result_path=operation / "last-message.json",
            expected_schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )


__all__ = ["DurableHarnessOperationCanceller", "HarnessCancellationError"]
