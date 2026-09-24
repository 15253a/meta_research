"""Launch the durable supervisor with an unbuffered provider stdout pipe.

Transport configuration stays outside the frozen provider adapter sources so
already-admitted runs can retain their exact input and result contracts.
"""

import errno
import json
import os
from pathlib import Path
import subprocess
import sys

from meta_research.provider_supervisor import (
    PROVIDER_OPERATION_ENV,
    SUPERVISOR_EXIT_SCHEMA_V2,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    ProviderProcessPlatform,
    SupervisorFileLock,
    _file_sha256,
    _validated_request_paths,
    read_supervisor_request,
    read_transport_envelope,
    read_transport_key_for_operation,
    write_exit_receipt,
    write_transport_envelope,
    ProviderSupervisorError,
    supervise,
)


class StreamingProviderProcessPlatform(ProviderProcessPlatform):
    def provider_spawn_options(self) -> dict[str, object]:
        # BufferedReader.read(64 KiB) waits for a full block or EOF. FileIO.read
        # returns the bytes currently available while keeping the same bounded
        # drain, signed receipts, process isolation, and cancellation behavior.
        return {**super().provider_spawn_options(), "bufsize": 0}



def _operation_processes_absent(request_path: Path) -> bool:
    """Prove absence of same-user supervisors and escaped provider descendants."""
    if os.name != "posix" or not Path("/proc").is_dir():
        return False
    request_token = str(request_path).encode("utf-8")
    provider_token = f"{PROVIDER_OPERATION_ENV}={request_path}".encode("utf-8")
    for directory in Path("/proc").glob("[0-9]*"):
        try:
            if int(directory.name) == os.getpid():
                continue  # This reconciliation supervisor owns the spool lock.
            if directory.stat().st_uid != os.getuid():
                continue
            argv = (directory / "cmdline").read_bytes().split(b"\0")
            environment = (directory / "environ").read_bytes().split(b"\0")
            if request_token in argv or provider_token in environment:
                return False
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            raise ProviderSupervisorError("provider_process_identity_unavailable") from error
    return True


def _seal_absent_provider_interruption(
    request_path: Path, *, process_platform: ProviderProcessPlatform
) -> bool:
    """Close a lost supervisor boundary as failure, without replaying its provider.

    ``stopped`` is the existing transport interruption contract. The separately
    signed recovery record distinguishes this absence proof from a user stop or
    an observed SIGTERM and preserves the original bridge return code, if any.
    No partial output is promoted to a successful result.
    """
    request_path = request_path.resolve()
    if process_platform.platform_name != "posix" or not Path("/proc").is_dir():
        return False
    _, key = read_transport_key_for_operation(request_path.parent)
    request = read_supervisor_request(request_path, key)
    # Generic v2 is the resident Harness transport. Legacy stage contracts stay
    # on their existing exact-version recovery paths.
    if request.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA_V2:
        return False
    invocation_hash = request.get("invocation_hash")
    if (
        not isinstance(invocation_hash, str)
        or len(invocation_hash) != 64
        or any(character not in "0123456789abcdef" for character in invocation_hash)
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    _, paths, _, stream_limit, result_limit = _validated_request_paths(request_path, request)
    with SupervisorFileLock(paths["lock_path"]):
        if paths["receipt_path"].exists():
            return True  # The transport still verifies the complete signed receipt.
        ready = read_transport_envelope(paths["ready_path"], key)
        started = read_transport_envelope(paths["started_path"], key)
        ready_keys = {"schema_ref", "invocation_hash", "supervisor_process_id", "supervisor_process_group"}
        started_keys = ready_keys | {"provider_process_id", "provider_process_group", "provider_operation_path"}
        if (
            set(ready) != ready_keys or set(started) != started_keys
            or ready.get("schema_ref") != "meta-research/provider-supervisor-ready/v2"
            or started.get("schema_ref") != "meta-research/provider-started/v2"
            or ready.get("invocation_hash") != invocation_hash
            or started.get("invocation_hash") != invocation_hash
            or started.get("provider_operation_path") != str(request_path)
            or any(ready.get(name) != started.get(name) for name in ("supervisor_process_id", "supervisor_process_group"))
        ):
            raise ProviderSupervisorError("provider_started_marker_invalid")
        for prefix in ("supervisor", "provider"):
            pid = started.get(f"{prefix}_process_id")
            group = started.get(f"{prefix}_process_group")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or group != pid:
                raise ProviderSupervisorError("provider_started_marker_invalid")
            # Numeric PID reuse is not proof of absence. Do not signal it or seal
            # this operation while either the recorded PID or group still exists.
            if (Path("/proc") / str(pid)).exists() or process_platform.process_group_running(pid):
                return False
        if not _operation_processes_absent(request_path):
            return False
        bridge_path = request_path.parent / ".last-message.supervisor.tmp"
        for file_path in (*paths.values(), bridge_path):
            if file_path.is_symlink() or (file_path.exists() and not file_path.is_file()):
                raise ProviderSupervisorError("provider_supervisor_spool_invalid")
        if not paths["stdout_path"].is_file() or paths["stdout_path"].stat().st_size > stream_limit:
            raise ProviderSupervisorError("provider_supervisor_spool_invalid")
        if any(path.is_file() and path.stat().st_size > result_limit for path in (paths["result_path"], bridge_path)):
            raise ProviderSupervisorError("provider_supervisor_spool_invalid")
        bridge_returncode = None
        if bridge_path.is_file() and bridge_path.stat().st_size <= 4096:
            try:
                bridge = json.loads(bridge_path.read_bytes())
                code = bridge.get("returncode") if isinstance(bridge, dict) else None
                if (
                    isinstance(bridge, dict)
                    and bridge.get("schema_ref") == "meta-research/harness-bridge-result/v1"
                    and isinstance(code, int) and not isinstance(code, bool)
                ):
                    bridge_returncode = code
            except (ValueError, UnicodeError):
                pass  # A partial bridge result supplies no outcome evidence.
        write_transport_envelope(
            request_path.parent / "supervisor-recovery.json",
            {
                "schema_ref": "meta-research/provider-supervisor-recovery/v1",
                "invocation_hash": invocation_hash,
                "reason": "orphaned_provider_without_exit_receipt",
                "boundary_returncode": 143,
                "recorded_bridge_returncode": bridge_returncode,
                "bridge_result_file_hash": _file_sha256(bridge_path) if bridge_path.is_file() else None,
                "supervisor_request_file_hash": _file_sha256(request_path),
                "supervisor_ready_file_hash": _file_sha256(paths["ready_path"]),
                "provider_started_file_hash": _file_sha256(paths["started_path"]),
            }, key,
        )
        write_exit_receipt(
            paths["receipt_path"], key=key, invocation_hash=invocation_hash,
            prompt_path=paths["prompt_path"], schema_path=paths["schema_path"],
            stdout_path=paths["stdout_path"], result_path=paths["result_path"],
            returncode=143, input_bytes=0, termination_reason="stopped",
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        platform = StreamingProviderProcessPlatform()
        try:
            supervise(Path(arguments[0]), process_platform=platform)
        except ProviderSupervisorError as error:
            if str(error) != "provider_outcome_unknown" or not _seal_absent_provider_interruption(
                Path(arguments[0]), process_platform=platform
            ):
                raise
    except (OSError, ProviderSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
