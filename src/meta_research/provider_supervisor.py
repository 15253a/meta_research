from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


TRANSPORT_KEY_BYTES = 32
SUPERVISOR_REQUEST_SCHEMA = "meta-research/codex-provider-supervisor-request/v1"
SUPERVISOR_EXIT_SCHEMA = "meta-research/codex-provider-supervisor-exit/v1"
SUPERVISOR_STOP_SCHEMA = "meta-research/codex-provider-supervisor-stop/v1"


class ProviderSupervisorError(RuntimeError):
    pass


@dataclass(frozen=True)
class TypedExecutionFence:
    """Cross-provider Run/Attempt/Session/Fence identity invariant."""

    run_ref: str
    attempt_ref: str
    generation: int
    root_session_ref: str
    fence_ref: str

    def validate(self) -> None:
        if (
            not self.run_ref
            or not self.attempt_ref
            or isinstance(self.generation, bool)
            or self.generation < 1
            or not self.root_session_ref
            or not self.fence_ref
        ):
            raise ProviderSupervisorError("typed_execution_fence_invalid")


def provider_operation_ref(
    run_ref: str,
    operation_kind: str,
    generation: int,
) -> str:
    """Stable effect identity shared by durable provider Run implementations."""

    if (
        not run_ref
        or not operation_kind
        or ":" in operation_kind
        or isinstance(generation, bool)
        or generation < 1
    ):
        raise ProviderSupervisorError("provider_operation_identity_invalid")
    value = f"{run_ref}:{operation_kind}:{generation}"
    if len(value) > 128:
        raise ProviderSupervisorError("provider_operation_identity_invalid")
    return value


def ensure_transport_key(workspace: Path) -> tuple[Path, bytes]:
    operation_root = workspace / "provider-operations"
    operation_root.mkdir(parents=True, exist_ok=True)
    key_path = operation_root / ".transport-seal.key"
    if not key_path.exists():
        _publish_exclusive(
            key_path,
            secrets.token_bytes(TRANSPORT_KEY_BYTES),
        )
    return key_path, _read_transport_key(key_path)


def transport_key_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def transport_canonical_json(value: object) -> str:
    """Canonical transport encoding shared by durable provider adapters."""

    return _canonical_json(value)


def sealed_transport_envelope(
    payload: dict[str, object], key: bytes
) -> dict[str, object]:
    return {"payload": payload, "seal": _seal(payload, key)}


def verify_transport_envelope(
    envelope: dict[str, object], key: bytes
) -> dict[str, object]:
    return _verified_envelope_payload(envelope, key)


def write_transport_envelope(
    path: Path, payload: dict[str, object], key: bytes
) -> None:
    _write_signed_envelope(path, payload, key)


def read_transport_envelope(path: Path, key: bytes) -> dict[str, object]:
    return _read_signed_envelope(path, key)


def transport_file_sha256(path: Path) -> str:
    return _file_sha256(path)


def provider_process_group_running(process_group: int) -> bool:
    return _process_group_running(process_group)


def terminate_provider_process_group(process_group: int) -> bool:
    return _terminate_process_group(process_group)


def read_transport_key_for_operation(
    operation_directory: Path,
) -> tuple[Path, bytes]:
    key_path = _operation_key_path(operation_directory)
    return key_path, _read_transport_key(key_path)


def write_supervisor_request(
    path: Path,
    payload: dict[str, object],
    key: bytes,
) -> None:
    if payload.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    _write_signed_envelope(path, payload, key)


def read_supervisor_request(path: Path, key: bytes) -> dict[str, object]:
    payload = _read_signed_envelope(path, key)
    if payload.get("schema_ref") != SUPERVISOR_REQUEST_SCHEMA:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    return payload


def write_supervisor_stop_request(
    path: Path, *, key: bytes, invocation_hash: str
) -> None:
    if not isinstance(invocation_hash, str) or len(invocation_hash) != 64:
        raise ProviderSupervisorError("provider_supervisor_stop_invalid")
    _write_signed_envelope(
        path,
        {
            "schema_ref": SUPERVISOR_STOP_SCHEMA,
            "invocation_hash": invocation_hash,
        },
        key,
    )


def supervisor_stop_requested(
    path: Path, *, key: bytes, invocation_hash: str
) -> bool:
    if not path.exists():
        return False
    payload = _read_signed_envelope(path, key)
    if payload != {
        "schema_ref": SUPERVISOR_STOP_SCHEMA,
        "invocation_hash": invocation_hash,
    }:
        raise ProviderSupervisorError("provider_supervisor_stop_invalid")
    return True


def write_exit_receipt(
    path: Path,
    *,
    key: bytes,
    invocation_hash: str,
    prompt_path: Path,
    schema_path: Path,
    stdout_path: Path,
    result_path: Path,
    returncode: int,
    input_bytes: int,
    termination_reason: str = "completed",
) -> None:
    prompt_bytes = prompt_path.stat().st_size
    payload: dict[str, object] = {
        "schema_ref": SUPERVISOR_EXIT_SCHEMA,
        "invocation_hash": invocation_hash,
        "returncode": returncode,
        "termination_reason": termination_reason,
        "prompt_file_hash": _file_sha256(prompt_path),
        "prompt_bytes": prompt_bytes,
        "input_bytes": input_bytes,
        "input_complete": input_bytes == prompt_bytes,
        "output_schema_file_hash": _file_sha256(schema_path),
        "stdout_file_hash": _file_sha256(stdout_path),
        "result_file_hash": (
            _file_sha256(result_path) if result_path.is_file() else None
        ),
    }
    _write_signed_envelope(path, payload, key)


def read_verified_exit_receipt(
    path: Path,
    *,
    key: bytes,
    invocation_hash: str,
    prompt_path: Path,
    schema_path: Path,
    stdout_path: Path,
    result_path: Path,
) -> tuple[dict[str, object], dict[str, object]]:
    envelope = _read_envelope(path)
    payload = _verified_envelope_payload(envelope, key)
    expected_keys = {
        "schema_ref",
        "invocation_hash",
        "returncode",
        "termination_reason",
        "prompt_file_hash",
        "prompt_bytes",
        "input_bytes",
        "input_complete",
        "output_schema_file_hash",
        "stdout_file_hash",
        "result_file_hash",
    }
    returncode = payload.get("returncode")
    termination_reason = payload.get("termination_reason")
    prompt_bytes = payload.get("prompt_bytes")
    input_bytes = payload.get("input_bytes")
    current_result_hash = (
        _file_sha256(result_path) if result_path.is_file() else None
    )
    if (
        set(payload) != expected_keys
        or payload.get("schema_ref") != SUPERVISOR_EXIT_SCHEMA
        or payload.get("invocation_hash") != invocation_hash
        or not isinstance(returncode, int)
        or isinstance(returncode, bool)
        or termination_reason
        not in {
            "completed",
            "timeout",
            "stopped",
            "output_limit",
            "descendant_process",
            "launch_failed",
        }
        or not isinstance(prompt_bytes, int)
        or isinstance(prompt_bytes, bool)
        or not isinstance(input_bytes, int)
        or isinstance(input_bytes, bool)
        or prompt_bytes != prompt_path.stat().st_size
        or (
            termination_reason == "completed"
            and returncode == 0
            and (
                input_bytes != prompt_bytes
                or payload.get("input_complete") is not True
            )
        )
        or payload.get("prompt_file_hash") != _file_sha256(prompt_path)
        or payload.get("output_schema_file_hash") != _file_sha256(schema_path)
        or payload.get("stdout_file_hash") != _file_sha256(stdout_path)
        or payload.get("result_file_hash") != current_result_hash
    ):
        raise ProviderSupervisorError("provider_supervisor_exit_invalid")
    return cast(dict[str, object], payload), envelope


def _operation_key_path(operation_directory: Path) -> Path:
    try:
        operation_root = operation_directory.parents[1]
    except IndexError as error:
        raise ProviderSupervisorError("provider_supervisor_path_invalid") from error
    return operation_root / ".transport-seal.key"


def _read_transport_key(path: Path) -> bytes:
    try:
        value = path.read_bytes()
    except OSError as error:
        raise ProviderSupervisorError("provider_transport_key_unavailable") from error
    if len(value) != TRANSPORT_KEY_BYTES:
        raise ProviderSupervisorError("provider_transport_key_invalid")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _seal(payload: dict[str, object], key: bytes) -> str:
    return hmac.new(
        key,
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _write_signed_envelope(
    path: Path,
    payload: dict[str, object],
    key: bytes,
) -> None:
    envelope = {"payload": payload, "seal": _seal(payload, key)}
    encoded = _canonical_json(envelope)
    if not _publish_exclusive(path, encoded.encode("utf-8")):
        try:
            persisted = path.read_text(encoding="utf-8")
        except OSError as error:
            raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
        if persisted != encoded:
            raise ProviderSupervisorError("provider_supervisor_spool_invalid")


def _read_signed_envelope(path: Path, key: bytes) -> dict[str, object]:
    return _verified_envelope_payload(_read_envelope(path), key)


def _read_envelope(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
    if not isinstance(value, dict):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    return cast(dict[str, object], value)


def _verified_envelope_payload(
    envelope: dict[str, object], key: bytes
) -> dict[str, object]:
    payload = envelope.get("payload")
    seal = envelope.get("seal")
    if (
        set(envelope) != {"payload", "seal"}
        or not isinstance(payload, dict)
        or not isinstance(seal, str)
        or not hmac.compare_digest(seal, _seal(payload, key))
    ):
        raise ProviderSupervisorError("provider_supervisor_seal_invalid")
    return cast(dict[str, object], payload)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise ProviderSupervisorError("provider_supervisor_spool_invalid") from error
    return digest.hexdigest()


def _publish_exclusive(path: Path, value: bytes) -> bool:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    try:
        with temporary.open("xb") as destination:
            os.fchmod(destination.fileno(), 0o600)
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


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validated_request_paths(
    request_path: Path, payload: dict[str, object]
) -> tuple[list[str], dict[str, Path], float, int, int]:
    directory = request_path.parent.resolve()
    values = {
        "prompt_path": directory / "prompt.txt",
        "schema_path": directory / "output-schema.json",
        "stdout_path": directory / "stdout.jsonl",
        "result_path": directory / "last-message.json",
        "lock_path": directory / "supervisor.lock",
        "ready_path": directory / "supervisor-ready.json",
        "started_path": directory / "provider-started.json",
        "receipt_path": directory / "supervisor-exit.json",
        "stop_path": directory / "supervisor-stop.json",
    }
    argv = payload.get("argv")
    timeout_seconds = payload.get("timeout_seconds")
    stream_max_bytes = payload.get("stream_max_bytes")
    result_max_bytes = payload.get("result_max_bytes")
    if (
        set(payload)
        != {
            "schema_ref",
            "invocation_hash",
            "argv",
            "timeout_seconds",
            "stream_max_bytes",
            "result_max_bytes",
            *values,
        }
        or not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) and value for value in argv)
        or not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not 0 < float(timeout_seconds) <= 24 * 60 * 60
        or not isinstance(stream_max_bytes, int)
        or isinstance(stream_max_bytes, bool)
        or not 0 < stream_max_bytes <= 16 * 1024 * 1024
        or not isinstance(result_max_bytes, int)
        or isinstance(result_max_bytes, bool)
        or not 0 < result_max_bytes <= 16 * 1024 * 1024
        or any(payload.get(name) != str(path) for name, path in values.items())
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    typed_argv = cast(list[str], argv)
    try:
        schema_arg = typed_argv[typed_argv.index("--output-schema") + 1]
        result_arg = typed_argv[typed_argv.index("--output-last-message") + 1]
    except (ValueError, IndexError) as error:
        raise ProviderSupervisorError("provider_supervisor_request_invalid") from error
    if (
        schema_arg != str(values["schema_path"])
        or result_arg != str(values["result_path"])
        or typed_argv[-1] != "-"
    ):
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    return (
        typed_argv,
        values,
        float(timeout_seconds),
        stream_max_bytes,
        result_max_bytes,
    )


def _terminate_provider(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
    assert process.returncode is not None
    return process.returncode


def _process_group_running(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    return True


def _terminate_process_group(process_group: int) -> bool:
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + 0.5
    while _process_group_running(process_group) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _process_group_running(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + 0.5
        while _process_group_running(process_group) and time.monotonic() < deadline:
            time.sleep(0.01)
    return not _process_group_running(process_group)


def _bounded_stdout_drain(
    stream,
    destination,
    maximum_bytes: int,
    exceeded: threading.Event,
    errors: list[BaseException],
) -> None:
    written = 0
    try:
        while chunk := stream.read(64 * 1024):
            remaining = maximum_bytes - written
            if remaining > 0:
                accepted = chunk[:remaining]
                destination.write(accepted)
                written += len(accepted)
            if len(chunk) > max(remaining, 0):
                exceeded.set()
    except BaseException as error:
        errors.append(error)
    finally:
        stream.close()


def _provider_argv_with_result_pipe(
    argv: list[str], result_write_fd: int
) -> list[str]:
    provider_argv = list(argv)
    try:
        result_index = provider_argv.index("--output-last-message") + 1
    except (ValueError, IndexError) as error:
        raise ProviderSupervisorError("provider_supervisor_request_invalid") from error
    provider_argv[result_index] = (
        f"/proc/{os.getpid()}/fd/{result_write_fd}"
    )
    return provider_argv


def _write_phase_marker(
    path: Path,
    *,
    schema_ref: str,
    invocation_hash: str,
    key: bytes,
) -> None:
    _write_signed_envelope(
        path,
        {
            "schema_ref": schema_ref,
            "invocation_hash": invocation_hash,
        },
        key,
    )


def supervise(request_path: Path) -> None:
    request_path = request_path.resolve()
    key = _read_transport_key(_operation_key_path(request_path.parent))
    payload = read_supervisor_request(request_path, key)
    invocation_hash = payload.get("invocation_hash")
    if not isinstance(invocation_hash, str) or len(invocation_hash) != 64:
        raise ProviderSupervisorError("provider_supervisor_request_invalid")
    argv, paths, timeout_seconds, stream_max_bytes, result_max_bytes = (
        _validated_request_paths(request_path, payload)
    )
    stop_requested = False

    def request_stop(_signal_number: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    with paths["lock_path"].open("a+b") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
        if paths["receipt_path"].exists():
            return
        if paths["started_path"].exists():
            raise ProviderSupervisorError("provider_outcome_unknown")
        _write_phase_marker(
            paths["ready_path"],
            schema_ref="meta-research/codex-provider-supervisor-ready/v1",
            invocation_hash=invocation_hash,
            key=key,
        )
        _supervise_locked(
            argv=argv,
            paths=paths,
            invocation_hash=invocation_hash,
            key=key,
            timeout_seconds=timeout_seconds,
            stream_max_bytes=stream_max_bytes,
            result_max_bytes=result_max_bytes,
            stop_requested=lambda: stop_requested
            or supervisor_stop_requested(
                paths["stop_path"], key=key, invocation_hash=invocation_hash
            ),
        )


def _supervise_locked(
    *,
    argv: list[str],
    paths: dict[str, Path],
    invocation_hash: str,
    key: bytes,
    timeout_seconds: float,
    stream_max_bytes: int,
    result_max_bytes: int,
    stop_requested: Callable[[], bool],
) -> None:
    prompt_path = paths["prompt_path"]
    schema_path = paths["schema_path"]
    stdout_path = paths["stdout_path"]
    result_path = paths["result_path"]
    receipt_path = paths["receipt_path"]
    termination_reason = "completed"
    returncode = 143
    stdout_exceeded = threading.Event()
    result_exceeded = threading.Event()
    stdout_errors: list[BaseException] = []
    result_errors: list[BaseException] = []
    result_temporary = result_path.with_name(".last-message.supervisor.tmp")
    if (
        stdout_path.exists()
        or result_path.exists()
        or result_temporary.exists()
    ):
        raise ProviderSupervisorError("provider_supervisor_spool_invalid")
    result_read_fd, result_write_fd = os.pipe()
    provider_argv = _provider_argv_with_result_pipe(argv, result_write_fd)
    with prompt_path.open("rb", buffering=0) as prompt_stream, stdout_path.open(
        "xb", buffering=0
    ) as stdout_stream, result_temporary.open("xb", buffering=0) as result_stream:
        if stop_requested():
            termination_reason = "stopped"
            os.close(result_read_fd)
            os.close(result_write_fd)
        else:
            _write_phase_marker(
                paths["started_path"],
                schema_ref="meta-research/codex-provider-started/v1",
                invocation_hash=invocation_hash,
                key=key,
            )
            try:
                process = subprocess.Popen(
                    provider_argv,
                    stdin=prompt_stream,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except OSError:
                os.close(result_read_fd)
                os.close(result_write_fd)
                termination_reason = "launch_failed"
                returncode = 127
            else:
                assert process.stdout is not None
                stdout_drainer = threading.Thread(
                    target=_bounded_stdout_drain,
                    args=(
                        process.stdout,
                        stdout_stream,
                        stream_max_bytes,
                        stdout_exceeded,
                        stdout_errors,
                    ),
                )
                result_drainer = threading.Thread(
                    target=_bounded_stdout_drain,
                    args=(
                        os.fdopen(result_read_fd, "rb", buffering=0),
                        result_stream,
                        result_max_bytes,
                        result_exceeded,
                        result_errors,
                    ),
                )
                stdout_drainer.start()
                result_drainer.start()
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None:
                    if stop_requested():
                        termination_reason = "stopped"
                        returncode = _terminate_provider(process)
                        break
                    if time.monotonic() >= deadline:
                        termination_reason = "timeout"
                        returncode = _terminate_provider(process)
                        break
                    if stdout_exceeded.is_set() or result_exceeded.is_set():
                        termination_reason = "output_limit"
                        returncode = _terminate_provider(process)
                        break
                    try:
                        process.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        continue
                else:
                    assert process.returncode is not None
                    returncode = process.returncode
                if termination_reason == "completed":
                    assert process.returncode is not None
                    returncode = process.returncode
                if _process_group_running(process.pid):
                    termination_reason = "descendant_process"
                    if not _terminate_process_group(process.pid):
                        raise ProviderSupervisorError(
                            "provider_descendant_cleanup_failed"
                        )
                os.close(result_write_fd)
                stdout_drainer.join(timeout=1.0)
                result_drainer.join(timeout=1.0)
                if stdout_drainer.is_alive() or result_drainer.is_alive():
                    _terminate_process_group(process.pid)
                    stdout_drainer.join(timeout=1.0)
                    result_drainer.join(timeout=1.0)
                if (
                    stdout_drainer.is_alive()
                    or result_drainer.is_alive()
                    or stdout_errors
                    or result_errors
                ):
                    raise ProviderSupervisorError(
                        "provider_stdout_capture_failed"
                    )
                if stdout_exceeded.is_set() or result_exceeded.is_set():
                    termination_reason = "output_limit"
        os.fsync(stdout_stream.fileno())
        os.fsync(result_stream.fileno())
        input_bytes = prompt_stream.tell()
    os.replace(result_temporary, result_path)
    _fsync_directory(result_path.parent)
    if (
        stdout_path.stat().st_size > stream_max_bytes
        or result_path.stat().st_size > result_max_bytes
    ):
        termination_reason = "output_limit"
    write_exit_receipt(
        receipt_path,
        key=key,
        invocation_hash=invocation_hash,
        prompt_path=prompt_path,
        schema_path=schema_path,
        stdout_path=stdout_path,
        result_path=result_path,
        returncode=returncode,
        input_bytes=input_bytes,
        termination_reason=termination_reason,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        return 64
    try:
        supervise(Path(arguments[0]))
    except (OSError, ProviderSupervisorError, subprocess.SubprocessError):
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
