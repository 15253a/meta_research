"""Exercise the pinned native shell sandbox without a model or credentials."""

import json
from pathlib import Path
import sys

import pytest

from meta_research.paths import prepare_data_root
from test_issue296_stable_workspace import _SignedCeilingRunner, _exercise_two_recoveries
from test_native_session_cleanup import _server, _vendor


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux workspace-write acceptance")
def test_native_workspace_write_keeps_parallel_workspace_read_only(tmp_path: Path):
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
import errno, json, pathlib, sys
original = pathlib.Path(sys.argv[1])
assert original.read_bytes() == b'sample,value\\nother,296\\n'
pathlib.Path('work.csv').write_bytes(original.read_bytes() + b'new,297\\n')
denied = False
try:
    original.write_bytes(b'forbidden')
except OSError as error:
    denied = error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
assert denied, 'workspace-write allowed mutation of the parallel Target workspace'
checkpoint = pathlib.Path('native-checkpoint.json')
segments = json.loads(checkpoint.read_text())['segments'] if checkpoint.exists() else 0
checkpoint.write_text(json.dumps({'segments': segments + 1}))
print(json.dumps({'cwd': str(pathlib.Path.cwd()), 'parallel_write_denied': denied, 'segments': segments + 1}))
"""
    with _server(executable, root) as call:
        result = call("command/exec", {
            "command": [sys.executable, "-c", program, str(original)],
            "cwd": str(workspace),
            "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [], "networkAccess": False},
            "timeoutMs": 10000,
        })
    assert result["exitCode"] == 0, result
    observed = json.loads(result["stdout"])
    assert observed["cwd"] == str(workspace)
    assert observed["parallel_write_denied"] is True
    assert original.read_bytes() == b"sample,value\nother,296\n"
    return observed


class _NativeSandboxCeilingRunner(_SignedCeilingRunner):
    def __init__(self, executable):
        super().__init__()
        self.executable = executable
        self.sandbox_observations = []

    def __call__(self, argv, prompt, timeout, environment):
        if "--sandbox" in argv:
            assert argv[argv.index("--sandbox") + 1] == "workspace-write"
            root = self.runtime.data_root
            original = root.root / "parallel-native-original.csv"
            if not original.exists():
                original.write_bytes(b"sample,value\nother,296\n")
            workspace = Path(environment["META_RESEARCH_HARNESS_WORKSPACE"])
            self.sandbox_observations.append(_run_in_workspace(self.executable, root, workspace, original))
        return super().__call__(argv, prompt, timeout, environment)


@pytest.mark.skipif(sys.platform != "linux", reason="Native Linux workspace-write acceptance")
def test_two_recoveries_use_native_workspace_write_at_the_same_cwd(tmp_path: Path):
    runner = _NativeSandboxCeilingRunner(_vendor())
    _exercise_two_recoveries(tmp_path, runner, False)
    assert [item["segments"] for item in runner.sandbox_observations] == [1, 2]
    assert len({item["cwd"] for item in runner.sandbox_observations}) == 1
