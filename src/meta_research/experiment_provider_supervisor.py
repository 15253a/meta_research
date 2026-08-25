from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

# The supervisor is launched as an isolated, absolute script so it cannot import
# a shadow package from the provider workspace. Add only its packaged source
# root before importing the shared #117 durable transport/process primitives.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meta_research.provider_supervisor import (
    PROVIDER_OPERATION_ENV,
    ProviderProcessJob,
    ProviderProcessJobFactory,
    ProviderSupervisorError,
    ProviderProcessPlatform,
    SupervisorFileLock,
    WindowsProviderJob,
    ensure_transport_key as ensure_shared_transport_key,
    minimal_subprocess_environment,
    read_transport_envelope,
    sealed_transport_envelope,
    supervisor_stop_requested,
    transport_canonical_json,
    transport_file_sha256,
    verify_transport_envelope,
    write_transport_envelope,
)


REQUEST_SCHEMA = "meta-research/experiment-provider-supervisor-request/v2"
EXIT_SCHEMA = "meta-research/experiment-provider-supervisor-exit/v2"
MARKER_SCHEMA = "meta-research/experiment-provider-phase/v1"
OBSERVATION_SCHEMA = "meta-research/experiment-provider-observation/v1"
OBSERVATION_MAX_RECORD_BYTES = 32 * 1024
RESULT_PREFIX = b"META_RESEARCH_RESULT\t"


class ExperimentSupervisorError(RuntimeError):
    pass


def canonical_json(value: object) -> str:
    return transport_canonical_json(value)


def ensure_transport_key(workspace: Path) -> bytes:
    try:
        _path, key = ensure_shared_transport_key(workspace)
    except (OSError, ProviderSupervisorError) as error:
        raise ExperimentSupervisorError("transport_key_unavailable") from error
    return key


def write_signed(path: Path, payload: dict[str, object], key: bytes) -> None:
    if path.exists():
        try:
            existing = read_transport_envelope(path, key)
        except ProviderSupervisorError as error:
            raise ExperimentSupervisorError("spool_invalid") from error
        if existing != payload:
            raise ExperimentSupervisorError("identity_conflict")
        return
    try:
        write_transport_envelope(path, payload, key)
    except ProviderSupervisorError as error:
        raise ExperimentSupervisorError("spool_invalid") from error


def read_signed(path: Path, key: bytes) -> dict[str, object]:
    try:
        return read_transport_envelope(path, key)
    except ProviderSupervisorError as error:
        raise ExperimentSupervisorError("spool_invalid") from error


def file_sha256(path: Path) -> str:
    try:
        return transport_file_sha256(path)
    except ProviderSupervisorError as error:
        raise ExperimentSupervisorError("spool_invalid") from error


def decode_observation_line(encoded: bytes, key: bytes) -> dict[str, object]:
    try:
        envelope = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExperimentSupervisorError("observation_spool_invalid") from error
    if not isinstance(envelope, dict):
        raise ExperimentSupervisorError("observation_spool_invalid")
    try:
        return verify_transport_envelope(envelope, key)
    except ProviderSupervisorError as error:
        raise ExperimentSupervisorError("observation_spool_invalid") from error


class _ObservationLedger:
    def __init__(
        self,
        stream,
        *,
        key: bytes,
        invocation_hash: str,
        maximum_count: int,
    ) -> None:
        self._stream = stream
        self._key = key
        self._invocation_hash = invocation_hash
        self._maximum_count = maximum_count
        self._lock = threading.Lock()
        self._count = 0
        self.exceeded = threading.Event()

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def append(
        self,
        kind: str,
        payload: dict[str, object],
        observed_at: float,
    ) -> None:
        with self._lock:
            if self._count >= self._maximum_count:
                self.exceeded.set()
                return
            sequence = self._count + 1
            observation: dict[str, object] = {
                "schema_ref": OBSERVATION_SCHEMA,
                "invocation_hash": self._invocation_hash,
                "sequence": sequence,
                "kind": kind,
                "payload": payload,
                "observed_at": observed_at,
            }
            envelope = sealed_transport_envelope(observation, self._key)
            encoded = transport_canonical_json(envelope).encode("utf-8") + b"\n"
            if len(encoded) > OBSERVATION_MAX_RECORD_BYTES:
                self.exceeded.set()
                return
            self._stream.write(encoded)
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._count = sequence


def supervise(
    request_path: Path,
    *,
    process_platform: ProviderProcessPlatform | None = None,
    provider_job_factory: ProviderProcessJobFactory | None = None,
) -> None:
    request_path = request_path.resolve()
    platform = process_platform or ProviderProcessPlatform()
    directory = request_path.parent
    key = ensure_transport_key(directory.parents[1])
    payload = read_signed(request_path, key)
    values = _validate_request(directory, payload)
    lock_path = directory / "supervisor.lock"
    with SupervisorFileLock(lock_path):
        receipt_path = directory / "supervisor-exit.json"
        if receipt_path.exists():
            return
        if (directory / "provider-started.json").exists():
            raise ExperimentSupervisorError("provider_outcome_unknown")
        _phase_marker(
            directory / "supervisor-ready.json",
            "ready",
            values,
            key,
            process_platform=platform,
        )
        _supervise_locked(
            directory,
            values,
            key,
            process_platform=platform,
            provider_job_factory=provider_job_factory,
        )


def _validate_request(
    directory: Path, payload: dict[str, object]
) -> dict[str, object]:
    expected_paths = {
        "stdin_path": directory / "stdin.json",
        "stdout_path": directory / "stdout.bin",
        "observation_path": directory / "observations.jsonl",
        "started_path": directory / "provider-started.json",
        "ready_path": directory / "supervisor-ready.json",
        "receipt_path": directory / "supervisor-exit.json",
    }
    expected_keys = {
        "schema_ref",
        "invocation_hash",
        "argv",
        "wall_timeout_seconds",
        "stdout_max_bytes",
        "stdout_max_records",
        "result_max_bytes",
        "observation_max_count",
        "telemetry_cadence_seconds",
        *expected_paths,
    }
    argv = payload.get("argv")
    timeout = payload.get("wall_timeout_seconds")
    stdout_max = payload.get("stdout_max_bytes")
    stdout_max_records = payload.get("stdout_max_records")
    result_max = payload.get("result_max_bytes")
    observation_max = payload.get("observation_max_count")
    telemetry_cadence = payload.get("telemetry_cadence_seconds")
    invocation_hash = payload.get("invocation_hash")
    if (
        set(payload) != expected_keys
        or payload.get("schema_ref") != REQUEST_SCHEMA
        or not isinstance(invocation_hash, str)
        or len(invocation_hash) != 64
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) and value for value in argv)
        or not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < float(timeout) <= 24 * 60 * 60
        or not isinstance(stdout_max, int)
        or isinstance(stdout_max, bool)
        or not 0 < stdout_max <= 16 * 1024 * 1024
        or not isinstance(stdout_max_records, int)
        or isinstance(stdout_max_records, bool)
        or not 1 <= stdout_max_records <= 65536
        or not isinstance(result_max, int)
        or isinstance(result_max, bool)
        or not 0 < result_max <= 16 * 1024 * 1024
        or not isinstance(observation_max, int)
        or isinstance(observation_max, bool)
        or not 1 <= observation_max <= 65536
        or not isinstance(telemetry_cadence, (int, float))
        or isinstance(telemetry_cadence, bool)
        or not 0.05 <= float(telemetry_cadence) <= 60.0
        or any(payload.get(name) != str(path) for name, path in expected_paths.items())
    ):
        raise ExperimentSupervisorError("request_invalid")
    return {
        "invocation_hash": invocation_hash,
        "argv": argv,
        "wall_timeout_seconds": float(timeout),
        "stdout_max_bytes": stdout_max,
        "stdout_max_records": stdout_max_records,
        "result_max_bytes": result_max,
        "observation_max_count": observation_max,
        "telemetry_cadence_seconds": float(telemetry_cadence),
        **expected_paths,
    }


def _phase_marker(
    path: Path,
    phase: str,
    values: dict[str, object],
    key: bytes,
    *,
    provider_process: subprocess.Popen[bytes] | None = None,
    operation_path: Path | None = None,
    process_platform: ProviderProcessPlatform | None = None,
) -> None:
    platform = process_platform or ProviderProcessPlatform()
    payload: dict[str, object] = {
        "schema_ref": (
            MARKER_SCHEMA
            if provider_process is None
            else "meta-research/experiment-provider-phase/v2"
        ),
        "phase": phase,
        "invocation_hash": values["invocation_hash"],
        "supervisor_process_id": os.getpid(),
        "supervisor_process_group": platform.current_process_group(),
    }
    if provider_process is not None:
        if operation_path is None:
            raise ExperimentSupervisorError("provider_process_identity_invalid")
        payload.update(
            {
                "provider_process_id": provider_process.pid,
                "provider_process_group": platform.process_group_for_pid(
                    provider_process.pid
                ),
                "provider_operation_path": str(operation_path.resolve()),
            }
        )
    write_signed(
        path,
        payload,
        key,
    )


def _supervise_locked(
    directory: Path,
    values: dict[str, object],
    key: bytes,
    *,
    process_platform: ProviderProcessPlatform,
    provider_job_factory: ProviderProcessJobFactory | None,
) -> None:
    stdout_path = directory / "stdout.bin"
    observation_path = directory / "observations.jsonl"
    if stdout_path.exists() or observation_path.exists():
        raise ExperimentSupervisorError("spool_invalid")
    started_at = time.time()
    termination_reason = "completed"
    returncode = 127
    exceeded = threading.Event()
    drain_errors: list[BaseException] = []
    stop_requested = False

    def request_stop(_signal_number: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    def should_stop() -> bool:
        return stop_requested or supervisor_stop_requested(
            directory / "supervisor-stop.json",
            key=key,
            invocation_hash=str(values["invocation_hash"]),
        )

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    maximum_bytes = (
        int(values["stdout_max_bytes"])
        + int(values["result_max_bytes"])
        + 1024
    )
    stdin_path = directory / "stdin.json"
    with ExitStack() as stack:
        provider_job: ProviderProcessJob | None = None
        if process_platform.platform_name == "nt":
            provider_job = (
                provider_job_factory or WindowsProviderJob
            )()
            stack.callback(provider_job.close)
        elif process_platform.platform_name != "posix":
            raise ExperimentSupervisorError("platform_unsupported")
        stdin_stream = stack.enter_context(stdin_path.open("rb", buffering=0))
        stdout_stream = stack.enter_context(stdout_path.open("xb", buffering=0))
        observation_stream = stack.enter_context(
            observation_path.open("xb", buffering=0)
        )
        _make_private(stdout_path, stdout_stream.fileno())
        _make_private(observation_path, observation_stream.fileno())
        ledger = _ObservationLedger(
            observation_stream,
            key=key,
            invocation_hash=str(values["invocation_hash"]),
            maximum_count=int(values["observation_max_count"]),
        )
        if should_stop():
            process = None
            termination_reason = "stopped"
        else:
            try:
                spawn_options: dict[str, object] = {
                    "stdin": stdin_stream,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "env": minimal_subprocess_environment(
                        platform_name=process_platform.platform_name,
                        extra={
                            PROVIDER_OPERATION_ENV: str(
                                (
                                    directory / "supervisor-request.json"
                                ).resolve()
                            )
                        },
                    ),
                    **process_platform.provider_spawn_options(),
                }
                process = (
                    provider_job.spawn(
                        list(values["argv"]),
                        **spawn_options,
                    )
                    if provider_job is not None
                    else subprocess.Popen(
                        list(values["argv"]),
                        **spawn_options,
                    )
                )
            except OSError:
                process = None
                termination_reason = "launch_failed"
        if process is not None:
            _phase_marker(
                directory / "provider-started.json",
                "started",
                values,
                key,
                provider_process=process,
                operation_path=directory / "supervisor-request.json",
                process_platform=process_platform,
            )
            assert process.stdout is not None
            cadence = float(values["telemetry_cadence_seconds"])
            initial_observed_at = time.time()
            ledger.append(
                "telemetry",
                _host_telemetry(
                    process.pid,
                    started_at,
                    initial_observed_at,
                    cadence,
                ),
                initial_observed_at,
            )
            drainer = threading.Thread(
                target=_bounded_drain,
                args=(
                    process.stdout,
                    stdout_stream,
                    maximum_bytes,
                    int(values["stdout_max_bytes"]),
                    int(values["stdout_max_records"]),
                    int(values["result_max_bytes"]),
                    exceeded,
                    drain_errors,
                    ledger,
                ),
            )
            drainer.start()
            deadline = time.monotonic() + float(values["wall_timeout_seconds"])
            next_telemetry = time.monotonic() + cadence
            while process.poll() is None or (
                provider_job is not None
                and provider_job.active_process_count() > 0
            ):
                now_monotonic = time.monotonic()
                if should_stop():
                    termination_reason = "stopped"
                    _terminate_process(
                        process,
                        process_platform=process_platform,
                        provider_job=provider_job,
                    )
                    break
                if exceeded.is_set() or ledger.exceeded.is_set():
                    termination_reason = "output_limit"
                    _terminate_process(
                        process,
                        process_platform=process_platform,
                        provider_job=provider_job,
                    )
                    break
                if now_monotonic >= deadline:
                    termination_reason = "timeout"
                    _terminate_process(
                        process,
                        process_platform=process_platform,
                        provider_job=provider_job,
                    )
                    break
                if process.poll() is None and now_monotonic >= next_telemetry:
                    observed_at = time.time()
                    ledger.append(
                        "telemetry",
                        _host_telemetry(
                            process.pid, started_at, observed_at, cadence
                        ),
                        observed_at,
                    )
                    next_telemetry = now_monotonic + cadence
                if process.poll() is None:
                    try:
                        process.wait(timeout=0.02)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    time.sleep(0.02)
            if process.poll() is None:
                _terminate_process(
                    process,
                    process_platform=process_platform,
                    provider_job=provider_job,
                )
            assert process.returncode is not None
            returncode = process.returncode
            if provider_job is None and process_platform.process_group_running(
                process.pid
            ):
                termination_reason = "descendant_process"
                process_platform.terminate_process_group(process.pid)
            drainer.join(timeout=1.0)
            if drainer.is_alive():
                if provider_job is not None:
                    if not provider_job.terminate():
                        raise ExperimentSupervisorError(
                            "provider_job_termination_failed"
                        )
                else:
                    process_platform.terminate_process_group(process.pid)
                drainer.join(timeout=1.0)
            if drainer.is_alive() or drain_errors:
                raise ExperimentSupervisorError("stdout_capture_failed")
            if exceeded.is_set() or ledger.exceeded.is_set():
                termination_reason = "output_limit"
        if (
            provider_job is not None
            and provider_job.active_process_count() != 0
        ):
            raise ExperimentSupervisorError("provider_job_not_empty")
        stdout_stream.flush()
        os.fsync(stdout_stream.fileno())
        observation_stream.flush()
        os.fsync(observation_stream.fileno())
        observation_count = ledger.count
    completed_at = time.time()
    receipt_payload: dict[str, object] = {
        "schema_ref": EXIT_SCHEMA,
        "invocation_hash": values["invocation_hash"],
        "termination_reason": termination_reason,
        "returncode": returncode,
        "stdin_hash": file_sha256(stdin_path),
        "stdout_hash": file_sha256(stdout_path),
        "stdout_bytes": stdout_path.stat().st_size,
        "observation_hash": file_sha256(observation_path),
        "observation_bytes": observation_path.stat().st_size,
        "observation_count": observation_count,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    write_signed(directory / "supervisor-exit.json", receipt_payload, key)


def _bounded_drain(
    source,
    destination,
    maximum_bytes: int,
    stdout_max_bytes: int,
    stdout_max_records: int,
    result_max_bytes: int,
    exceeded: threading.Event,
    errors: list[BaseException],
    ledger: _ObservationLedger,
) -> None:
    written = 0
    pending = b""
    raw_bytes = 0
    raw_records = 0

    def process_record(record: bytes, *, terminated: bool) -> None:
        nonlocal raw_bytes, raw_records
        if record.startswith(RESULT_PREFIX):
            if len(record) - len(RESULT_PREFIX) > result_max_bytes:
                exceeded.set()
            return
        record_bytes = len(record) + (1 if terminated else 0)
        raw_bytes += record_bytes
        raw_records += 1
        try:
            decoded = record.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            decoded = None
        if (
            raw_bytes > stdout_max_bytes
            or raw_records > stdout_max_records
            or (decoded is not None and len(decoded) > 4000)
        ):
            exceeded.set()
            return
        _publish_stdout_record(record, ledger)

    def partial_record_exceeded() -> bool:
        if RESULT_PREFIX.startswith(pending):
            return False
        if pending.startswith(RESULT_PREFIX):
            return len(pending) - len(RESULT_PREFIX) > result_max_bytes
        return raw_bytes + len(pending) > stdout_max_bytes

    try:
        read_available = getattr(source, "read1", source.read)
        while chunk := read_available(64 * 1024):
            remaining = maximum_bytes - written
            accepted = chunk[: max(remaining, 0)]
            if accepted:
                destination.write(accepted)
                destination.flush()
                written += len(accepted)
                pending += accepted
                while b"\n" in pending:
                    record, pending = pending.split(b"\n", 1)
                    process_record(record, terminated=True)
                if partial_record_exceeded():
                    exceeded.set()
            if len(chunk) > max(remaining, 0):
                exceeded.set()
        if pending:
            process_record(pending, terminated=False)
    except BaseException as error:
        errors.append(error)
    finally:
        source.close()


def _publish_stdout_record(record: bytes, ledger: _ObservationLedger) -> None:
    if (
        record.startswith(RESULT_PREFIX)
        or b"\r" in record
        or b"\x00" in record
    ):
        return
    try:
        line = record.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return
    if len(line) > 4000:
        return
    ledger.append(
        "stdout",
        {"line": line, "stream": "stdout"},
        time.time(),
    )


def _host_telemetry(
    process_id: int,
    started_at: float,
    observed_at: float,
    cadence: float,
) -> dict[str, object]:
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    memory_total_kib, memory_available_kib = _host_memory_kib()
    return {
        "collector": "builtin-host-telemetry-v1",
        "device": f"process:{process_id}",
        "scope": "CPU/load and memory are host-wide; correlated, not exclusive",
        "correlation": "same local provider process observation window",
        "cadence": cadence,
        "staleAfter": max(1.0, cadence * 4),
        "sampleTime": observed_at,
        "measurements": {
            "elapsed": {
                "value": max(0.0, observed_at - started_at),
                "unit": "seconds",
                "denominator": "same execution-attempt observation window",
            },
            "load1m": {
                "value": load[0],
                "unit": "runnable-entities",
                "denominator": "host-wide one-minute load average",
            },
            "memoryTotal": {
                "value": memory_total_kib,
                "unit": "KiB",
                "denominator": "host physical memory",
            },
            "memoryAvailable": {
                "value": memory_available_kib,
                "unit": "KiB",
                "denominator": "host physical memory",
            },
        },
        "freshness": "live",
    }


def _host_memory_kib() -> tuple[int | None, int | None]:
    if os.name == "nt":
        return _windows_host_memory_kib()
    memory_total_kib: int | None = None
    memory_available_kib: int | None = None
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory_total_kib = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                memory_available_kib = int(line.split()[1])
    except (OSError, ValueError):
        pass
    return memory_total_kib, memory_available_kib


def _windows_host_memory_kib() -> tuple[int | None, int | None]:
    import ctypes
    from ctypes import wintypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MemoryStatus)]
    kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None, None
    return status.ullTotalPhys // 1024, status.ullAvailPhys // 1024


def _terminate_process(
    process: subprocess.Popen[bytes],
    *,
    process_platform: ProviderProcessPlatform,
    provider_job: ProviderProcessJob | None,
) -> None:
    if provider_job is not None:
        if not provider_job.terminate():
            raise ExperimentSupervisorError("provider_job_termination_failed")
    else:
        process_platform.terminate_process_group(process.pid)
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            raise ExperimentSupervisorError(
                "provider_process_termination_unconfirmed"
            ) from error


def _make_private(path: Path, descriptor: int) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, 0o600)
    else:
        path.chmod(0o600)


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        supervise(Path(arguments[0]))
    except (OSError, ExperimentSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
