"""A sealed Harness outcome remains readable after switching release envs."""

import json
import subprocess
from pathlib import Path

import pytest

from meta_research.harness_adapters import HarnessSupervisorTransport
from meta_research.provider_supervisor import (
    SUPERVISOR_EXIT_SCHEMA_V2,
    read_supervisor_request,
    read_transport_key_for_operation,
    write_exit_receipt,
    write_supervisor_request,
)
from meta_research.quest_drafting import _CancellableProcessRunner


STDOUT = '{"type":"thread.started","thread_id":"retained-native-session"}\n'


class _StoppedProvider(_CancellableProcessRunner):
    """Replace only process execution; use the real request and receipt seals."""

    stdout = STDOUT
    stdout_bytes: bytes | None = None
    termination_reason = "stopped"
    returncode = -15

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.request_path: Path | None = None

    def run_durable_job(self, *args, **kwargs):
        self.calls += 1
        assert self.calls == 1, "A terminal provider operation must not relaunch"
        self.request_path = args[6]
        directory = self.request_path.parent
        _, key = read_transport_key_for_operation(directory)
        request = read_supervisor_request(self.request_path, key)
        stdout_path = Path(request["stdout_path"])
        stdout_path.write_bytes(
            self.stdout.encode("utf-8") if self.stdout_bytes is None else self.stdout_bytes
        )
        prompt_path = Path(request["prompt_path"])
        write_exit_receipt(
            Path(request["receipt_path"]),
            key=key,
            invocation_hash=request["invocation_hash"],
            prompt_path=prompt_path,
            schema_path=Path(request["schema_path"]),
            stdout_path=stdout_path,
            result_path=Path(request["result_path"]),
            returncode=self.returncode,
            input_bytes=prompt_path.stat().st_size,
            termination_reason=self.termination_reason,
            schema_ref=SUPERVISOR_EXIT_SCHEMA_V2,
        )
        return subprocess.CompletedProcess(args[1], self.returncode, self.stdout, "")


def _call(transport):
    return transport(
        ["codex", "exec", "--json", "-"],
        "Retain the exact Target input and execution identity.",
        None,
        {
            "META_RESEARCH_HARNESS_FAMILY": "codex",
            "META_RESEARCH_PROVIDER_OPERATION_REF": "target-run:env-switch:harness_turn:1",
        },
    )


def test_signed_stopped_outcome_replays_in_new_env_without_rewriting_request(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "meta_research.harness_adapters.sys.executable", "/releases/old/bin/python"
    )
    runner = _StoppedProvider()
    first = _call(HarnessSupervisorTransport(tmp_path, process_runner=runner))
    request_path = runner.request_path
    assert request_path is not None
    receipt_path = request_path.with_name("supervisor-exit.json")
    request_before = request_path.read_bytes()
    receipt_before = receipt_path.read_bytes()
    _, key = read_transport_key_for_operation(request_path.parent)
    assert read_supervisor_request(request_path, key)["argv"][0] == (
        "/releases/old/bin/python"
    )

    monkeypatch.setattr(
        "meta_research.harness_adapters.sys.executable", "/releases/new/bin/python"
    )
    replay = _call(HarnessSupervisorTransport(tmp_path, process_runner=runner))

    assert replay.returncode == first.returncode == 143
    assert replay.stdout == first.stdout == STDOUT
    assert replay.meta_research_transport_receipt == first.meta_research_transport_receipt
    assert replay.meta_research_transport_receipt["provider_returncode"] == -15
    assert runner.calls == 1
    assert request_path.read_bytes() == request_before
    assert receipt_path.read_bytes() == receipt_before


@pytest.fixture
def stopped_operation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "meta_research.harness_adapters.sys.executable", "/releases/old/bin/python"
    )
    runner = _StoppedProvider()
    _call(HarnessSupervisorTransport(tmp_path, process_runner=runner))
    assert runner.request_path is not None
    monkeypatch.setattr(
        "meta_research.harness_adapters.sys.executable", "/releases/new/bin/python"
    )
    return HarnessSupervisorTransport(tmp_path, process_runner=runner), runner


@pytest.mark.parametrize(
    "damage",
    (
        "unsigned_request",
        "invalid_seal",
        "bridge_arguments",
        "invocation_hash",
        "prompt_path",
        "unexpected_field",
        "missing_request",
    ),
)
def test_terminal_replay_rejects_untrusted_or_different_request(
    stopped_operation, damage
):
    transport, runner = stopped_operation
    request_path = runner.request_path
    _, key = read_transport_key_for_operation(request_path.parent)
    request = read_supervisor_request(request_path, key)
    if damage == "unsigned_request":
        request_path.write_text(json.dumps(request), encoding="utf-8")
    elif damage == "invalid_seal":
        envelope = json.loads(request_path.read_text(encoding="utf-8"))
        envelope["payload"]["argv"][0] = "/unsigned/other/python"
        request_path.write_text(json.dumps(envelope), encoding="utf-8")
    elif damage == "missing_request":
        request_path.unlink()
    else:
        if damage == "bridge_arguments":
            request["argv"][2] = "other.bridge"
        elif damage == "invocation_hash":
            request["invocation_hash"] = "f" * 64
        elif damage == "prompt_path":
            request["prompt_path"] = str(request_path.with_name("other-prompt.txt"))
        else:
            request["unexpected_field"] = True
        request_path.unlink()
        write_supervisor_request(request_path, request, key)

    before = request_path.read_bytes() if request_path.exists() else None
    with pytest.raises(OSError, match="supervisor request unavailable"):
        _call(transport)
    assert runner.calls == 1
    assert (request_path.read_bytes() if request_path.exists() else None) == before


@pytest.mark.parametrize(
    "filename",
    (
        "prompt.txt",
        "provider-argv.json",
        "output-schema.json",
        "stdout.jsonl",
        "last-message.json",
        "supervisor-exit.json",
    ),
)
def test_terminal_replay_keeps_input_output_and_receipt_integrity(
    stopped_operation, filename
):
    transport, runner = stopped_operation
    request_path = runner.request_path
    before = request_path.read_bytes()
    request_path.with_name(filename).write_text("untrusted replacement", encoding="utf-8")

    with pytest.raises(OSError):
        _call(transport)
    assert runner.calls == 1
    assert request_path.read_bytes() == before


def test_missing_terminal_receipt_keeps_new_launch_request_strict(stopped_operation):
    transport, runner = stopped_operation
    request_path = runner.request_path
    request_path.with_name("supervisor-exit.json").unlink()
    before = request_path.read_bytes()

    with pytest.raises(OSError, match="supervisor request unavailable"):
        _call(transport)
    assert runner.calls == 1
    assert request_path.read_bytes() == before
    assert not request_path.with_name("supervisor-exit.json").exists()


def test_disappearing_terminal_receipt_cannot_fall_back_to_launch(
    stopped_operation, monkeypatch
):
    transport, runner = stopped_operation
    request_path = runner.request_path

    def read_then_remove_receipt(path, key):
        request = read_supervisor_request(path, key)
        path.with_name("supervisor-exit.json").unlink()
        return request

    monkeypatch.setattr(
        "meta_research.harness_adapters.read_supervisor_request", read_then_remove_receipt
    )
    with pytest.raises(OSError, match="provider supervisor receipt invalid"):
        _call(transport)
    assert runner.calls == 1


@pytest.mark.parametrize("damage", (
    "completed", "interior_byte", "invalid_start_byte", "invalid_seal",
))
def test_partial_utf8_tolerance_requires_signed_interrupted_eof(tmp_path, damage):
    runner = _StoppedProvider()
    partial_tail = b'{"type":"item.started","text":"' + "研".encode("utf-8")[:2]
    runner.stdout_bytes = STDOUT.encode("utf-8") + partial_tail
    if damage == "completed":
        runner.termination_reason = "completed"
        runner.returncode = 0
    elif damage == "interior_byte":
        runner.stdout_bytes = STDOUT.encode("utf-8") + b'\xff' + partial_tail
    elif damage == "invalid_start_byte":
        runner.stdout_bytes = STDOUT.encode("utf-8") + b'\xff'

    transport = HarnessSupervisorTransport(tmp_path, process_runner=runner)
    if damage == "invalid_seal":
        _call(transport)
        receipt_path = runner.request_path.with_name("supervisor-exit.json")
        envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
        envelope["payload"]["returncode"] = -9
        receipt_path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(OSError, match="provider supervisor receipt invalid"):
        _call(transport)
    assert runner.calls == 1
    assert runner.request_path.with_name("stdout.jsonl").read_bytes() == runner.stdout_bytes
