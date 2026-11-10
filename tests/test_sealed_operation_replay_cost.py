from collections import Counter
from pathlib import Path
import sys

from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.owners.common import canonical_hash
from test_runtime_conditions_provider_prompts import _SignedRunner


def test_signed_operation_replay_retains_provider_and_file_read_cost(tmp_path, monkeypatch):
    runner = _SignedRunner()
    adapter = CodexIdeaSkillAdapter(tmp_path, executable=sys.executable, process_runner=runner)
    arguments = dict(operation_name="primary", prompt="Read the exact frozen source.",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"], "additionalProperties": False},
        native_session_ref=None, job_ref="replay-cost", run_ref=None,
        attempt_ref=None, root_session_ref="session-one", fence_ref=None)
    expected = adapter._invoke_root_operation(**arguments)
    directory = tmp_path / "provider-operations" / canonical_hash({"job_ref": "replay-cost"}) / "primary"
    counts = Counter()
    original_open = Path.open
    def observed_open(path, mode="r", *args, **kwargs):
        if path.parent == directory:
            counts[(path.name, mode)] += 1
        return original_open(path, mode, *args, **kwargs)
    monkeypatch.setattr(Path, "open", observed_open)
    restarted = CodexIdeaSkillAdapter(tmp_path, executable=sys.executable, process_runner=runner)
    assert restarted._invoke_root_operation(**arguments) == expected
    assert len(runner.calls) == 1
    print({"physical_provider_calls_total": len(runner.calls),
           "operation_file_opens": {name + ":" + mode: count for (name, mode), count in sorted(counts.items())}})
