"""Launch the durable supervisor with an unbuffered provider stdout pipe.

Transport configuration stays outside the frozen provider adapter sources so
already-admitted runs can retain their exact input and result contracts.
"""

from pathlib import Path
import subprocess
import sys

from meta_research.provider_supervisor import (
    ProviderProcessPlatform,
    ProviderSupervisorError,
    SupervisorFileLock,
    SUPERVISOR_REQUEST_SCHEMA_V2,
    read_supervisor_request,
    read_transport_envelope,
    read_transport_key_for_operation,
    supervisor_host_identity,
    transport_file_sha256,
    verify_process_tree_closed,
    write_transport_envelope,
    supervise,
)


class StreamingProviderProcessPlatform(ProviderProcessPlatform):
    def provider_spawn_options(self) -> dict[str, object]:
        # BufferedReader.read(64 KiB) waits for a full block or EOF. FileIO.read
        # returns the bytes currently available while keeping the same bounded
        # drain, signed receipts, process isolation, and cancellation behavior.
        return {**super().provider_spawn_options(), "bufsize": 0}


def reconcile_rebooted_supervisor(request_path: Path) -> dict[str, object] | None:
    """Record local closure after reboot without inventing a native outcome."""
    if sys.platform != "linux":
        return None
    request_path = request_path.resolve()
    operation = request_path.parent
    _, key = read_transport_key_for_operation(operation)
    request = read_supervisor_request(request_path, key)
    if request.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA_V2:
        return None
    lock = SupervisorFileLock(operation / "supervisor.lock")
    if not lock.acquire(blocking=False):
        return None
    try:
        ready_path = operation / "supervisor-ready.json"
        if not ready_path.is_file():
            return None
        ready = read_transport_envelope(ready_path, key)
        current = supervisor_host_identity()
        previous_boot = ready.get("boot_identity_hash")
        if (ready.get("schema_ref") != "meta-research/provider-supervisor-ready/v2"
                or ready.get("invocation_hash") != request.get("invocation_hash")
                or ready.get("host_identity_hash") != current["host_identity_hash"]
                or not isinstance(previous_boot, str) or len(previous_boot) != 64
                or any(character not in "0123456789abcdef" for character in previous_boot)
                or previous_boot == current["boot_identity_hash"]):
            return None
        tree = {"schema_ref": "meta-research/provider-process-tree/v1", "state": "closed",
                "mechanism": "host-reboot", "boot_identity_hash": previous_boot,
                "host_identity_hash": current["host_identity_hash"]}
        verify_process_tree_closed(tree)
        recovery_path = operation / "supervisor-recovery.json"
        binding = {"schema_ref": "meta-research/provider-supervisor-recovery/v2",
                   "invocation_hash": request["invocation_hash"],
                   "supervisor_request_file_hash": transport_file_sha256(request_path),
                   "supervisor_ready_file_hash": transport_file_sha256(ready_path),
                   "process_tree": tree, "outcome": "unknown"}
        if recovery_path.exists():
            recovered = read_transport_envelope(recovery_path, key)
            observed_boot = recovered.get("observed_boot_identity_hash")
            if (set(recovered) != {*binding, "observed_boot_identity_hash"}
                    or not isinstance(observed_boot, str) or len(observed_boot) != 64
                    or any(character not in "0123456789abcdef" for character in observed_boot)
                    or observed_boot == previous_boot
                    or {name: value for name, value in recovered.items() if name != "observed_boot_identity_hash"} != binding):
                raise ProviderSupervisorError("provider_reboot_evidence_invalid")
            return recovered
        recovered = {**binding, "observed_boot_identity_hash": current["boot_identity_hash"]}
        write_transport_envelope(recovery_path, recovered, key)
        return recovered
    finally:
        lock.release()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        platform = StreamingProviderProcessPlatform()
        try:
            supervise(Path(arguments[0]), process_platform=platform)
        except ProviderSupervisorError as error:
            if str(error) == "provider_outcome_unknown":
                reconcile_rebooted_supervisor(Path(arguments[0]))
            raise
    except (OSError, ProviderSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
