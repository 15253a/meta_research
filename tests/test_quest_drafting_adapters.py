from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    CodexDraftingAdapter,
    DraftingUnavailable,
    INTENT_REPLY_MAX_LENGTH,
    IntentTurnRequest,
    NvidiaSmiProbe,
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    ProposalDraftRequest,
    QUESTION_FIELD_MAX_LENGTHS,
)


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class RecordingRunner:
    def __init__(self, output: dict[str, object], *, thread_id: str = "thread-1"):
        self.output = output
        self.thread_id = thread_id
        self.calls: list[tuple[list[str], str, float]] = []
        self.schemas: list[dict[str, object]] = []

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((argv, input_text, timeout))
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        self.schemas.append(json.loads(schema_path.read_text(encoding="utf-8")))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(json.dumps(self.output, ensure_ascii=False))
        stdout = json.dumps(
            {"type": "thread.started", "thread_id": self.thread_id}
        )
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class OversizedProviderRunner:
    def __init__(self, target: str) -> None:
        self.target = target

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        if self.target == "result":
            with output_path.open("wb") as output:
                output.truncate(PROVIDER_RESULT_MAX_BYTES + 1)
            stdout = ""
        else:
            output_path.write_text(json.dumps(QUESTION, ensure_ascii=False))
            stdout = "x" * (PROVIDER_STREAM_MAX_BYTES + 1)
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")


class DetachedDraftingSupervisorRunner:
    """Simulate daemon response loss after the detached supervisor starts."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("durable supervisor path required")

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError("durable supervisor path required")

    def run_durable_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
        stdout_path: Path,
        pid_path: Path,
        supervisor_request_path: Path,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref, argv, input_text, timeout, stdout_path, pid_path
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "meta_research.provider_supervisor",
                str(supervisor_request_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        raise OSError("simulated daemon loss after supervisor launch")


class ForbiddenDraftingReplayRunner:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(
        self, argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls += 1
        raise AssertionError("sealed durable result must not replay provider")

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, input_text, timeout)


def _fake_drafting_codex(path: Path) -> Path:
    encoded_question = repr(json.dumps(QUESTION, ensure_ascii=False))
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        f"result_path.write_text({encoded_question}, encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', "
        "'thread_id': 'durable-drafting-session'}))\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_codex_adapter_generates_a_schema_checked_proposal_outside_domain_state(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner(QUESTION)
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    result = adapter.draft(
        ProposalDraftRequest(
            initialization_id="quest_init_1",
            draft_revision=3,
            draft_hash="a" * 64,
            draft={"goal": "研究稀有形态", "completion_criteria": "形成证据边界"},
        )
    )

    assert result.content == QUESTION
    assert result.adapter_kind == "codex_cli"
    argv, prompt, timeout = runner.calls[0]
    assert argv[:2] == ["codex", "exec"]
    assert "--output-schema" in argv
    assert "--sandbox" in argv and "read-only" in argv
    assert "quest_init_1" in prompt and "a" * 64 in prompt
    assert timeout > 0


def test_durable_drafting_recovers_signed_result_without_provider_replay(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "durable-drafting-response-loss"
    executable = _fake_drafting_codex(tmp_path / "fake-durable-codex")
    detached = DetachedDraftingSupervisorRunner()
    request = ProposalDraftRequest(
        initialization_id="quest_init_durable_response_loss",
        draft_revision=1,
        draft_hash="a" * 64,
        draft={"goal": "recover", "completion_criteria": "zero replay"},
        job_ref="proposal_generation_stable:proposal",
    )
    first = CodexDraftingAdapter(
        workspace,
        executable=str(executable),
        process_runner=detached,
    )

    with pytest.raises(DraftingUnavailable, match="codex_job_outcome_unknown"):
        first.draft(request)
    assert detached.process is not None
    assert detached.process.wait(timeout=10) == 0

    forbidden = ForbiddenDraftingReplayRunner()
    restarted = CodexDraftingAdapter(
        workspace,
        executable=str(executable),
        process_runner=forbidden,
    )
    recovered = restarted.draft(request)

    assert recovered.content == QUESTION
    assert forbidden.calls == 0


def test_codex_adapter_rejects_a_title_over_the_public_field_limit(
    tmp_path: Path,
) -> None:
    oversized = {**QUESTION, "title": "x" * 501}
    runner = RecordingRunner(oversized)
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    with pytest.raises(DraftingUnavailable, match="codex_proposal_invalid"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_oversized",
                draft_revision=1,
                draft_hash="f" * 64,
                draft={"goal": "x", "completion_criteria": "y"},
            )
        )

    properties = runner.schemas[0]["properties"]
    assert {
        field: definition["maxLength"]
        for field, definition in properties.items()
    } == QUESTION_FIELD_MAX_LENGTHS


def test_codex_intent_reply_resumes_the_native_session(tmp_path: Path) -> None:
    first = RecordingRunner({"reply": "先明确证据边界。"}, thread_id="native-1")
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=first)
    created = adapter.reply(
        IntentTurnRequest(
            initialization_id="quest_init_1",
            draft_revision=1,
            draft_hash="b" * 64,
            draft={"goal": "研究稀有形态", "resource_envelope_ref": "env-1"},
            message="怎样缩小问题？",
            native_session_ref=None,
        )
    )
    assert created.reply == "先明确证据边界。"
    assert created.native_session_ref == "native-1"
    assert "研究稀有形态" in first.calls[0][1]
    assert "env-1" in first.calls[0][1]

    second = RecordingRunner({"reply": "再限定样本条件。"}, thread_id="native-1")
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=second)
    resumed = adapter.reply(
        IntentTurnRequest(
            initialization_id="quest_init_1",
            draft_revision=2,
            draft_hash="c" * 64,
            draft={"goal": "限定样本条件"},
            message="继续",
            native_session_ref=created.native_session_ref,
        )
    )
    assert resumed.reply == "再限定样本条件。"
    assert "resume" in second.calls[0][0]
    assert "native-1" in second.calls[0][0]


def test_codex_intent_reply_names_the_manual_creation_boundary(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({"reply": "只调整六字段 Proposal，不改写 Seed。"})
    adapter = CodexDraftingAdapter(tmp_path / "manual-drafting", process_runner=runner)

    adapter.reply(
        IntentTurnRequest(
            initialization_id="quest_init_manual",
            draft_revision=2,
            draft_hash="d" * 64,
            draft={
                "creation_mode": "ManualCreation",
                "parent_question_ref": "question_parent",
                "confirmed_seed": {"intent": "用户精确原话"},
            },
            message="怎样收窄答案边界？",
            native_session_ref=None,
            creation_context_kind="manual_question_creation",
            creation_context_ref="manual_creation_1",
            context_generation=3,
        )
    )

    prompt = runner.calls[0][1]
    assert "后续 Question" in prompt
    assert "manual_creation_1" in prompt
    assert "context_generation=3" in prompt
    assert "已确认 Seed 不可改写" in prompt
    assert "创建 Quest 之前" not in prompt


def test_codex_intent_reply_schema_and_adapter_share_the_public_length_limit(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({"reply": "x" * (INTENT_REPLY_MAX_LENGTH + 1)})
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    with pytest.raises(DraftingUnavailable, match="codex_intent_reply_invalid"):
        adapter.reply(
            IntentTurnRequest(
                initialization_id="quest_init_oversized_intent",
                draft_revision=1,
                draft_hash="c" * 64,
                draft={"goal": "限定回复长度"},
                message="请回复",
                native_session_ref=None,
            )
        )

    assert runner.schemas[0]["properties"]["reply"]["maxLength"] == (
        INTENT_REPLY_MAX_LENGTH
    )


def test_codex_intent_adapter_enforces_the_exact_reply_schema(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner({"reply": "有效回复", "receipt": "forged"})
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    with pytest.raises(DraftingUnavailable, match="codex_intent_reply_invalid"):
        adapter.reply(
            IntentTurnRequest(
                initialization_id="quest_init_extra_reply_field",
                draft_revision=1,
                draft_hash="c" * 64,
                draft={"goal": "严格校验 reply schema"},
                message="请回复",
                native_session_ref=None,
            )
        )


def test_codex_companion_may_return_one_typed_non_authoritative_proposal(
    tmp_path: Path,
) -> None:
    proposal = {
        "proposal_kind": "alternative_route",
        "text": "Prefer OA sources while institutional access is blocked.",
        "applies_to": ["literature_acquisition"],
    }
    runner = RecordingRunner(
        {"reply": "The institution route is still blocked.", "agent_proposal": proposal},
        thread_id="companion-native-1",
    )
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    result = adapter.reply(
        IntentTurnRequest(
            initialization_id="human_request_context_1",
            draft_revision=0,
            draft_hash="d" * 64,
            draft={
                "interaction_kind": "conversation",
                "scope_ref": "human_request_context_1",
                "authoritative_effect": False,
                "current_context": {
                    "status": "open",
                    "obligation": "Reconnect the institution route.",
                },
            },
            message="What alternative is available?",
            native_session_ref=None,
        )
    )

    assert result.agent_proposal == proposal
    assert "Reconnect the institution route" in runner.calls[0][1]
    assert set(runner.schemas[0]["required"]) == {"reply", "agent_proposal"}


def test_codex_companion_can_propose_a_typed_command_without_authorizing_it(
    tmp_path: Path,
) -> None:
    command = {
        "command_kind": "capability_authorization",
        "payload": {
            "capability": "broad_research",
            "decision": "revoked",
            "scope": {
                "quest_ref": "quest_1",
                "destination": None,
                "asset_ref": None,
                "duration": None,
                "method": None,
                "exclusions": [],
            },
        },
    }
    proposal = {
        "proposal_kind": "command_draft",
        "text": "Prepare a revocation draft for explicit human review.",
        "applies_to": ["quest_authorization"],
        "command": command,
    }
    runner = RecordingRunner(
        {"reply": "I prepared a non-authoritative draft.", "agent_proposal": proposal},
        thread_id="companion-command-native-1",
    )
    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=runner)

    result = adapter.reply(
        IntentTurnRequest(
            initialization_id="quest:quest_1",
            draft_revision=0,
            draft_hash="e" * 64,
            draft={
                "interaction_kind": "conversation",
                "scope_ref": "quest:quest_1",
                "authoritative_effect": False,
                "current_context": {"quest_ref": "quest_1"},
            },
            message="Prepare revocation for review.",
            native_session_ref=None,
        )
    )

    assert result.agent_proposal == proposal
    assert result.agent_proposal["command"] == command


def test_codex_adapter_fails_typed_instead_of_fabricating_a_reply(
    tmp_path: Path,
) -> None:
    def unavailable(
        argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("codex")

    adapter = CodexDraftingAdapter(tmp_path / "drafting", process_runner=unavailable)
    with pytest.raises(DraftingUnavailable, match="codex_cli_unavailable"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_1",
                draft_revision=1,
                draft_hash="d" * 64,
                draft={"goal": "x", "completion_criteria": "y"},
            )
        )


def test_durable_drafting_reports_a_missing_provider_as_unavailable(
    tmp_path: Path,
) -> None:
    adapter = CodexDraftingAdapter(
        tmp_path / "durable-missing-provider",
        executable=str(tmp_path / "missing-codex"),
        timeout_seconds=1.0,
    )

    with pytest.raises(DraftingUnavailable, match="codex_cli_unavailable"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_durable_missing",
                draft_revision=1,
                draft_hash="d" * 64,
                draft={"goal": "x", "completion_criteria": "y"},
                job_ref="proposal_generation_durable_missing:proposal",
            )
        )


@pytest.mark.parametrize("target", ["result", "stdout"])
def test_codex_adapter_does_not_read_unbounded_provider_output(
    tmp_path: Path, target: str
) -> None:
    adapter = CodexDraftingAdapter(
        tmp_path / "bounded-provider-output",
        process_runner=OversizedProviderRunner(target),
    )

    with pytest.raises(DraftingUnavailable, match="codex_output_too_large"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id=f"quest_init_oversized_{target}",
                draft_revision=1,
                draft_hash="d" * 64,
                draft={"goal": "bounded", "completion_criteria": "output"},
            )
        )


def test_production_runner_streams_provider_output_through_a_bounded_pipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "verbose-codex"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path

output_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output_path.write_text({json.dumps(json.dumps(QUESTION, ensure_ascii=False))}, encoding="utf-8")
sys.stdout.write("x" * {PROVIDER_STREAM_MAX_BYTES + 1})
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    original_popen = subprocess.Popen
    stream_targets: list[tuple[object, object]] = []

    def recording_popen(*args, **kwargs):
        stream_targets.append((kwargs.get("stdout"), kwargs.get("stderr")))
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)
    adapter = CodexDraftingAdapter(
        tmp_path / "bounded-production-streams",
        executable=str(executable),
        timeout_seconds=2,
    )

    with pytest.raises(DraftingUnavailable, match="codex_output_too_large"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_verbose",
                draft_revision=1,
                draft_hash="e" * 64,
                draft={"goal": "bounded", "completion_criteria": "stream"},
            )
        )

    assert stream_targets
    assert stream_targets[0][0] is subprocess.PIPE
    assert stream_targets[0][1] is subprocess.DEVNULL


def test_production_runner_times_out_while_provider_does_not_read_maximum_input(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "non-reading-codex"
    executable.write_text(
        f"""#!{sys.executable}
import time

time.sleep(30)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexDraftingAdapter(
        tmp_path / "bounded-provider-input",
        executable=str(executable),
        timeout_seconds=0.1,
    )
    request = IntentTurnRequest(
        initialization_id="quest_init_maximum_input",
        draft_revision=1,
        draft_hash="e" * 64,
        draft={
            "goal": "界" * 4000,
            "completion_criteria": "准" * 4000,
            "time_budget": "open",
            "route": "direct",
            "resource_envelope_ref": "envelope-maximum",
            "resource_envelope_hash": "f" * 64,
            "literature": {
                "mode": "oa_only",
                "library_entry_url": "",
                "scope_exclusions": "限" * 8000,
                "accepted_material_bindings": [],
            },
            "background_and_initial_direction": "景" * 12000,
        },
        message="问" * INTENT_REPLY_MAX_LENGTH,
        native_session_ref=None,
    )
    outcomes: list[str] = []

    def invoke() -> None:
        try:
            adapter.reply(request)
        except DraftingUnavailable as error:
            outcomes.append(error.code)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        worker.join(timeout=0.8)
        assert not worker.is_alive(), "provider stdin write escaped its total timeout"
        assert outcomes == ["codex_cli_timeout"]
    finally:
        adapter.request_stop()
        worker.join(timeout=1)


def test_result_byte_limit_accepts_the_public_schema_worst_case_json(
    tmp_path: Path,
) -> None:
    maximum_question = {
        field: "😀" * maximum
        for field, maximum in QUESTION_FIELD_MAX_LENGTHS.items()
    }

    def escaped_json_runner(
        argv: list[str], input_text: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(maximum_question, ensure_ascii=True), encoding="utf-8"
        )
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    adapter = CodexDraftingAdapter(
        tmp_path / "worst-case-valid-result",
        process_runner=escaped_json_runner,
    )

    result = adapter.draft(
        ProposalDraftRequest(
            initialization_id="quest_init_worst_case_result",
            draft_revision=1,
            draft_hash="f" * 64,
            draft={"goal": "bounded", "completion_criteria": "valid"},
        )
    )

    assert result.content == maximum_question


def test_codex_adapter_stop_terminates_then_kills_an_inflight_process(
    tmp_path: Path,
) -> None:
    started_marker = tmp_path / "provider-started"
    terminated_marker = tmp_path / "provider-terminated"
    child_started_marker = tmp_path / "provider-child-started"
    child_terminated_marker = tmp_path / "provider-child-terminated"
    child_pid_marker = tmp_path / "provider-child-pid"
    executable = tmp_path / "blocking-codex"
    child_code = f"""
import os
import signal
import time
from pathlib import Path

terminated = Path({str(child_terminated_marker)!r})

def ignore_termination(_signum, _frame):
    terminated.write_text("terminate received", encoding="utf-8")

signal.signal(signal.SIGTERM, ignore_termination)
Path({str(child_pid_marker)!r}).write_text(str(os.getpid()), encoding="utf-8")
Path({str(child_started_marker)!r}).write_text("started", encoding="utf-8")
while True:
    time.sleep(0.05)
"""
    executable.write_text(
        f"""#!{sys.executable}
import signal
import subprocess
import sys
import time
from pathlib import Path

terminated = Path({str(terminated_marker)!r})

def ignore_termination(_signum, _frame):
    terminated.write_text("terminate received", encoding="utf-8")

signal.signal(signal.SIGTERM, ignore_termination)
subprocess.Popen([sys.executable, "-c", {child_code!r}])
Path({str(started_marker)!r}).write_text("started", encoding="utf-8")
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexDraftingAdapter(
        tmp_path / "drafting",
        executable=str(executable),
        timeout_seconds=0.8,
    )
    outcomes: list[str] = []

    def invoke() -> None:
        try:
            adapter.draft(
                ProposalDraftRequest(
                    initialization_id="quest_init_shutdown",
                    draft_revision=1,
                    draft_hash="e" * 64,
                    draft={"goal": "shutdown", "completion_criteria": "bounded"},
                )
            )
        except DraftingUnavailable as error:
            outcomes.append(error.code)

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    deadline = time.monotonic() + 1
    while (
        not started_marker.exists() or not child_started_marker.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started_marker.exists()
    assert child_started_marker.exists()

    started = time.monotonic()
    try:
        adapter.request_stop()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert time.monotonic() - started < 1
        assert terminated_marker.read_text(encoding="utf-8") == "terminate received"
        assert child_terminated_marker.read_text(encoding="utf-8") == (
            "terminate received"
        )
        assert outcomes == ["codex_cli_stopped"]
    finally:
        worker.join(timeout=1)
        if worker.is_alive() and child_pid_marker.exists():
            child_pid = int(child_pid_marker.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            worker.join(timeout=1)


def test_codex_adapter_can_cancel_active_work_without_disabling_the_next_call(
    tmp_path: Path,
) -> None:
    attempt_marker = tmp_path / "provider-attempts"
    started_marker = tmp_path / "provider-cancellable-started"
    executable = tmp_path / "cancellable-codex"
    executable.write_text(
        f"""#!{sys.executable}
import json
import signal
import sys
import time
from pathlib import Path

attempt_marker = Path({str(attempt_marker)!r})
attempt = int(attempt_marker.read_text(encoding="utf-8")) + 1 if attempt_marker.exists() else 1
attempt_marker.write_text(str(attempt), encoding="utf-8")
if attempt == 1:
    signal.signal(signal.SIGTERM, lambda _signum, _frame: None)
    Path({str(started_marker)!r}).write_text("started", encoding="utf-8")
    while True:
        time.sleep(0.05)
output_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output_path.write_text({json.dumps(json.dumps(QUESTION, ensure_ascii=False))}, encoding="utf-8")
print(json.dumps({{"type": "thread.started", "thread_id": "native-after-cancel"}}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexDraftingAdapter(
        tmp_path / "drafting",
        executable=str(executable),
        timeout_seconds=3,
    )
    outcomes: list[str] = []

    def invoke_first() -> None:
        try:
            adapter.draft(
                ProposalDraftRequest(
                    initialization_id="quest_init_cancel_active",
                    draft_revision=1,
                    draft_hash="a" * 64,
                    draft={"goal": "cancel", "completion_criteria": "bounded"},
                )
            )
        except DraftingUnavailable as error:
            outcomes.append(error.code)

    worker = threading.Thread(target=invoke_first, daemon=True)
    worker.start()
    deadline = time.monotonic() + 1
    while not started_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert started_marker.exists()

    try:
        adapter.cancel_active()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert outcomes == ["codex_cli_stopped"]

        result = adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_after_cancel",
                draft_revision=1,
                draft_hash="b" * 64,
                draft={"goal": "retry", "completion_criteria": "succeeds"},
            )
        )
        assert result.content == QUESTION
        assert attempt_marker.read_text(encoding="utf-8") == "2"
    finally:
        adapter.request_stop()
        worker.join(timeout=1)


def test_job_cancellation_fence_blocks_pre_spawn_without_stopping_other_jobs(
    tmp_path: Path,
) -> None:
    spawn_marker = tmp_path / "job-provider-spawned"
    executable = tmp_path / "job-scoped-codex"
    executable.write_text(
        f"""#!{sys.executable}
import json
import sys
from pathlib import Path

sys.stdin.buffer.read()
Path({str(spawn_marker)!r}).write_text("spawned", encoding="utf-8")
output_path = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
output_path.write_text({json.dumps(json.dumps(QUESTION, ensure_ascii=False))}, encoding="utf-8")
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexDraftingAdapter(
        tmp_path / "job-scoped-provider",
        executable=str(executable),
        timeout_seconds=2,
    )

    assert adapter.cancel_job("generation_cancelled")
    with pytest.raises(DraftingUnavailable, match="codex_cli_stopped"):
        adapter.draft(
            ProposalDraftRequest(
                initialization_id="quest_init_cancelled_job",
                draft_revision=1,
                draft_hash="a" * 64,
                draft={"goal": "cancelled", "completion_criteria": "no spawn"},
                job_ref="generation_cancelled",
            )
        )
    assert not spawn_marker.exists()
    adapter.finish_job("generation_cancelled")

    result = adapter.draft(
        ProposalDraftRequest(
            initialization_id="quest_init_next_job",
            draft_revision=1,
            draft_hash="b" * 64,
            draft={"goal": "next", "completion_criteria": "can spawn"},
            job_ref="generation_next",
        )
    )
    assert result.content == QUESTION
    assert spawn_marker.read_text(encoding="utf-8") == "spawned"


def test_nvidia_smi_probe_reports_real_uuid_identity_and_typed_unavailability() -> None:
    calls: list[tuple[list[str], float]] = []

    def ready(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                "GPU-aaa, NVIDIA A100-SXM4-80GB, 81920\n"
                "GPU-bbb, NVIDIA A100-SXM4-80GB, 81920\n"
            ),
            stderr="",
        )

    snapshot = NvidiaSmiProbe(command_runner=ready).observe()
    assert snapshot.status == "ready"
    assert [device.uuid for device in snapshot.devices] == ["GPU-aaa", "GPU-bbb"]
    assert snapshot.devices[0].memory_total_mib == 81920
    assert calls[0][0] == [
        "nvidia-smi",
        "--query-gpu=uuid,name,memory.total",
        "--format=csv,noheader,nounits",
    ]

    def missing(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("nvidia-smi")

    unavailable = NvidiaSmiProbe(command_runner=missing).observe()
    assert unavailable.status == "unavailable"
    assert unavailable.reason_code == "nvidia_smi_unavailable"
    assert unavailable.devices == ()


def test_production_composition_installs_real_drafting_and_compute_adapters(
    tmp_path: Path,
) -> None:
    runtime = build_production_runtime(prepare_data_root(tmp_path / "production-seams"))
    try:
        hc = runtime.owners.human_collaboration
        assert isinstance(hc._proposal_drafter, CodexDraftingAdapter)
        assert isinstance(hc._intent_drafting_provider, CodexDraftingAdapter)
        assert isinstance(
            runtime.owners.agent_runtime._host_compute_probe, NvidiaSmiProbe
        )
    finally:
        runtime.close()


def test_provider_unavailability_is_durable_and_advances_the_public_feed(
    tmp_path: Path,
) -> None:
    class UnavailableAdapter:
        def draft(self, request: ProposalDraftRequest):
            raise DraftingUnavailable("codex_cli_unavailable")

        def reply(self, request: IntentTurnRequest):
            raise DraftingUnavailable("codex_cli_unavailable")

    adapter = UnavailableAdapter()
    root = prepare_data_root(tmp_path / "provider-unavailable")
    runtime = build_production_runtime(
        root,
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
    )
    hc = runtime.owners.human_collaboration
    try:
        created = hc.create_quest(
            {
                "goal": "验证真实 provider 不可用时 fail closed。",
                "completion_criteria": "不生成任何伪 Proposal。",
                "key_configuration": "test-only adapter injection",
                "literature_scope": "open_access",
                "initial_question_direction": "不可用状态是否 durable？",
                "material_receipts": [],
            },
            "create-unavailable",
        )
        hc.generate_question_proposal(
            created["initialization_id"],
            created["quest_draft"]["hash"],
            "generate-unavailable",
        )
        hc.send_intent_message(
            created["initialization_id"],
            expected_draft_revision=created["quest_draft"]["revision"],
            expected_draft_hash=created["quest_draft"]["hash"],
            message="请解释当前配置。",
            idempotency_key="intent-unavailable",
        )
        before_failure = runtime.feed.current_revision()
        assert hc.process_drafting_once()
        failed = hc.query_quest_creation(created["initialization_id"])
        assert failed["proposal"] is None
        assert failed["proposal_generation"]["status"] == (
            "capability_unavailable"
        )
        assert failed["proposal_generation"]["failure"] == {
            "code": "codex_cli_unavailable"
        }
        assert runtime.feed.current_revision() > before_failure
        before_intent_failure = runtime.feed.current_revision()
        assert hc.process_drafting_once()
        failed = hc.query_quest_creation(created["initialization_id"])
        assert failed["intent_session"]["turns"][0]["assistant_status"] == (
            "unavailable"
        )
        assert failed["intent_session"]["turns"][0]["reason"] == {
            "code": "codex_cli_unavailable"
        }
        assert runtime.feed.current_revision() > before_intent_failure
    finally:
        runtime.close()

    restarted = build_production_runtime(
        prepare_data_root(tmp_path / "provider-unavailable"),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
    )
    try:
        restored = restarted.owners.human_collaboration.query_quest_creation(
            created["initialization_id"]
        )
        assert restored["proposal_generation"]["status"] == (
            "capability_unavailable"
        )
        assert restored["proposal"] is None
        assert restored["intent_session"]["turns"][0]["assistant_status"] == (
            "unavailable"
        )
    finally:
        restarted.close()
