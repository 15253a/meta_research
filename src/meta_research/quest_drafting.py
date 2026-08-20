from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Literal, Protocol, cast

QUESTION_FIELD_MAX_LENGTHS = {
    "title": 500,
    "unknown_statement": 8000,
    "answer_shape": 8000,
    "applicability_scope": 8000,
    "background_context": 12000,
    "requirements_constraints": 12000,
}
INTENT_MESSAGE_MAX_LENGTH = 12000
INTENT_REPLY_MAX_LENGTH = 12000
PROVIDER_RESULT_MAX_BYTES = 1024 * 1024
PROVIDER_STREAM_MAX_BYTES = 256 * 1024
QUESTION_FIELDS = tuple(QUESTION_FIELD_MAX_LENGTHS)
_PSEUDO_VALUES = {"unknown", "not_applicable", "not applicable", "n/a", "na"}


class DraftingUnavailable(RuntimeError):
    """A real drafting provider could not produce an auditable result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProposalDraftRequest:
    initialization_id: str
    draft_revision: int
    draft_hash: str
    draft: dict[str, object]
    job_ref: str | None = None


@dataclass(frozen=True)
class ProposalDraftResult:
    content: dict[str, str]
    adapter_kind: str


@dataclass(frozen=True)
class IntentTurnRequest:
    initialization_id: str
    draft_revision: int
    draft_hash: str
    draft: dict[str, object]
    message: str
    native_session_ref: str | None
    job_ref: str | None = None


@dataclass(frozen=True)
class IntentTurnResult:
    reply: str
    native_session_ref: str
    adapter_kind: str


class ProposalDrafter(Protocol):
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult: ...


class IntentDraftingProvider(Protocol):
    def reply(self, request: IntentTurnRequest) -> IntentTurnResult: ...


@dataclass(frozen=True)
class HostComputeDevice:
    uuid: str
    name: str
    memory_total_mib: int

    def as_dict(self) -> dict[str, object]:
        return {
            "uuid": self.uuid,
            "name": self.name,
            "memory_total_mib": self.memory_total_mib,
        }


@dataclass(frozen=True)
class HostComputeSnapshot:
    status: Literal["ready", "unavailable"]
    observed_at: float
    devices: tuple[HostComputeDevice, ...]
    adapter_kind: str = "nvidia_smi"
    reason_code: str | None = None

    def as_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "status": self.status,
            "observed_at": self.observed_at,
            "devices": [device.as_dict() for device in self.devices],
            "adapter_kind": self.adapter_kind,
        }
        if self.reason_code is not None:
            value["reason"] = {"code": self.reason_code}
        return value


class HostComputeProbe(Protocol):
    def observe(self) -> HostComputeSnapshot: ...


ProcessRunner = Callable[
    [list[str], str, float], subprocess.CompletedProcess[str]
]
CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class _ProcessStopped(RuntimeError):
    pass


class _BoundedPipeCapture:
    """Drain a provider pipe without retaining more than its public byte budget."""

    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._value = bytearray()
        self.too_large = False

    def drain(self, stream: BinaryIO, overflow: Callable[[], None]) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                remaining = self._maximum_bytes - len(self._value)
                if remaining > 0:
                    self._value.extend(chunk[:remaining])
                if len(chunk) > remaining and not self.too_large:
                    self.too_large = True
                    overflow()
        finally:
            with contextlib.suppress(OSError):
                stream.close()

    def text(self, *, reject_oversized: bool = True) -> str:
        if self.too_large and reject_oversized:
            raise DraftingUnavailable("codex_output_too_large")
        return bytes(self._value).decode("utf-8", errors="replace")


class _PipeInputWriter:
    """Write provider input off-thread so the process deadline starts immediately."""

    def __init__(self, value: bytes) -> None:
        self._value = value
        self.error: OSError | None = None

    def write(self, stream: BinaryIO) -> None:
        try:
            stream.write(self._value)
        except BrokenPipeError:
            pass
        except OSError as error:
            self.error = error
        finally:
            with contextlib.suppress(OSError):
                stream.close()


class _CancellableProcessRunner:
    def __init__(self, termination_grace_seconds: float = 0.25) -> None:
        self._termination_grace_seconds = termination_grace_seconds
        self._lock = threading.Lock()
        self._processes: dict[subprocess.Popen[bytes], int | None] = {}
        self._jobs: dict[str, tuple[subprocess.Popen[bytes], int | None]] = {}
        self._cancelled_jobs: set[str] = set()
        self._cancelled: set[subprocess.Popen[bytes]] = set()
        self._stopping = False

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._run(None, argv, input_text, timeout)

    def run_job(
        self, job_ref: str, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        return self._run(job_ref, argv, input_text, timeout)

    def _run(
        self,
        job_ref: str | None,
        argv: list[str],
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        deadline = time.monotonic() + timeout
        with self._lock:
            if self._stopping or (
                job_ref is not None and job_ref in self._cancelled_jobs
            ):
                raise _ProcessStopped
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            process_group = os.getpgid(process.pid) if os.name == "posix" else None
            self._processes[process] = process_group
            if job_ref is not None:
                self._jobs[job_ref] = (process, process_group)
        capture = _BoundedPipeCapture(PROVIDER_STREAM_MAX_BYTES)
        assert process.stdout is not None
        drainer = threading.Thread(
            target=capture.drain,
            args=(
                process.stdout,
                lambda: self._signal_process_tree(
                    process, process_group, signal.SIGKILL
                ),
            ),
            daemon=True,
        )
        drainer.start()
        writer = _PipeInputWriter(input_text.encode("utf-8"))
        assert process.stdin is not None
        input_thread = threading.Thread(
            target=writer.write,
            args=(process.stdin,),
            daemon=True,
        )
        input_thread.start()
        try:
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                self._signal_process_tree(process, process_group, signal.SIGKILL)
                process.wait()
                try:
                    self._join_pipe_thread(
                        input_thread,
                        process,
                        process_group,
                        "provider stdin pipe did not close",
                    )
                finally:
                    self._join_drainer(drainer, process, process_group)
                raise subprocess.TimeoutExpired(
                    argv,
                    timeout,
                    output=capture.text(reject_oversized=False),
                    stderr="",
                ) from error
            try:
                self._join_pipe_thread(
                    input_thread,
                    process,
                    process_group,
                    "provider stdin pipe did not close",
                )
            finally:
                self._join_drainer(drainer, process, process_group)
            if writer.error is not None:
                raise writer.error
            stdout = capture.text()
            with self._lock:
                stopped = (
                    self._stopping
                    or process in self._cancelled
                    or job_ref is not None
                    and job_ref in self._cancelled_jobs
                )
            if stopped:
                raise _ProcessStopped
            return subprocess.CompletedProcess(
                argv, process.returncode, stdout=stdout, stderr=""
            )
        finally:
            with self._lock:
                self._processes.pop(process, None)
                if job_ref is not None:
                    active = self._jobs.get(job_ref)
                    if active is not None and active[0] is process:
                        self._jobs.pop(job_ref, None)
                self._cancelled.discard(process)

    def _join_drainer(
        self,
        drainer: threading.Thread,
        process: subprocess.Popen[bytes],
        process_group: int | None,
    ) -> None:
        self._join_pipe_thread(
            drainer,
            process,
            process_group,
            "provider stdout pipe did not close",
        )

    def _join_pipe_thread(
        self,
        worker: threading.Thread,
        process: subprocess.Popen[bytes],
        process_group: int | None,
        error_message: str,
    ) -> None:
        worker.join(timeout=self._termination_grace_seconds)
        if worker.is_alive():
            self._terminate_processes(((process, process_group),))
            worker.join(timeout=self._termination_grace_seconds)
        if worker.is_alive():
            raise OSError(error_message)

    def cancel_job(self, job_ref: str) -> None:
        with self._lock:
            self._cancelled_jobs.add(job_ref)
            active = self._jobs.get(job_ref)
            if active is not None:
                self._cancelled.add(active[0])
        if active is not None:
            self._terminate_processes((active,))

    def finish_job(self, job_ref: str) -> None:
        with self._lock:
            self._cancelled_jobs.discard(job_ref)

    def cancel_active(self) -> None:
        with self._lock:
            processes = tuple(self._processes.items())
            self._cancelled.update(process for process, _group in processes)
        self._terminate_processes(processes)

    def request_stop(self) -> None:
        with self._lock:
            self._stopping = True
            processes = tuple(self._processes.items())
            self._cancelled.update(process for process, _group in processes)
        self._terminate_processes(processes)

    def _terminate_processes(
        self, processes: tuple[tuple[subprocess.Popen[bytes], int | None], ...]
    ) -> None:
        for process, process_group in processes:
            self._signal_process_tree(process, process_group, signal.SIGTERM)
        self._wait_until_stopped(processes)
        for process, process_group in processes:
            if self._process_tree_running(process, process_group):
                self._signal_process_tree(process, process_group, signal.SIGKILL)
        self._wait_until_stopped(processes)

    def _wait_until_stopped(
        self, processes: tuple[tuple[subprocess.Popen[bytes], int | None], ...]
    ) -> None:
        deadline = time.monotonic() + self._termination_grace_seconds
        while any(
            self._process_tree_running(process, process_group)
            for process, process_group in processes
        ):
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)

    @staticmethod
    def _signal_process_tree(
        process: subprocess.Popen[bytes], process_group: int | None, signal_number: int
    ) -> None:
        with contextlib.suppress(ProcessLookupError):
            if process_group is not None:
                os.killpg(process_group, signal_number)
            elif signal_number == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()

    @staticmethod
    def _process_tree_running(
        process: subprocess.Popen[bytes], process_group: int | None
    ) -> bool:
        if process_group is None:
            return process.poll() is None
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return False
        return True


def _run_command(
    argv: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class CodexDraftingAdapter(ProposalDrafter, IntentDraftingProvider):
    """Narrow production adapter for schema-constrained Codex CLI drafting.

    The adapter owns provider invocation only. It receives immutable basis values and
    cannot mutate a Quest draft, confirm a bundle, or write an Owner receipt.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        executable: str = "codex",
        timeout_seconds: float = 180.0,
        process_runner: ProcessRunner | None = None,
    ) -> None:
        self._workspace = workspace
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._process_runner = process_runner or _CancellableProcessRunner()

    def request_stop(self) -> None:
        request_stop = getattr(self._process_runner, "request_stop", None)
        if callable(request_stop):
            request_stop()

    def cancel_active(self) -> None:
        cancel_active = getattr(self._process_runner, "cancel_active", None)
        if callable(cancel_active):
            cancel_active()

    def cancel_job(self, job_ref: str) -> bool:
        cancel_job = getattr(self._process_runner, "cancel_job", None)
        if not callable(cancel_job):
            return False
        cancel_job(job_ref)
        return True

    def finish_job(self, job_ref: str) -> None:
        finish_job = getattr(self._process_runner, "finish_job", None)
        if callable(finish_job):
            finish_job(job_ref)

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        prompt = (
            "你是 meta-research 的 Proposal Drafter。只基于给定的 Quest 草稿，"
            "生成一个可编辑的 QuestionProposal。不得声称已创建 Quest、Question、"
            "Cycle、receipt 或已执行检索。六个字段必须有具体语义，禁止用 unknown、"
            "N/A、not_applicable 等占位值。\n\n"
            f"initialization_id={request.initialization_id}\n"
            f"draft_revision={request.draft_revision}\n"
            f"draft_hash={request.draft_hash}\n"
            f"draft={_canonical_json(request.draft)}"
        )
        raw, _thread_id = self._invoke(
            prompt,
            _proposal_schema(),
            native_session_ref=None,
            ephemeral=True,
            job_ref=request.job_ref,
        )
        try:
            content = _validated_question(raw)
        except (TypeError, ValueError) as error:
            raise DraftingUnavailable("codex_proposal_invalid") from error
        return ProposalDraftResult(content=content, adapter_kind="codex_cli")

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        context = (
            "你是创建 Quest 之前的 Intent Drafting Session 助手。帮助用户澄清意图，"
            "但只能回复建议；不得修改草稿、确认 bundle、创建领域对象或签发 receipt。\n\n"
            f"initialization_id={request.initialization_id}\n"
            f"current_draft_revision={request.draft_revision}\n"
            f"current_draft_hash={request.draft_hash}\n"
            f"current_draft={_canonical_json(request.draft)}\n"
            f"user_message={request.message}"
        )
        raw, thread_id = self._invoke(
            context,
            _reply_schema(),
            native_session_ref=request.native_session_ref,
            ephemeral=False,
            job_ref=request.job_ref,
        )
        reply = raw.get("reply")
        if (
            set(raw) != {"reply"}
            or not isinstance(reply, str)
            or not reply.strip()
            or len(reply.strip()) > INTENT_REPLY_MAX_LENGTH
        ):
            raise DraftingUnavailable("codex_intent_reply_invalid")
        if thread_id is None:
            raise DraftingUnavailable("codex_session_ref_missing")
        return IntentTurnResult(
            reply=reply.strip(),
            native_session_ref=thread_id,
            adapter_kind="codex_cli",
        )

    def _invoke(
        self,
        prompt: str,
        schema: dict[str, object],
        *,
        native_session_ref: str | None,
        ephemeral: bool,
        job_ref: str | None,
    ) -> tuple[dict[str, object], str | None]:
        with tempfile.TemporaryDirectory(
            prefix="provider-", dir=self._workspace
        ) as raw_directory:
            directory = Path(raw_directory)
            schema_path = directory / "output-schema.json"
            result_path = directory / "last-message.json"
            schema_path.write_text(_canonical_json(schema), encoding="utf-8")
            argv = [
                self._executable,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--cd",
                str(self._workspace),
                "--json",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
            ]
            if ephemeral:
                argv.append("--ephemeral")
            if native_session_ref is not None:
                argv.extend(["resume", native_session_ref, "-"])
            else:
                argv.append("-")
            try:
                run_job = getattr(self._process_runner, "run_job", None)
                if job_ref is not None and callable(run_job):
                    completed = run_job(
                        job_ref, argv, prompt, self._timeout_seconds
                    )
                else:
                    completed = self._process_runner(
                        argv, prompt, self._timeout_seconds
                    )
            except _ProcessStopped as error:
                raise DraftingUnavailable("codex_cli_stopped") from error
            except FileNotFoundError as error:
                raise DraftingUnavailable("codex_cli_unavailable") from error
            except subprocess.TimeoutExpired as error:
                raise DraftingUnavailable("codex_cli_timeout") from error
            except OSError as error:
                raise DraftingUnavailable("codex_cli_io_unavailable") from error
            if completed.returncode != 0:
                raise DraftingUnavailable("codex_cli_failed")
            if _text_exceeds_limit(
                completed.stdout, PROVIDER_STREAM_MAX_BYTES
            ) or _text_exceeds_limit(completed.stderr, PROVIDER_STREAM_MAX_BYTES):
                raise DraftingUnavailable("codex_output_too_large")
            try:
                decoded = json.loads(
                    _read_bounded_text(result_path, PROVIDER_RESULT_MAX_BYTES)
                )
            except DraftingUnavailable:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DraftingUnavailable("codex_output_invalid") from error
            if not isinstance(decoded, dict):
                raise DraftingUnavailable("codex_output_invalid")
            thread_id = native_session_ref or _thread_id(completed.stdout)
            return cast(dict[str, object], decoded), thread_id


class NvidiaSmiProbe(HostComputeProbe):
    """Read-only observation adapter for the actual NVIDIA devices on the host."""

    _ARGV = [
        "nvidia-smi",
        "--query-gpu=uuid,name,memory.total",
        "--format=csv,noheader,nounits",
    ]

    def __init__(
        self,
        *,
        timeout_seconds: float = 5.0,
        command_runner: CommandRunner = _run_command,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._command_runner = command_runner

    def observe(self) -> HostComputeSnapshot:
        observed_at = time.time()
        try:
            completed = self._command_runner(list(self._ARGV), self._timeout_seconds)
        except FileNotFoundError:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_smi_unavailable",
            )
        except subprocess.TimeoutExpired:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_smi_timeout",
            )
        except OSError:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_smi_io_unavailable",
            )
        if completed.returncode != 0:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_smi_failed",
            )
        try:
            devices = tuple(_parse_device(line) for line in completed.stdout.splitlines())
        except ValueError:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_smi_output_invalid",
            )
        if not devices:
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=observed_at,
                devices=(),
                reason_code="nvidia_device_not_found",
            )
        return HostComputeSnapshot(
            status="ready", observed_at=observed_at, devices=devices
        )


def _parse_device(line: str) -> HostComputeDevice:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 3 or not parts[0].startswith("GPU-") or not parts[1]:
        raise ValueError("invalid nvidia-smi row")
    memory_total_mib = int(parts[2])
    if memory_total_mib <= 0:
        raise ValueError("invalid GPU memory")
    return HostComputeDevice(
        uuid=parts[0], name=parts[1], memory_total_mib=memory_total_mib
    )


def _thread_id(stdout: str) -> str | None:
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            value = event.get("thread_id")
            if isinstance(value, str) and value:
                return value
    return None


def _validated_question(value: dict[str, object]) -> dict[str, str]:
    if set(value) != set(QUESTION_FIELDS):
        raise ValueError("question fields do not match schema")
    normalized: dict[str, str] = {}
    for field in QUESTION_FIELDS:
        item = value[field]
        if not isinstance(item, str):
            raise TypeError(field)
        item = item.strip()
        if not item or item.lower() in _PSEUDO_VALUES:
            raise ValueError(field)
        if len(item) > QUESTION_FIELD_MAX_LENGTHS[field]:
            raise ValueError(field)
        normalized[field] = item
    return normalized


def _proposal_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            field: {
                "type": "string",
                "minLength": 1,
                "maxLength": QUESTION_FIELD_MAX_LENGTHS[field],
            }
            for field in QUESTION_FIELDS
        },
        "required": list(QUESTION_FIELDS),
    }


def _reply_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {
                "type": "string",
                "minLength": 1,
                "maxLength": INTENT_REPLY_MAX_LENGTH,
            }
        },
        "required": ["reply"],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _text_exceeds_limit(value: str, maximum_bytes: int) -> bool:
    if len(value) > maximum_bytes:
        return True
    return len(value.encode("utf-8")) > maximum_bytes


def _read_bounded_text(path: Path, maximum_bytes: int) -> str:
    if path.stat().st_size > maximum_bytes:
        raise DraftingUnavailable("codex_output_too_large")
    with path.open("rb") as source:
        value = source.read(maximum_bytes + 1)
    if len(value) > maximum_bytes:
        raise DraftingUnavailable("codex_output_too_large")
    return value.decode("utf-8")
