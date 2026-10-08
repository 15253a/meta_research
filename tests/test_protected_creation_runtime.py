from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

import meta_research.protected_creation_runtime as runtime_module
from meta_research.protected_creation_runtime import (
    NativeCreationCall,
    ProtectedCreationError,
    ProtectedCreationRuntime,
)


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    if sys.platform != "linux" or os.geteuid() != 0:
        pytest.skip("The enforced runtime requires Linux and root for chroot.")
    root = tmp_path_factory.mktemp("protected-creation")
    credentials = root / "credentials"
    credentials.mkdir()
    (credentials / "auth.json").write_text('{"test":"private-copy"}', encoding="utf-8")
    (credentials / "config.toml").write_text('model_provider = "private-test"\n', encoding="utf-8")
    result = ProtectedCreationRuntime(root / "jail", Path("/bin/bash"), credentials)
    completed = result.run(_call("print('prepared')"))
    assert completed.stdout == "prepared\n"
    yield result
    assert result.request_stop()["descendants_ended"] is True


def _call(script: str, *, arguments=(), inputs=(), outputs=(), environment=None, timeout=15.0):
    return NativeCreationCall(
        argv=("/bin/bash", "-c", 'exec /bin/python - "$@"', "creation", *map(str, arguments)),
        prompt=script,
        timeout_seconds=timeout,
        environment=environment or {},
        read_only_inputs=tuple(inputs),
        output_paths=tuple(outputs),
    )


def test_unsupported_platform_never_launches(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_module.sys, "platform", "win32")
    protected = ProtectedCreationRuntime(tmp_path / "jail", tmp_path / "native", tmp_path / "auth")
    with pytest.raises(ProtectedCreationError, match="protected_creation_runtime_unavailable"):
        protected.run(_call("raise AssertionError('must not run')"))
    assert not protected.jail_root.exists()


def test_native_boundary_inputs_and_exact_outputs(runtime, tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text('{"trusted":"schema"}', encoding="utf-8")
    original = tmp_path / "original.py"
    original.write_text("x = 3\n", encoding="utf-8")
    large = tmp_path / "large-unread.bin"
    with large.open("wb") as stream:
        stream.truncate(4 * 1024**3)
    destination = tmp_path / "selected.json"
    script = '''
import ctypes, json, os, pathlib, sys
source, output, original = map(pathlib.Path, sys.argv[1:])
denied = []
for path in (source, original):
    try:
        path.write_text("modified")
    except OSError:
        denied.append(str(path))
link = pathlib.Path("/workspace/original-link")
link.symlink_to(original)
try:
    link.write_text("modified")
except OSError:
    denied.append("original-link")
pathlib.Path("/workspace/unselected.txt").write_text("trial only")
pathlib.Path.home().joinpath("implicit.txt").write_text("private home")
pathlib.Path(os.environ["XDG_CACHE_HOME"], "implicit.txt").write_text("private cache")
output.write_text(json.dumps({"input":json.loads(source.read_text()), "uid":os.getuid(),
    "gid":os.getgid(), "home":str(pathlib.Path.home()), "codex_home":os.environ["CODEX_HOME"],
    "no_new_privs":ctypes.CDLL(None).prctl(39, 0, 0, 0, 0),
    "exe":str(pathlib.Path("/proc/self/exe").readlink()), "denied":len(denied)}))
print("native complete")
'''
    completed = runtime.run(_call(script, arguments=(schema, destination, original),
        inputs=(schema,), outputs=(destination,),
        environment={"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path)}))
    assert completed.returncode == 0
    assert completed.stdout == "native complete\n"
    assert json.loads(destination.read_text()) == {
        "input": {"trusted": "schema"}, "uid": 65534, "gid": 65534,
        "home": "/home/creation", "codex_home": "/home/creation/.codex",
        "no_new_privs": 1, "exe": "/bin/codex", "denied": 3,
    }
    assert original.read_text() == "x = 3\n"
    assert schema.read_text() == '{"trusted":"schema"}'
    assert large.stat().st_size == 4 * 1024**3
    assert large.stat().st_blocks == 0
    assert not (tmp_path / "unselected.txt").exists()
    assert (runtime.work_directory / "unselected.txt").read_text() == "trial only"


def test_private_native_state_survives_resume_and_auth_stays_private(runtime):
    first = runtime.run(_call('''
from pathlib import Path
home = Path.home() / ".codex"
home.joinpath("session-test").write_text("native session")
home.joinpath("auth.json").write_text("private change")
'''))
    assert first.returncode == 0
    second = runtime.run(_call('''
from pathlib import Path
print(Path.home().joinpath(".codex/session-test").read_text())
print(Path.home().joinpath(".codex/auth.json").read_text())
print(Path.home().joinpath(".codex/config.toml").read_text(), end="")
'''))
    assert second.stdout == 'native session\nprivate change\nmodel_provider = "private-test"\n'
    assert (runtime.credentials_home / "auth.json").read_text() == '{"test":"private-copy"}'


def test_native_failure_preserves_actual_status_and_stdout(runtime):
    completed = runtime.run(_call("print('failed program'); raise SystemExit(7)"))
    assert completed.returncode == 7
    assert completed.stdout == "failed program\n"
    assert runtime.request_stop()["descendants_ended"] is True


def test_explicit_node_child_uses_private_runtime_and_reports_exe_alias(runtime):
    completed = runtime.run(_call('''
import subprocess
result = subprocess.run(["/bin/node", "-e", 'const c=require("child_process").spawnSync("/bin/node",["-e","console.log(42)"],{encoding:"utf8"});console.log(JSON.stringify({status:c.status,stdout:c.stdout,exe:process.execPath}));'], capture_output=True, text=True)
print(result.stdout, end="")
raise SystemExit(result.returncode)
'''))
    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"status": 0, "stdout": "42\n", "exe": "/bin/codex"}


@pytest.mark.parametrize("attack", ("input_symlink", "input_parent_symlink", "input_hardlink"))
def test_projection_rejects_links(runtime, tmp_path, attack):
    original = tmp_path / "trusted.json"
    original.write_text("{}", encoding="utf-8")
    supplied = tmp_path / "supplied.json"
    if attack == "input_symlink":
        supplied.symlink_to(original)
    elif attack == "input_parent_symlink":
        directory = tmp_path / "linked"
        directory.symlink_to(tmp_path, target_is_directory=True)
        supplied = directory / original.name
    else:
        os.link(original, supplied)
    with pytest.raises((OSError, ProtectedCreationError)):
        runtime.run(_call("print('must not launch')", inputs=(supplied,)))
    assert original.read_text() == "{}"


def test_projection_limit_is_applied_without_copying_tree(runtime, tmp_path):
    large = tmp_path / "oversized.json"
    with large.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024 + 1)
    with pytest.raises(ProtectedCreationError, match="protected_creation_input_too_large"):
        runtime.run(_call("print('must not launch')", inputs=(large,)))
    with pytest.raises(ProtectedCreationError, match="protected_creation_file_invalid"):
        runtime.run(_call("print('must not launch')", inputs=(tmp_path,)))


@pytest.mark.parametrize("attack", ("native_output_symlink", "native_output_hardlink", "destination_symlink", "destination_parent_symlink"))
def test_exact_output_slots_reject_symlink_traversal(runtime, tmp_path, attack):
    original = tmp_path / "original.txt"
    original.write_text("original", encoding="utf-8")
    destination = tmp_path / "selected.txt"
    if attack == "destination_symlink":
        destination.symlink_to(original)
    elif attack == "destination_parent_symlink":
        directory = tmp_path / "linked"
        directory.symlink_to(tmp_path, target_is_directory=True)
        destination = directory / "selected.txt"
    script = 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text("selected")'
    if attack == "native_output_symlink":
        script = 'from pathlib import Path; import sys; Path(sys.argv[1]).symlink_to(sys.argv[2])'
    elif attack == "native_output_hardlink":
        script = 'import os, sys; os.link("/home/creation/.codex/auth.json", sys.argv[1])'
    with pytest.raises(ProtectedCreationError, match="protected_creation_output_invalid"):
        runtime.run(_call(script, arguments=(destination, original), outputs=(destination,)))
    assert original.read_text() == "original"


def test_missing_output_does_not_republish_prior_output(runtime, tmp_path):
    destination = tmp_path / "selected.txt"
    first = runtime.run(_call('from pathlib import Path; import sys; Path(sys.argv[1]).write_text("first")',
        arguments=(destination,), outputs=(destination,)))
    assert first.returncode == 0
    destination.write_text("caller retained", encoding="utf-8")
    second = runtime.run(_call("print('no selected output')", arguments=(destination,), outputs=(destination,)))
    assert second.returncode == 0
    assert destination.read_text() == "caller retained"


def test_sparse_oversized_native_result_never_copies_to_host(runtime, tmp_path):
    destination = tmp_path / "selected.json"
    destination.write_text("retained", encoding="utf-8")
    script = 'import sys; output = open(sys.argv[1], "wb"); output.truncate(4 * 1024**3); output.close()'
    with pytest.raises(ProtectedCreationError, match="protected_creation_output_invalid"):
        runtime.run(_call(script, arguments=(destination,), outputs=(destination,)))
    assert destination.read_text() == "retained"


def test_leader_exit_reaps_setsid_grandchild(runtime):
    marker = runtime.work_directory / "escaped-grandchild.pid"
    completed = runtime.run(_call('''
import os, pathlib, time
child = os.fork()
if child == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild:
        os._exit(0)
    pathlib.Path("/workspace/escaped-grandchild.pid").write_text(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
while not pathlib.Path("/workspace/escaped-grandchild.pid").exists():
    time.sleep(0.01)
os._exit(0)
'''))
    assert completed.returncode == 0
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert runtime.request_stop()["descendants_ended"] is True


def test_stop_reaps_child_that_escaped_original_group(runtime):
    marker = runtime.work_directory / "cancel-grandchild.pid"
    result = []
    failures = []

    def execute():
        try:
            result.append(runtime.run(_call('''
import os, pathlib, time
child = os.fork()
if child == 0:
    os.setsid()
    grandchild = os.fork()
    if grandchild:
        os._exit(0)
    pathlib.Path("/workspace/cancel-grandchild.pid").write_text(str(os.getpid()))
    time.sleep(60)
    os._exit(0)
time.sleep(60)
''')))
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=execute)
    worker.start()
    deadline = time.monotonic() + 15
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    outcome = runtime.request_stop()
    worker.join(timeout=15)
    assert not worker.is_alive()
    assert not failures
    assert outcome == {"status": "stopped", "descendants_ended": True}
    assert result[0].returncode == -9
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_timeout_seals_native_tree_and_returns_actual_stdout(runtime):
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        runtime.run(_call("import time; print('started', flush=True); time.sleep(60)", timeout=0.4))
    assert failure.value.output == "started\n"
    assert runtime.request_stop()["descendants_ended"] is True


def test_missing_seal_retains_output_and_reports_unknown(runtime, tmp_path, monkeypatch):
    isolated = ProtectedCreationRuntime(tmp_path / "isolated", Path("/bin/bash"), runtime.credentials_home)
    destination = tmp_path / "selected.txt"
    destination.write_text("retained", encoding="utf-8")
    monkeypatch.setattr(runtime_module, "_HELPER", "import sys; sys.exit(0)")
    with pytest.raises(ProtectedCreationError, match="protected_creation_unknown_outcome"):
        isolated.run(_call("print('unproved')", arguments=(destination,), outputs=(destination,)))
    assert isolated.request_stop() == {"status": "unknown_outcome", "descendants_ended": False}
    assert destination.read_text() == "retained"
    assert isolated.work_directory.exists()
    reconstructed = ProtectedCreationRuntime(isolated.jail_root, isolated.executable, isolated.credentials_home)
    assert reconstructed.request_stop() == {"status": "unknown_outcome", "descendants_ended": False}
    with pytest.raises(ProtectedCreationError, match="protected_creation_unknown_outcome"):
        reconstructed.run(_call("print('must not resume')"))


def test_launcher_environment_does_not_load_caller_python_code(runtime, tmp_path):
    external_marker = tmp_path / "external-code-ran"
    (tmp_path / "sitecustomize.py").write_text(
        f"from pathlib import Path; Path({str(external_marker)!r}).write_text('unsafe')", encoding="utf-8")
    completed = runtime.run(_call("import sys; print('private interpreter')", environment={
        "PYTHONPATH": str(tmp_path), "PYTHONHOME": str(tmp_path), "PATH": str(tmp_path), "HOME": str(tmp_path)}))
    assert completed.stdout == "private interpreter\n"
    assert not external_marker.exists()


def test_config_values_and_work_directory_paths_are_mapped_exactly(runtime, tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    child = runtime.work_directory / "requested-copy.py"
    completed = runtime.run(_call("import json, sys; print(json.dumps(sys.argv[1:]))", arguments=(
        f'model_catalog_json="{schema}"', str(runtime.work_directory), str(child), str(schema) + ".other"),
        inputs=(schema,)))
    assert json.loads(completed.stdout) == [
        'model_catalog_json="/inputs/0000-schema.json"', "/workspace", "/workspace/requested-copy.py", str(schema) + ".other",
    ]


def test_prompt_translates_only_declared_paths_and_owned_work(runtime, tmp_path):
    schema = tmp_path / "prompt-schema.json"
    schema.write_text("{}", encoding="utf-8")
    output = tmp_path / "prompt-result.json"
    original = tmp_path / "external-original.py"
    working = runtime.work_directory / "operations/current/copy.py"
    values = {"schema": str(schema), "output": str(output), "original": str(original), "working": str(working)}
    completed = runtime.run(_call("import json; print(json.dumps(" + repr(values) + "))",
        inputs=(schema,), outputs=(output,)))
    result = json.loads(completed.stdout)
    assert result["schema"] == "/inputs/0000-prompt-schema.json"
    assert result["output"].startswith("/outputs/")
    assert result["output"].endswith("-0000-prompt-result.json")
    assert result["original"] == str(original)
    assert result["working"] == "/workspace/operations/current/copy.py"


def test_untrusted_control_directory_never_launches(runtime, tmp_path):
    protected = ProtectedCreationRuntime(tmp_path / "jail", Path("/bin/bash"), runtime.credentials_home)
    control = tmp_path / ".jail-control"
    control.mkdir(mode=0o777)
    control.chmod(0o777)
    with pytest.raises(ProtectedCreationError, match="protected_creation_jail_unrecognized"):
        protected.run(_call("print('must not launch')"))
    assert not protected.jail_root.exists()
