from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Protocol

from meta_research.experiment_contract import (
    EXPERIMENT_MAX_PROVIDER_OPERATION_GENERATIONS,
    EXPERIMENT_RETRYABLE_PROVIDER_FAILURES,
    EXPERIMENT_REQUIRED_METRICS,
    ExperimentDomainAdmission,
    ExperimentIntent,
    ExperimentObservation,
    ExperimentObserver,
    ExperimentProviderRequest,
    ExperimentProviderResult,
    ExperimentProviderUnavailable,
    ExperimentRuntimeBinding,
    MaterializedExperimentCheckpoint,
    experiment_definition_document,
    experiment_execution_log_document,
    validate_experiment_provider_result,
)
from meta_research.experiment_provider_supervisor import (
    EXIT_SCHEMA,
    MARKER_SCHEMA,
    OBSERVATION_MAX_RECORD_BYTES,
    OBSERVATION_SCHEMA,
    REQUEST_SCHEMA,
    ExperimentSupervisorError,
    decode_observation_line,
    ensure_transport_key,
    file_sha256,
    read_signed,
    write_signed,
)
from meta_research.owners.agent_runtime import AgentRuntimeInterface, ExperimentRun
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import ResearchGraphInterface
from meta_research.owners.research_memory import (
    AssetIntakeRequest,
    ResearchMemoryInterface,
)
from meta_research.provider_supervisor import (
    ProviderSupervisorError,
    request_supervisor_stop,
    supervisor_request_never_started,
)


_EXPERIMENT_CONTROL_WON_CODES = frozenset(
    {
        "runtime_fence_revoked",
        "experiment_fence_stale",
        "runtime_reconciliation_required",
        "runtime_run_suspended",
        "terminal_run_cannot_reopen",
    }
)


class ExperimentProvider(Protocol):
    def runtime_binding(self) -> ExperimentRuntimeBinding: ...

    def implementation_bundle(self) -> bytes: ...

    def execute(
        self,
        request: ExperimentProviderRequest,
        observe: ExperimentObserver,
    ) -> ExperimentProviderResult: ...

    def reconcile_cancelled_job(self, job_ref: str) -> bool: ...


class BuiltinMicroExperimentProvider:
    """Packaged real subprocess adapter for the installable micro experiment."""

    _MAX_STDOUT_RECORDS = 4096
    _MAX_OBSERVATIONS = 8192
    _TELEMETRY_CADENCE_SECONDS = 0.25

    def __init__(
        self,
        workspace: Path,
        *,
        runner_path: Path | None = None,
        wall_timeout_seconds: float = 300.0,
        stdout_max_bytes: int = 1024 * 1024,
        result_max_bytes: int = 1024 * 1024,
    ) -> None:
        package_root = Path(__file__).resolve().parent
        self._workspace = workspace.expanduser().resolve()
        self._runner = (
            runner_path or package_root / "experiment_runner.py"
        ).expanduser().resolve()
        self._supervisor = package_root / "experiment_provider_supervisor.py"
        if (
            not self._runner.is_file()
            or not self._supervisor.is_file()
            or not isinstance(wall_timeout_seconds, (int, float))
            or isinstance(wall_timeout_seconds, bool)
            or not 0 < float(wall_timeout_seconds) <= 24 * 60 * 60
            or isinstance(stdout_max_bytes, bool)
            or not 0 < stdout_max_bytes <= 16 * 1024 * 1024
            or isinstance(result_max_bytes, bool)
            or not 0 < result_max_bytes <= 16 * 1024 * 1024
        ):
            raise OwnerConflict("experiment_provider_configuration_invalid")
        self._wall_timeout_seconds = float(wall_timeout_seconds)
        self._stdout_max_bytes = stdout_max_bytes
        self._result_max_bytes = result_max_bytes
        self._state_lock = threading.Lock()
        self._active_supervisors: set[subprocess.Popen[bytes]] = set()
        self._observation_progress: dict[str, tuple[int, int]] = {}
        self._stop_requested = False

    @property
    def workspace(self) -> Path:
        return self._workspace

    def implementation_bundle(self) -> bytes:
        package_root = Path(__file__).resolve().parent
        members = [
            package_root / "experiment.py",
            package_root / "experiment_contract.py",
            package_root / "experiment_provider_supervisor.py",
            package_root / "provider_supervisor.py",
            self._runner,
        ]
        unique: dict[Path, str] = {}
        for member in members:
            resolved = member.resolve()
            if resolved in unique:
                continue
            if resolved.parent == package_root:
                name = f"meta_research/{resolved.name}"
            else:
                name = f"runner/{resolved.name}"
            unique[resolved] = name
        document = {
            "schema_ref": "meta-research/experiment-implementation-bundle/v1",
            "members": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "content_base64": base64.b64encode(path.read_bytes()).decode(
                        "ascii"
                    ),
                }
                for path, name in sorted(unique.items(), key=lambda item: item[1])
            ],
        }
        return canonical_json(document).encode("utf-8")

    def runtime_binding(self) -> ExperimentRuntimeBinding:
        runner_hash = hashlib.sha256(self.implementation_bundle()).hexdigest()
        resources = [
            "host:cpu",
            "process-group:local",
            f"limit:wall-time-seconds:{self._wall_timeout_seconds:g}",
            f"limit:stdout-bytes:{self._stdout_max_bytes}",
            f"limit:result-bytes:{self._result_max_bytes}",
            f"limit:stdout-records:{self._MAX_STDOUT_RECORDS}",
            f"limit:observation-count:{self._MAX_OBSERVATIONS}",
            f"limit:observation-record-bytes:{OBSERVATION_MAX_RECORD_BYTES}",
            (
                "telemetry:cadence-seconds:"
                f"{self._TELEMETRY_CADENCE_SECONDS:g}"
            ),
        ]
        return ExperimentRuntimeBinding(
            runner_bundle_hash=runner_hash,
            adapter_ref="builtin-micro-experiment-v2-durable",
            interpreter_ref=(
                f"cpython-{sys.version_info.major}.{sys.version_info.minor}:"
                f"{Path(sys.executable).resolve()}"
            ),
            capability_bindings=("subprocess", "telemetry-sampling"),
            resource_bindings=tuple(sorted(resources)),
        )

    def execute(
        self,
        request: ExperimentProviderRequest,
        observe: ExperimentObserver,
    ) -> ExperimentProviderResult:
        if (
            not request.provider_operation_ref
            or len(request.provider_operation_ref) > 128
        ):
            raise OwnerConflict("experiment_provider_operation_ref_invalid")
        request.validate()
        with self._state_lock:
            if self._stop_requested:
                raise OwnerConflict("experiment_provider_shutdown_detached")
        variant_inputs = request.variant_run_binding.inputs
        try:
            payload = {
                "sample_count": variant_inputs["data"]["sample_count"],
                "variant_parameter": variant_inputs["recipe"][
                    "variant_parameter"
                ],
                "request_kind": request.request_kind,
                "selected_checkpoints": [
                    checkpoint.as_invocation_dict()
                    for checkpoint in request.selected_checkpoints
                ],
            }
        except (KeyError, TypeError) as error:
            raise OwnerConflict("experiment_provider_request_invalid") from error
        operation = self._prepare_operation(request, payload)
        receipt = self._ensure_terminal_operation(operation, observe)
        output = self._verified_output(operation, receipt)
        try:
            text_output = output.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise OwnerConflict("experiment_provider_output_invalid") from error
        raw_lines: list[str] = []
        result_lines: list[str] = []
        records = output.split(b"\n")
        if records and records[-1] == b"":
            records.pop()
        raw_size = 0
        for index, encoded_line in enumerate(records):
            if b"\r" in encoded_line or b"\x00" in encoded_line:
                raise OwnerConflict("experiment_provider_output_invalid")
            line = encoded_line.decode("utf-8", errors="strict")
            if line.startswith("META_RESEARCH_RESULT\t"):
                result_lines.append(line.split("\t", 1)[1])
            else:
                raw_lines.append(line)
                raw_size += len(encoded_line)
                if index < len(records) - 1 or output.endswith(b"\n"):
                    raw_size += 1
        if (
            raw_size > self._stdout_max_bytes
            or len(raw_lines) > self._MAX_STDOUT_RECORDS
            or any(len(line) > 4000 for line in raw_lines)
            or len(result_lines) != 1
        ):
            code = (
                "experiment_provider_output_limit"
                if raw_size > self._stdout_max_bytes
                or len(raw_lines) > self._MAX_STDOUT_RECORDS
                or any(len(line) > 4000 for line in raw_lines)
                else "experiment_result_missing"
            )
            raise OwnerConflict(code)
        encoded_result = result_lines[0].encode("utf-8")
        if len(encoded_result) > self._result_max_bytes:
            raise OwnerConflict("experiment_provider_output_limit")
        try:
            result_document = json.loads(result_lines[0])
        except json.JSONDecodeError as error:
            raise OwnerConflict("experiment_provider_output_invalid") from error
        if not isinstance(result_document, dict):
            raise OwnerConflict("experiment_result_invalid")
        self._raise_if_shutdown_requested()
        checkpoint = result_document.get("checkpoint")
        analysis = result_document.get("analysis")
        result_content = result_document.get("result_content")
        if (
            (
                request.request_kind == "retrain"
                and not isinstance(checkpoint, dict)
            )
            or (request.request_kind == "remeasure" and checkpoint is not None)
            or not isinstance(analysis, dict)
            or not isinstance(result_content, dict)
        ):
            raise OwnerConflict("experiment_result_invalid")
        result = ExperimentProviderResult(
            checkpoint_content=(
                None
                if checkpoint is None
                else canonical_json(checkpoint).encode("utf-8")
            ),
            analysis=analysis,
            result_content=result_content,
            adapter_kind="builtin_subprocess",
        )
        validate_experiment_provider_result(
            result,
            request_kind=request.request_kind,
        )
        return result

    def request_stop(self) -> None:
        with self._state_lock:
            self._stop_requested = True

    def reconcile_cancelled_job(self, job_ref: str) -> bool:
        """Stop one detached experiment operation and verify its exit receipt."""

        operation = (
            self._workspace
            / "provider-operations"
            / hashlib.sha256(job_ref.encode("utf-8")).hexdigest()
        )
        if not operation.exists():
            return True
        try:
            key = ensure_transport_key(self._workspace)
            invocation = read_signed(operation / "invocation.json", key)
            if (
                invocation.get("schema_ref")
                != "meta-research/experiment-provider-operation/v1"
                or invocation.get("provider_operation_ref") != job_ref
            ):
                return False
            invocation_hash = canonical_hash(invocation)
            receipt_path = operation / "supervisor-exit.json"
            if not receipt_path.is_file():
                if not (operation / "supervisor-ready.json").is_file():
                    if not supervisor_request_never_started(
                        operation,
                        key=key,
                        invocation_hash=invocation_hash,
                        request_schema=REQUEST_SCHEMA,
                    ):
                        return False
                    return True
                if not request_supervisor_stop(
                    operation,
                    key=key,
                    invocation_hash=invocation_hash,
                    ready_schema=MARKER_SCHEMA,
                ):
                    return False
                if not receipt_path.is_file():
                    return True
            receipt = read_signed(receipt_path, key)
            stdin_path = operation / "stdin.json"
            stdout_path = operation / "stdout.bin"
            observation_path = operation / "observations.jsonl"
            if (
                set(receipt)
                != {
                    "schema_ref",
                    "invocation_hash",
                    "termination_reason",
                    "returncode",
                    "stdin_hash",
                    "stdout_hash",
                    "stdout_bytes",
                    "observation_hash",
                    "observation_bytes",
                    "observation_count",
                    "started_at",
                    "completed_at",
                }
                or receipt.get("schema_ref") != EXIT_SCHEMA
                or receipt.get("invocation_hash") != invocation_hash
                or receipt.get("termination_reason")
                not in {
                    "completed",
                    "timeout",
                    "stopped",
                    "output_limit",
                    "descendant_process",
                    "launch_failed",
                }
                or not isinstance(receipt.get("returncode"), int)
                or isinstance(receipt.get("returncode"), bool)
                or receipt.get("stdin_hash") != file_sha256(stdin_path)
                or receipt.get("stdout_hash") != file_sha256(stdout_path)
                or receipt.get("stdout_bytes") != stdout_path.stat().st_size
                or receipt.get("observation_hash")
                != file_sha256(observation_path)
                or receipt.get("observation_bytes")
                != observation_path.stat().st_size
            ):
                return False
        except (
            OSError,
            ExperimentSupervisorError,
            ProviderSupervisorError,
        ):
            return False
        return True

    def _raise_if_shutdown_requested(self) -> None:
        with self._state_lock:
            stopped = self._stop_requested
        if stopped:
            # The detached supervisor owns the durable provider effect. A daemon
            # stop must fence this AR Attempt without converting the still-live
            # operation into a terminal domain failure.
            raise OwnerConflict("experiment_provider_shutdown_detached")

    def _prepare_operation(
        self,
        request: ExperimentProviderRequest,
        payload: dict[str, object],
    ) -> Path:
        try:
            key = ensure_transport_key(self._workspace)
        except (OSError, ExperimentSupervisorError) as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        runtime_hash = self.runtime_binding().runner_bundle_hash
        invocation = {
            "schema_ref": "meta-research/experiment-provider-operation/v1",
            "provider_operation_ref": request.provider_operation_ref,
            # Experiment effects are non-repeatable. Technical AR Attempts may
            # change, but they reconcile generation 1 of this same provider
            # operation and never authorize a second subprocess.
            "provider_operation_generation": (
                request.provider_operation_generation
            ),
            "provider_operation_retry_permitted": True,
            "request_kind": request.request_kind,
            "selected_checkpoints": payload["selected_checkpoints"],
            "identities": request.identities.as_dict(),
            "variant_input_binding_ref": request.variant_run_binding.binding_ref,
            "variant_input_hash": request.variant_run_binding.inputs_hash,
            "measurement_input_binding_ref": (
                request.evaluation_attempt_binding.binding_ref
            ),
            "measurement_input_hash": (
                request.evaluation_attempt_binding.inputs_hash
            ),
            "required_metrics": list(request.required_metrics),
            "runtime_bundle_hash": runtime_hash,
            "stdin_hash": hashlib.sha256(
                canonical_json(payload).encode("utf-8")
            ).hexdigest(),
        }
        invocation_hash = canonical_hash(invocation)
        operation = (
            self._workspace
            / "provider-operations"
            / hashlib.sha256(
                request.provider_operation_ref.encode("utf-8")
            ).hexdigest()
        )
        operation.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            write_signed(operation / "invocation.json", invocation, key)
            self._ensure_exact_file(
                operation / "stdin.json", canonical_json(payload).encode("utf-8")
            )
            request_payload: dict[str, object] = {
                "schema_ref": REQUEST_SCHEMA,
                "invocation_hash": invocation_hash,
                "argv": [sys.executable, "-I", str(self._runner)],
                "wall_timeout_seconds": self._wall_timeout_seconds,
                "stdout_max_bytes": self._stdout_max_bytes,
                "stdout_max_records": self._MAX_STDOUT_RECORDS,
                "result_max_bytes": self._result_max_bytes,
                "stdin_path": str(operation / "stdin.json"),
                "stdout_path": str(operation / "stdout.bin"),
                "observation_path": str(operation / "observations.jsonl"),
                "started_path": str(operation / "provider-started.json"),
                "ready_path": str(operation / "supervisor-ready.json"),
                "receipt_path": str(operation / "supervisor-exit.json"),
                "observation_max_count": self._MAX_OBSERVATIONS,
                "telemetry_cadence_seconds": (
                    self._TELEMETRY_CADENCE_SECONDS
                ),
            }
            write_signed(operation / "supervisor-request.json", request_payload, key)
        except ExperimentSupervisorError as error:
            code = (
                "experiment_provider_identity_conflict"
                if str(error) == "identity_conflict"
                else "experiment_provider_spool_invalid"
            )
            raise OwnerConflict(code) from error
        return operation

    def _ensure_terminal_operation(
        self,
        operation: Path,
        observe: ExperimentObserver,
    ) -> dict[str, object]:
        receipt_path = operation / "supervisor-exit.json"
        supervisor: subprocess.Popen[bytes] | None = None
        try:
            key = ensure_transport_key(self._workspace)
            invocation = read_signed(operation / "invocation.json", key)
        except (OSError, ExperimentSupervisorError) as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        invocation_hash = canonical_hash(invocation)
        if not receipt_path.exists() and not (
            operation / "provider-started.json"
        ).exists():
            try:
                supervisor = subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(self._supervisor),
                        str(operation / "supervisor-request.json"),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={"PATH": os.environ.get("PATH", "")},
                    start_new_session=True,
                )
            except OSError as error:
                raise ExperimentProviderUnavailable(
                    "experiment_provider_launch_failed",
                    durable_outcome="terminal",
                ) from error
            with self._state_lock:
                self._active_supervisors.add(supervisor)
        deadline = time.monotonic() + self._wall_timeout_seconds + 5.0
        with self._state_lock:
            observation_cursor, next_observation_sequence = (
                self._observation_progress.get(invocation_hash, (0, 1))
            )
        try:
            while not receipt_path.exists():
                self._raise_if_shutdown_requested()
                observation_cursor, next_observation_sequence = (
                    self._tail_observation_ledger(
                        operation,
                        key=key,
                        invocation_hash=invocation_hash,
                        cursor=observation_cursor,
                        next_sequence=next_observation_sequence,
                        observe=observe,
                    )
                )
                if supervisor is not None and supervisor.poll() is not None:
                    if supervisor.returncode != 0:
                        raise OwnerConflict("experiment_provider_spool_invalid")
                if time.monotonic() >= deadline:
                    raise OwnerConflict("experiment_provider_reconciliation_pending")
                time.sleep(0.02)
            if supervisor is not None:
                try:
                    supervisor.wait(timeout=2.0)
                except subprocess.TimeoutExpired as error:
                    raise OwnerConflict("experiment_provider_spool_invalid") from error
                if supervisor.returncode != 0:
                    raise OwnerConflict("experiment_provider_spool_invalid")
        finally:
            if supervisor is not None:
                if supervisor.poll() is None:
                    threading.Thread(
                        target=self._reap_supervisor,
                        args=(supervisor,),
                        daemon=True,
                    ).start()
                else:
                    with self._state_lock:
                        self._active_supervisors.discard(supervisor)
        self._raise_if_shutdown_requested()
        observation_cursor, next_observation_sequence = (
            self._tail_observation_ledger(
                operation,
                key=key,
                invocation_hash=invocation_hash,
                cursor=observation_cursor,
                next_sequence=next_observation_sequence,
                observe=observe,
            )
        )
        try:
            receipt = read_signed(receipt_path, key)
        except (OSError, ExperimentSupervisorError) as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        if (
            set(receipt)
            != {
                "schema_ref",
                "invocation_hash",
                "termination_reason",
                "returncode",
                "stdin_hash",
                "stdout_hash",
                "stdout_bytes",
                "observation_hash",
                "observation_bytes",
                "observation_count",
                "started_at",
                "completed_at",
            }
            or receipt.get("schema_ref") != EXIT_SCHEMA
            or receipt.get("invocation_hash") != invocation_hash
            or not isinstance(receipt.get("returncode"), int)
            or isinstance(receipt.get("returncode"), bool)
            or not isinstance(receipt.get("stdout_bytes"), int)
            or isinstance(receipt.get("stdout_bytes"), bool)
            or int(receipt["stdout_bytes"]) < 0
            or not isinstance(receipt.get("observation_bytes"), int)
            or isinstance(receipt.get("observation_bytes"), bool)
            or not 0 <= int(receipt["observation_bytes"]) <= (
                self._MAX_OBSERVATIONS * OBSERVATION_MAX_RECORD_BYTES
            )
            or not isinstance(receipt.get("observation_count"), int)
            or isinstance(receipt.get("observation_count"), bool)
            or not 0 <= int(receipt["observation_count"]) <= (
                self._MAX_OBSERVATIONS
            )
            or not isinstance(receipt.get("stdin_hash"), str)
            or len(receipt["stdin_hash"]) != 64
            or not isinstance(receipt.get("stdout_hash"), str)
            or len(receipt["stdout_hash"]) != 64
            or not isinstance(receipt.get("observation_hash"), str)
            or len(receipt["observation_hash"]) != 64
            or not isinstance(receipt.get("started_at"), (int, float))
            or isinstance(receipt.get("started_at"), bool)
            or not math.isfinite(float(receipt["started_at"]))
            or not isinstance(receipt.get("completed_at"), (int, float))
            or isinstance(receipt.get("completed_at"), bool)
            or not math.isfinite(float(receipt["completed_at"]))
            or float(receipt["completed_at"]) < float(receipt["started_at"])
            or receipt.get("observation_bytes") != observation_cursor
            or receipt.get("observation_count")
            != next_observation_sequence - 1
        ):
            raise OwnerConflict("experiment_provider_spool_invalid")
        observation_path = operation / "observations.jsonl"
        try:
            observation_hash = file_sha256(observation_path)
        except ExperimentSupervisorError as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        if receipt.get("observation_hash") != observation_hash:
            raise OwnerConflict("experiment_provider_spool_invalid")
        reason = receipt.get("termination_reason")
        returncode = receipt.get("returncode")
        if reason == "timeout":
            raise ExperimentProviderUnavailable(
                "experiment_provider_timeout",
                durable_outcome="terminal",
            )
        if reason == "output_limit":
            raise ExperimentProviderUnavailable(
                "experiment_provider_output_limit",
                durable_outcome="terminal",
            )
        if reason == "descendant_process":
            raise ExperimentProviderUnavailable(
                "experiment_provider_descendant_process",
                durable_outcome="terminal",
            )
        if reason == "stopped":
            raise ExperimentProviderUnavailable(
                "experiment_provider_stopped",
                durable_outcome="terminal",
            )
        if reason != "completed" or returncode != 0:
            raise ExperimentProviderUnavailable(
                "experiment_provider_failed",
                durable_outcome="terminal",
            )
        return receipt

    def _tail_observation_ledger(
        self,
        operation: Path,
        *,
        key: bytes,
        invocation_hash: str,
        cursor: int,
        next_sequence: int,
        observe: ExperimentObserver,
    ) -> tuple[int, int]:
        path = operation / "observations.jsonl"
        if not path.exists():
            return cursor, next_sequence
        next_cursor = cursor
        try:
            with path.open("rb") as stream:
                stream.seek(cursor)
                while True:
                    encoded = stream.readline(OBSERVATION_MAX_RECORD_BYTES + 1)
                    if not encoded:
                        break
                    if len(encoded) > OBSERVATION_MAX_RECORD_BYTES:
                        raise OwnerConflict("experiment_provider_spool_invalid")
                    if not encoded.endswith(b"\n"):
                        # The supervisor publishes one fsynced line at a time.
                        # A concurrently observed partial line is retried from
                        # its original cursor on the next poll.
                        break
                    payload = decode_observation_line(encoded[:-1], key)
                    observed_at = payload.get("observed_at")
                    observation_payload = payload.get("payload")
                    kind = payload.get("kind")
                    if (
                        set(payload)
                        != {
                            "schema_ref",
                            "invocation_hash",
                            "sequence",
                            "kind",
                            "payload",
                            "observed_at",
                        }
                        or payload.get("schema_ref") != OBSERVATION_SCHEMA
                        or payload.get("invocation_hash") != invocation_hash
                        or payload.get("sequence") != next_sequence
                        or kind not in {"stdout", "telemetry"}
                        or type(observation_payload) is not dict
                        or not isinstance(observed_at, (int, float))
                        or isinstance(observed_at, bool)
                        or not math.isfinite(float(observed_at))
                    ):
                        raise OwnerConflict("experiment_provider_spool_invalid")
                    if kind == "stdout" and (
                        set(observation_payload) != {"line", "stream"}
                        or observation_payload.get("stream") != "stdout"
                        or not isinstance(observation_payload.get("line"), str)
                        or len(str(observation_payload["line"])) > 4000
                        or "\n" in str(observation_payload["line"])
                        or "\r" in str(observation_payload["line"])
                        or "\x00" in str(observation_payload["line"])
                    ):
                        raise OwnerConflict("experiment_provider_spool_invalid")
                    observe(
                        ExperimentObservation(
                            kind,
                            observation_payload,
                            float(observed_at),
                        )
                    )
                    next_sequence += 1
                    next_cursor = stream.tell()
                    self._remember_observation_progress(
                        invocation_hash,
                        cursor=next_cursor,
                        next_sequence=next_sequence,
                    )
        except OwnerConflict:
            raise
        except (OSError, ExperimentSupervisorError) as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        return next_cursor, next_sequence

    def _remember_observation_progress(
        self,
        invocation_hash: str,
        *,
        cursor: int,
        next_sequence: int,
    ) -> None:
        """Advance only after the current AR observer accepted the ledger row."""

        with self._state_lock:
            previous_cursor, previous_sequence = self._observation_progress.get(
                invocation_hash, (0, 1)
            )
            if cursor < previous_cursor or next_sequence < previous_sequence:
                raise OwnerConflict("experiment_provider_spool_invalid")
            self._observation_progress[invocation_hash] = (
                cursor,
                next_sequence,
            )

    def _reap_supervisor(self, supervisor: subprocess.Popen[bytes]) -> None:
        try:
            supervisor.wait()
        finally:
            with self._state_lock:
                self._active_supervisors.discard(supervisor)

    def _verified_output(
        self, operation: Path, receipt: dict[str, object]
    ) -> bytes:
        stdout_path = operation / "stdout.bin"
        try:
            output = stdout_path.read_bytes()
            key = ensure_transport_key(self._workspace)
            invocation = read_signed(operation / "invocation.json", key)
        except OSError as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        except ExperimentSupervisorError as error:
            raise OwnerConflict("experiment_provider_spool_invalid") from error
        if (
            receipt.get("stdin_hash") != file_sha256(operation / "stdin.json")
            or invocation.get("stdin_hash") != receipt.get("stdin_hash")
            or receipt.get("stdout_hash") != hashlib.sha256(output).hexdigest()
            or receipt.get("stdout_bytes") != len(output)
            or len(output)
            > self._stdout_max_bytes + self._result_max_bytes + 1024
        ):
            raise OwnerConflict("experiment_provider_spool_invalid")
        return output

    @staticmethod
    def _ensure_exact_file(path: Path, content: bytes) -> None:
        try:
            with path.open("xb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
            return
        except FileExistsError:
            pass
        try:
            existing = path.read_bytes()
        except OSError as error:
            raise ExperimentSupervisorError("spool_invalid") from error
        if existing != content:
            raise ExperimentSupervisorError("identity_conflict")

class ExperimentService:
    """Coordinates public intent while preserving all three Owner boundaries."""

    def __init__(
        self,
        research_graph: ResearchGraphInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        provider: ExperimentProvider,
    ) -> None:
        self._research_graph = research_graph
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._provider = provider
        self._admission_lock = threading.Lock()

    def start(
        self,
        intent: ExperimentIntent,
        idempotency_key: str,
        *,
        require_idle: bool = False,
    ) -> dict[str, object]:
        if require_idle:
            with self._admission_lock:
                return self._start(intent, idempotency_key, require_idle=True)
        return self._start(intent, idempotency_key, require_idle=False)

    def _start(
        self,
        intent: ExperimentIntent,
        idempotency_key: str,
        *,
        require_idle: bool,
    ) -> dict[str, object]:
        intent.validate()
        if not idempotency_key or len(idempotency_key) > 128:
            raise OwnerConflict("experiment_idempotency_key_invalid")
        replay = self._research_graph.preflight_experiment(
            intent=intent,
            idempotency_key=idempotency_key,
        )
        if replay is not None:
            frozen_runtime_binding = _frozen_experiment_runtime_binding(replay)
            admission = self._research_graph.admit_experiment(
                intent=intent,
                runtime_binding=frozen_runtime_binding,
                definition_binding=replay.execution_request.definition_binding,
                implementation_binding=(
                    replay.execution_request.implementation_binding
                ),
                idempotency_key=idempotency_key,
            )
            self._agent_runtime.admit_experiment(
                admission=admission,
                runtime_binding=frozen_runtime_binding,
                require_idle=require_idle,
            )
            return self.query(admission.identities.evaluation_attempt_ref)
        if (
            require_idle
            and self._agent_runtime.query_active_experiment_run() is not None
        ):
            raise OwnerConflict("experiment_execution_busy")
        runtime_binding = self._provider.runtime_binding()
        runtime_binding.as_dict()
        implementation_reader = getattr(
            self._provider, "implementation_bundle", None
        )
        if not callable(implementation_reader):
            raise OwnerConflict("experiment_implementation_bundle_unavailable")
        implementation_content = implementation_reader()
        if (
            not isinstance(implementation_content, bytes)
            or hashlib.sha256(implementation_content).hexdigest()
            != runtime_binding.runner_bundle_hash
        ):
            raise OwnerConflict("experiment_implementation_bundle_invalid")
        implementation_intake = self._research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name=(
                    f"experiment-implementation-{intent.execution_request_ref}.json"
                ),
                media_type=(
                    "application/vnd.meta-research.experiment-implementation+json"
                ),
                content=implementation_content,
                provenance={
                    "kind": "experiment_implementation",
                    "execution_request_ref": intent.execution_request_ref,
                    "quest_ref": intent.quest_ref,
                    "runner_bundle_hash": runtime_binding.runner_bundle_hash,
                },
            ),
            idempotency_key=(
                f"experiment-implementation:{intent.execution_request_ref}"
            ),
        )
        if (
            implementation_intake.status != "accepted"
            or implementation_intake.asset is None
        ):
            raise OwnerConflict("experiment_implementation_not_accepted")
        definition = experiment_definition_document(intent, runtime_binding)
        definition_intake = self._research_memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name=f"experiment-definition-{intent.execution_request_ref}.json",
                media_type="application/vnd.meta-research.experiment-definition+json",
                content=canonical_json(definition).encode("utf-8"),
                provenance={
                    "kind": "experiment_definition",
                    "execution_request_ref": intent.execution_request_ref,
                    "quest_ref": intent.quest_ref,
                },
            ),
            idempotency_key=f"experiment-definition:{intent.execution_request_ref}",
        )
        if definition_intake.status != "accepted" or definition_intake.asset is None:
            raise OwnerConflict("experiment_definition_not_accepted")
        admission: ExperimentDomainAdmission = self._research_graph.admit_experiment(
            intent=intent,
            runtime_binding=runtime_binding,
            definition_binding=definition_intake.asset.as_binding(),
            implementation_binding=implementation_intake.asset.as_binding(),
            idempotency_key=idempotency_key,
        )
        self._agent_runtime.admit_experiment(
            admission=admission,
            runtime_binding=runtime_binding,
            require_idle=require_idle,
        )
        return self.query(admission.identities.evaluation_attempt_ref)

    def query(self, evaluation_attempt_ref: str) -> dict[str, object]:
        domain = self._research_graph.query_experiment(evaluation_attempt_ref)
        if domain is None:
            raise OwnerConflict("experiment_not_found")
        run = self._agent_runtime.query_experiment_run(evaluation_attempt_ref)
        roles = self._research_graph.query_experiment_asset_roles(
            evaluation_attempt_ref
        )
        metric_result = self._research_graph.query_formal_metric_result(
            evaluation_attempt_ref
        )
        return _public_experiment(domain, run, roles, metric_result)

    def query_current(self) -> dict[str, object] | None:
        domain = self._research_graph.query_current_experiment()
        if domain is None:
            return None
        run = self._agent_runtime.query_experiment_run(
            domain.identities.evaluation_attempt_ref
        )
        roles = self._research_graph.query_experiment_asset_roles(
            domain.identities.evaluation_attempt_ref
        )
        metric_result = self._research_graph.query_formal_metric_result(
            domain.identities.evaluation_attempt_ref
        )
        return _public_experiment(domain, run, roles, metric_result)

    def query_events(
        self,
        evaluation_attempt_ref: str,
        *,
        after_sequence: int = 0,
        limit: int = 256,
    ) -> tuple[dict[str, object], ...]:
        if self._research_graph.query_experiment(evaluation_attempt_ref) is None:
            raise OwnerConflict("experiment_not_found")
        return self._agent_runtime.query_experiment_events(
            evaluation_attempt_ref,
            after_sequence=after_sequence,
            limit=limit,
        )

    def process_once(self) -> bool:
        if self._agent_runtime.reconcile_pending_provider_cleanup(
            self._provider,
            unit_kinds=("experiment",),
        ):
            return True
        run = self._agent_runtime.claim_next_experiment()
        if run is not None:
            domain = self._research_graph.query_experiment(run.evaluation_attempt_ref)
            if domain is None:
                self._agent_runtime.fail_experiment_execution(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    failure_code="experiment_domain_binding_missing",
                )
                return True
            provider_safe = True
            try:
                current_binding = self._provider.runtime_binding()
                if canonical_hash(current_binding.as_dict()) != run.runtime_binding_hash:
                    raise OwnerConflict("experiment_runtime_binding_stale")

                def observe(observation: ExperimentObservation) -> None:
                    try:
                        self._agent_runtime.record_experiment_observation(
                            run_ref=run.run_ref,
                            attempt_ref=run.attempt_ref,
                            fence_ref=run.fence_ref,
                            kind=observation.kind,
                            payload=observation.payload,
                            observed_at=observation.observed_at,
                        )
                    except OwnerConflict as error:
                        if error.code not in _EXPERIMENT_CONTROL_WON_CODES:
                            raise
                        # A late telemetry record proves only that the detached
                        # provider is *still* active. Do not unwind to the outer
                        # terminal handler and forge a Safe Point. Ask the bound
                        # adapter to stop this exact operation, then continue
                        # monitoring until execute() observes a real exit.
                        reconcile = getattr(
                            self._provider, "reconcile_cancelled_job", None
                        )
                        if callable(reconcile):
                            try:
                                reconcile(run.provider_operation_ref)
                            except Exception:
                                pass

                result = self._provider.execute(
                    self._provider_request(
                        domain=domain,
                        provider_operation_ref=run.provider_operation_ref,
                        provider_operation_generation=(
                            run.provider_operation_generation
                        ),
                    ),
                    observe,
                )
                validate_experiment_provider_result(
                    result,
                    request_kind=domain.intent.request_kind,
                )
                self._agent_runtime.complete_experiment_execution(
                    run_ref=run.run_ref,
                    attempt_ref=run.attempt_ref,
                    fence_ref=run.fence_ref,
                    result=result.as_document(),
                )
            except OwnerConflict as error:
                managed = self._agent_runtime.query_managed_run(run.run_ref)
                provider_terminal_after_control = (
                    isinstance(error, ExperimentProviderUnavailable)
                    and error.durable_outcome == "terminal"
                    and managed is not None
                    and managed["status"] != "running"
                )
                if error.code == "experiment_provider_shutdown_detached":
                    # ProductionRuntime is stopping. Leave the fenced AR Attempt
                    # running so startup recovery can replace only the technical
                    # Attempt and reconcile this same durable provider operation.
                    provider_safe = False
                elif error.code == "experiment_provider_reconciliation_pending":
                    provider_safe = False
                    self._agent_runtime.defer_experiment_reconciliation(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        reason_code=error.code,
                    )
                elif error.code in _EXPERIMENT_CONTROL_WON_CODES:
                    # A control command won the race after the provider reached a
                    # physical terminal boundary.  The revoked Fence owns the
                    # logical result; finally still acknowledges cleanup.
                    pass
                elif self._agent_runtime.provider_quiescence_requested(run.run_ref):
                    # An operation-scoped pause/prune stop reached a real provider
                    # boundary. Keep the logical Run admitted for the control
                    # transaction, which will persist its Safe Point and suspension.
                    pass
                elif provider_terminal_after_control:
                    # The stop reconciler can prove physical termination and let
                    # the control transaction commit before this worker receives
                    # the provider's terminal receipt.  In that ordering the
                    # managed status, not the now-applied reservation, proves the
                    # old Attempt lost the race.  Treat its late receipt as an ACK;
                    # retrying/failing through the revoked Fence would leak a
                    # spurious runtime_run_suspended error to the daemon.
                    pass
                elif (
                    isinstance(error, ExperimentProviderUnavailable)
                    and error.durable_outcome == "terminal"
                    and error.code in EXPERIMENT_RETRYABLE_PROVIDER_FAILURES
                    and run.provider_operation_generation
                    < EXPERIMENT_MAX_PROVIDER_OPERATION_GENERATIONS
                ):
                    self._agent_runtime.retry_experiment_execution(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        failure_code=error.code,
                    )
                else:
                    self._agent_runtime.fail_experiment_execution(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        failure_code=error.code,
                    )
            except Exception:
                if not self._agent_runtime.provider_quiescence_requested(run.run_ref):
                    self._agent_runtime.fail_experiment_execution(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                        failure_code="experiment_provider_failed",
                    )
            finally:
                if provider_safe:
                    self._agent_runtime.acknowledge_provider_safe_point(
                        run_ref=run.run_ref,
                        attempt_ref=run.attempt_ref,
                        fence_ref=run.fence_ref,
                    )
            return True

        if self._reconcile_runtime_admission_once():
            return True

        page_size = 64
        offset = 0
        while True:
            executed_page = self._agent_runtime.query_executed_experiment_runs(
                offset=offset,
                limit=page_size,
            )
            for executed in executed_page:
                domain = self._research_graph.query_experiment(
                    executed.evaluation_attempt_ref
                )
                if domain is None:
                    continue
                roles = self._research_graph.query_experiment_asset_roles(
                    executed.evaluation_attempt_ref
                )
                result_roles = tuple(
                    role for role in roles if role.role != "checkpoint_artifact"
                )
                if not result_roles:
                    self._accept_result_assets(executed, domain)
                    return True
                if (
                    domain.formal_measurement_status == "not_attempted"
                    and self._research_graph.query_formal_metric_result(
                        executed.evaluation_attempt_ref
                    ) is None
                ):
                    try:
                        self._accept_formal_measurement(executed, roles)
                    except OwnerConflict as error:
                        if not error.code.startswith("formal_measurement_"):
                            raise
                        self._research_graph.reject_formal_measurement(
                            executed.evaluation_attempt_ref, error.code
                        )
                    return True
            if len(executed_page) < page_size:
                break
            offset += len(executed_page)
        return False

    def _provider_request(
        self,
        *,
        domain: ExperimentDomainAdmission,
        provider_operation_ref: str,
        provider_operation_generation: int,
    ) -> ExperimentProviderRequest:
        roles = self._research_graph.query_experiment_asset_roles(
            domain.identities.evaluation_attempt_ref
        )
        by_ref = {role.role_ref: role for role in roles}
        selected = []
        for ordinal, role_ref in enumerate(
            domain.intent.selected_checkpoint_role_refs
        ):
            role = by_ref.get(role_ref)
            if role is None:
                raise OwnerConflict("experiment_checkpoint_selection_not_found")
            if (
                role.role != "checkpoint_artifact"
                or role.subject_kind != "variant_run"
                or role.subject_ref != domain.identities.variant_run_ref
            ):
                raise OwnerConflict("experiment_checkpoint_selection_foreign")
            materialized = self._research_memory.materialize_asset(
                role.binding.version_ref
            )
            checkpoint = MaterializedExperimentCheckpoint(
                ordinal=ordinal,
                role_ref=role.role_ref,
                binding=role.binding,
                role_receipt=role.receipt,
                content=materialized.content,
            )
            if materialized.memory_ref != role.binding.version_ref:
                raise OwnerConflict("experiment_checkpoint_materialization_invalid")
            checkpoint.validate()
            selected.append(checkpoint)
        request = ExperimentProviderRequest(
            provider_operation_ref=provider_operation_ref,
            provider_operation_generation=provider_operation_generation,
            identities=domain.identities,
            variant_run_binding=domain.variant_run_binding,
            evaluation_attempt_binding=domain.evaluation_attempt_binding,
            required_metrics=domain.required_metrics,
            request_kind=domain.intent.request_kind,
            selected_checkpoints=tuple(selected),
        )
        request.validate()
        return request

    def _reconcile_runtime_admission_once(self) -> bool:
        """Repair the durable RG -> AR admission crash window without providers."""

        after_created_at = 0.0
        after_evaluation_attempt_ref = ""
        while True:
            candidates = self._research_graph.query_experiment_admission_refs(
                after_created_at=after_created_at,
                after_evaluation_attempt_ref=after_evaluation_attempt_ref,
                limit=64,
            )
            for evaluation_attempt_ref, _created_at in candidates:
                if (
                    self._agent_runtime.query_experiment_run(
                        evaluation_attempt_ref
                    )
                    is not None
                ):
                    continue
                admission = self._research_graph.query_experiment(
                    evaluation_attempt_ref
                )
                if admission is None:
                    raise OwnerConflict("experiment_domain_binding_missing")
                self._agent_runtime.admit_experiment(
                    admission=admission,
                    runtime_binding=_frozen_experiment_runtime_binding(admission),
                )
                return True
            if len(candidates) < 64:
                return False
            after_evaluation_attempt_ref, after_created_at = candidates[-1]

    def _accept_result_assets(
        self, run: ExperimentRun, domain: ExperimentDomainAdmission
    ) -> None:
        if (
            run.result is None
            or run.result_hash is None
            or run.execution_receipt is None
        ):
            raise OwnerConflict("experiment_execution_result_missing")
        result = ExperimentProviderResult.from_document(run.result)
        validate_experiment_provider_result(
            result,
            request_kind=domain.intent.request_kind,
        )
        execution_events = self._all_execution_events(run)
        provenance = {
            "execution_request_ref": run.execution_request_ref,
            "variant_run_ref": run.variant_run_ref,
            "evaluation_attempt_ref": run.evaluation_attempt_ref,
            "run_ref": run.run_ref,
            "execution_attempt_ref": run.attempt_ref,
            "fence_ref": run.fence_ref,
            "execution_receipt": run.execution_receipt.as_public_dict(),
        }
        if domain.intent.request_kind == "remeasure":
            checkpoint_documents = ()
        else:
            if result.checkpoint_content is None:
                raise OwnerConflict("experiment_checkpoint_invalid")
            checkpoint_documents = tuple(
                (content, "application/vnd.meta-research.checkpoint+json")
                for content in (
                    result.checkpoint_content,
                    *result.additional_checkpoint_contents,
                )
            )
        documents = {
            "checkpoint_artifact": checkpoint_documents,
            "log_asset": ((
                canonical_json(
                    experiment_execution_log_document(execution_events)
                ).encode("utf-8"),
                "application/vnd.meta-research.execution-log+json",
            ),),
            "analysis_asset": ((
                canonical_json(result.analysis).encode("utf-8"),
                "application/vnd.meta-research.analysis+json",
            ),),
            "result_content": ((
                canonical_json(result.result_content).encode("utf-8"),
                "application/vnd.meta-research.metric-result+json",
            ),),
        }
        accepted = {}
        for role, role_documents in documents.items():
            bindings = []
            for ordinal, (content, media_type) in enumerate(role_documents):
                intake = self._research_memory.submit_asset_intake(
                    AssetIntakeRequest(
                        source_kind="text",
                        custody_mode="managed",
                        display_name=(
                            f"{run.evaluation_attempt_ref}-{role}-{ordinal}.json"
                        ),
                        media_type=media_type,
                        content=content,
                        provenance={
                            **provenance,
                            "semantic_role_candidate": role,
                            "ordinal": ordinal,
                        },
                    ),
                    idempotency_key=(
                        f"experiment-asset:{run.evaluation_attempt_ref}:"
                        f"{role}:{ordinal}"
                    ),
                )
                if intake.status != "accepted" or intake.asset is None:
                    raise OwnerConflict("experiment_result_asset_not_accepted")
                bindings.append(intake.asset.as_binding())
            accepted[role] = tuple(bindings)
        self._research_graph.accept_experiment_asset_roles(
            evaluation_attempt_ref=run.evaluation_attempt_ref,
            roles=accepted,
            run_ref=run.run_ref,
            execution_attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            execution_result_hash=run.result_hash,
            execution_receipt=run.execution_receipt,
        )

    def _all_execution_events(
        self, run: ExperimentRun
    ) -> tuple[dict[str, object], ...]:
        events: list[dict[str, object]] = []
        after_sequence = 0
        while True:
            page = self._agent_runtime.query_experiment_events(
                run.evaluation_attempt_ref,
                after_sequence=after_sequence,
                limit=512,
            )
            if not page:
                break
            events.extend(page)
            after_sequence = int(page[-1]["sequence"])
            if len(page) < 512:
                break
        if len(events) != run.event_count:
            raise OwnerConflict("experiment_execution_log_incomplete")
        return tuple(events)

    def _accept_formal_measurement(self, run: ExperimentRun, roles) -> None:
        if (
            run.result is None
            or run.result_hash is None
            or run.execution_receipt is None
        ):
            raise OwnerConflict("experiment_execution_result_missing")
        result_role = next(
            (role for role in roles if role.role == "result_content"), None
        )
        if result_role is None:
            raise OwnerConflict("formal_measurement_result_role_invalid")
        materialized = self._research_memory.materialize_asset(
            result_role.binding.version_ref
        )
        try:
            result_content = json.loads(materialized.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OwnerConflict("formal_measurement_result_content_invalid") from error
        if not isinstance(result_content, dict):
            raise OwnerConflict("formal_measurement_result_content_invalid")
        provider_result = ExperimentProviderResult.from_document(run.result)
        if provider_result.result_content != result_content:
            raise OwnerConflict("formal_measurement_result_content_invalid")
        self._research_graph.accept_formal_measurement(
            evaluation_attempt_ref=run.evaluation_attempt_ref,
            result_role_ref=result_role.role_ref,
            result_content=result_content,
            run_ref=run.run_ref,
            execution_attempt_ref=run.attempt_ref,
            fence_ref=run.fence_ref,
            execution_result_hash=run.result_hash,
            execution_receipt=run.execution_receipt,
        )


def _frozen_experiment_runtime_binding(
    admission: ExperimentDomainAdmission,
) -> ExperimentRuntimeBinding:
    value = admission.execution_request.definition.get("runtime_binding")
    if not isinstance(value, dict):
        raise OwnerConflict("experiment_runtime_binding_invalid")
    capabilities = value.get("capability_bindings")
    resources = value.get("resource_bindings")
    if not isinstance(capabilities, list) or not isinstance(resources, list):
        raise OwnerConflict("experiment_runtime_binding_invalid")
    try:
        binding = ExperimentRuntimeBinding(
            schema_ref=str(value["schema_ref"]),
            runner_bundle_hash=str(value["runner_bundle_hash"]),
            adapter_ref=str(value["adapter_ref"]),
            interpreter_ref=str(value["interpreter_ref"]),
            capability_bindings=tuple(str(item) for item in capabilities),
            resource_bindings=tuple(str(item) for item in resources),
        )
        if binding.as_dict() != value:
            raise OwnerConflict("experiment_runtime_binding_invalid")
    except (KeyError, TypeError, ValueError) as error:
        raise OwnerConflict("experiment_runtime_binding_invalid") from error
    return binding


def _public_experiment(
    domain: ExperimentDomainAdmission,
    run: ExperimentRun | None,
    roles,
    metric_result,
) -> dict[str, object]:
    execution = (
        {"status": "not_attempted"}
        if run is None
        else run.as_public_dict(include_events=True)
    )
    by_role = {
        name: [role.as_public_dict() for role in roles if role.role == name]
        for name in (
            "checkpoint_artifact",
            "log_asset",
            "analysis_asset",
            "result_content",
        )
    }
    return {
        "intent": domain.intent.as_dict(),
        "execution_request": domain.execution_request.as_public_dict(),
        "identities": domain.identities.as_dict(),
        "frozen_inputs": {
            "variant_run": domain.variant_run_binding.as_public_dict(),
            "evaluation_attempt": domain.evaluation_attempt_binding.as_public_dict(),
        },
        "execution": execution,
        "assets": {
            "status": (
                "accepted"
                if by_role["log_asset"]
                and by_role["analysis_asset"]
                and by_role["result_content"]
                else "not_attempted"
            ),
            "checkpoint_artifacts": by_role["checkpoint_artifact"],
            "log_assets": by_role["log_asset"],
            "analysis_assets": by_role["analysis_asset"],
            "result_content": (
                by_role["result_content"][0]
                if by_role["result_content"]
                else None
            ),
        },
        "formal_measurement": {
            "status": domain.formal_measurement_status,
            **(
                {
                    "reason": {
                        "code": domain.formal_rejection_code,
                    }
                }
                if domain.formal_measurement_status == "rejected"
                else {}
            ),
            "metric_result": (
                None if metric_result is None else metric_result.as_public_dict()
            ),
        },
    }


__all__ = [
    "BuiltinMicroExperimentProvider",
    "ExperimentIntent",
    "ExperimentObservation",
    "ExperimentProvider",
    "ExperimentProviderRequest",
    "ExperimentProviderResult",
    "ExperimentRuntimeBinding",
    "ExperimentService",
]
