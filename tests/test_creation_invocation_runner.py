from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from meta_research.companion import CodexCompanionAdapter
from meta_research.creation_work import ProtectedCreationRunner
from meta_research.idea_skill import IdeaSkillUnavailable
from meta_research.owners.common import canonical_hash
from meta_research.protected_creation_runtime import ProtectedCreationError
from meta_research.provider_supervisor import read_transport_envelope
from test_idea_skill_contract import _SequenceRunner, _PrelaunchLossRunner


SCHEMA = {"type": "object", "properties": {"reply": {"type": "string"}},
          "required": ["reply"], "additionalProperties": False}


def test_concurrent_calls_keep_local_runner_and_signed_execution_binding(tmp_path):
    host = _PrelaunchLossRunner()
    adapter = CodexCompanionAdapter(tmp_path, process_runner=host)
    barrier = threading.Barrier(2)

    class Runner(_SequenceRunner):
        runtime_conditions = "Private standard-library runtime only."

        def run_job(self, job_ref, argv, prompt, timeout):
            barrier.wait(timeout=10)
            return super().run_job(job_ref, argv, prompt, timeout)

    runners = [Runner([{"reply": name}], thread_ids=[name]) for name in ("parent-a", "parent-b")]

    def invoke(index, binding=None):
        return adapter._invoke(operation_name="companion-turn", prompt="Read this operation only.",
            schema=SCHEMA, native_session_ref=None, job_ref="job-" + str(index),
            invocation_runner=runners[index], provider_execution_binding=binding or {"work_ref": "work-" + str(index)})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, (0, 1)))
    assert [result[0]["reply"] for result in results] == ["parent-a", "parent-b"]
    assert adapter._runner is host
    for index, runner in enumerate(runners):
        directory = tmp_path / "provider-operations" / canonical_hash({"job_ref": "job-" + str(index)}) / "companion-turn"
        _, key = adapter._transport_key()
        invocation = read_transport_envelope(directory / "invocation.json", key)
        assert invocation["provider_execution_binding"] == {"work_ref": "work-" + str(index)}
        assert invocation["transport_mode"] == "unreconciled_runner"
        assert len(runner.calls) == 1
        assert "Private standard-library runtime only." in runner.calls[0][1]
        assert invoke(index)[:2] == results[index][:2]
        assert len(runner.calls) == 1
        assert not adapter.reconcile_cancelled_job("job-" + str(index))
    with pytest.raises(IdeaSkillUnavailable, match="codex_operation_identity_conflict"):
        invoke(0, {"work_ref": "different-work"})


def test_protected_unknown_outcome_preserves_exact_slots_and_has_no_host_fallback(tmp_path):
    captured = []

    def run(call):
        captured.append(call)
        raise ProtectedCreationError("protected_creation_unknown_outcome")

    work = SimpleNamespace(operation=SimpleNamespace(operation_ref="unknown-job"), run=run,
        request_stop=lambda: {"status": "unknown_outcome", "descendants_ended": False})
    context = tmp_path / "context.json"
    context.write_text("{}")
    runner = ProtectedCreationRunner(work, read_only_inputs=(context,))
    adapter = CodexCompanionAdapter(tmp_path / "adapter", process_runner=_PrelaunchLossRunner())
    with pytest.raises(IdeaSkillUnavailable, match="protected_creation_unknown_outcome"):
        adapter._invoke(operation_name="companion-turn", prompt="bounded input", schema=SCHEMA,
            native_session_ref=None, job_ref="unknown-job", invocation_runner=runner,
            provider_execution_binding={"work_ref": "unknown-work"})
    assert len(captured) == 1
    call = captured[0]
    assert call.read_only_inputs[0] == context
    assert call.read_only_inputs[1].name == "output-schema.json"
    assert call.output_paths == (Path(call.argv[call.argv.index("--output-last-message") + 1]),)
    assert runner.cancel_job("unknown-job")["descendants_ended"] is False
    assert runner.cancel_job("another-job")["descendants_ended"] is False
    assert "third-party Python scientific packages" in runner.runtime_conditions
