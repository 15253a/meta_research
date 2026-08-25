"""Durable mechanical cancellation for native Harness provider operations."""

from __future__ import annotations

import re
from pathlib import Path

from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ProviderSupervisorError,
    ensure_transport_key,
    read_supervisor_request,
    read_verified_exit_receipt,
    request_supervisor_stop,
    supervisor_request_never_started,
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
        operation = (
            self._workspace
            / "provider-operations"
            / invocation_hash[:2]
            / invocation_hash
        )
        if not operation.is_dir() or operation.is_symlink():
            return False
        try:
            request = read_supervisor_request(
                operation / "supervisor-request.json",
                self._key,
            )
            if (
                request.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA_V2
                or request.get("invocation_hash") != invocation_hash
            ):
                raise ProviderSupervisorError(
                    "provider_supervisor_request_invalid"
                )
            receipt_path = operation / "supervisor-exit.json"
            if receipt_path.is_file():
                self._verify_terminal(operation, invocation_hash)
                return True
            ready_path = operation / "supervisor-ready.json"
            if not ready_path.is_file():
                return supervisor_request_never_started(
                    operation,
                    key=self._key,
                    invocation_hash=invocation_hash,
                    request_schema=SUPERVISOR_REQUEST_SCHEMA_V2,
                )
            if not request_supervisor_stop(
                operation,
                key=self._key,
                invocation_hash=invocation_hash,
                ready_schema=self._READY_SCHEMA,
            ):
                return False
            if receipt_path.is_file():
                self._verify_terminal(operation, invocation_hash)
            return True
        except (OSError, UnicodeDecodeError, ProviderSupervisorError) as error:
            raise HarnessCancellationError(
                "harness_cancel_transport_invalid"
            ) from error

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
