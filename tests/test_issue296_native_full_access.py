"""Exercise explicitly configured native full-access recovery without a model."""

import json
from pathlib import Path
import sys

import pytest

from meta_research.paths import prepare_data_root
from test_issue296_stable_workspace import _SignedCeilingRunner, _exercise_two_recoveries
from test_native_session_cleanup import _server, _vendor


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux full-access acceptance")
def test_native_full_access_runs_in_assigned_workspace(tmp_path: Path):
    executable = _vendor()
    root = prepare_data_root(tmp_path / "issue296-native")
    workspace = root.root / "target-a"
    parallel = root.root / "target-b"
    workspace.mkdir()
    parallel.mkdir()
    original = parallel / "formal-original.csv"
    original.write_bytes(b"sample,value\nother,296\n")
    _run_in_workspace(executable, root, workspace, original)
    assert (workspace / "work.csv").read_bytes() == b"sample,value\nother,296\nnew,297\n"
    assert original.read_bytes() == b"sample,value\nother,296\n"


def _run_in_workspace(executable, root, workspace, original):
    program = """
import json, pathlib, sys
original = pathlib.Path(sys.argv[1])
assert original.read_bytes() == b'sample,value\\nother,296\\n'
pathlib.Path('work.csv').write_bytes(original.read_bytes() + b'new,297\\n')
checkpoint = pathlib.Path('native-checkpoint.json')
segments = json.loads(checkpoint.read_text())['segments'] if checkpoint.exists() else 0
checkpoint.write_text(json.dumps({'segments': segments + 1}))
print(json.dumps({'cwd': str(pathlib.Path.cwd()), 'segments': segments + 1}))
"""
    with _server(executable, root) as call:
        result = call("command/exec", {
            "command": [sys.executable, "-c", program, str(original)],
            "cwd": str(workspace),
            "sandboxPolicy": {"type": "dangerFullAccess"},
            "timeoutMs": 10000,
        })
    assert result["exitCode"] == 0, result
    observed = json.loads(result["stdout"])
    assert observed["cwd"] == str(workspace)
    assert original.read_bytes() == b"sample,value\nother,296\n"
    return observed


class _NativeFullAccessCeilingRunner(_SignedCeilingRunner):
    def __init__(self, executable):
        super().__init__(expected_sandbox="danger-full-access")
        self.executable = executable
        self.native_observations = []

    def __call__(self, argv, prompt, timeout, environment):
        if "--sandbox" in argv:
            assert argv[argv.index("--sandbox") + 1] == self.expected_sandbox
            root = self.runtime.data_root
            original = root.root / "parallel-native-original.csv"
            if not original.exists():
                original.write_bytes(b"sample,value\nother,296\n")
            workspace = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"])
            self.native_observations.append(_run_in_workspace(self.executable, root, workspace, original))
        return super().__call__(argv, prompt, timeout, environment)


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux full-access acceptance")
def test_two_recoveries_use_native_full_access_at_the_same_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("META_RESEARCH_TARGET_SANDBOX_MODE", "danger-full-access")
    runner = _NativeFullAccessCeilingRunner(_vendor())
    _exercise_two_recoveries(tmp_path, runner, False, run_after_second_recovery=True)
    assert [item["segments"] for item in runner.native_observations] == [1, 2, 3]
    assert len({item["cwd"] for item in runner.native_observations}) == 1
    assert all(call[0][call[0].index("--sandbox") + 1] == "danger-full-access" for call in runner.calls)
    print("NATIVE_FULL_ACCESS " + json.dumps(runner.native_observations, sort_keys=True))
