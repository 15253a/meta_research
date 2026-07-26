from __future__ import annotations

import os
import pwd
import shutil
import stat
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from orchestrator import runtime_mcp as runtime_mcp_module
from orchestrator import runtime_storage as runtime_storage_module
from orchestrator.instance_lease import InstanceLease
from orchestrator.runtime_mcp import RuntimeMCPBroker
from orchestrator.runtime_storage import configure_process_storage


_SERVICE_STORAGE = {
    "TMPDIR": ".process-tmp",
    "TMP": ".process-tmp",
    "TEMP": ".process-tmp",
    "HOME": ".process-home/service",
    "CODEX_HOME": ".codex-runtime/service",
    "CODEX_SQLITE_HOME": ".codex-runtime/service-sqlite",
    "XDG_CACHE_HOME": ".process-cache/service",
    "PIP_CACHE_DIR": ".process-cache/service/pip",
    "HF_HOME": ".process-cache/service/huggingface",
    "HF_HUB_CACHE": ".process-cache/service/huggingface/hub",
    "HF_DATASETS_CACHE": ".process-cache/service/huggingface/datasets",
    "TRANSFORMERS_CACHE": ".process-cache/service/huggingface/transformers",
    "TORCH_HOME": ".process-cache/service/torch",
    "TORCH_EXTENSIONS_DIR": ".process-cache/service/torch-extensions",
    "TRITON_CACHE_DIR": ".process-cache/service/triton",
    "XDG_CONFIG_HOME": ".process-home/service/.config",
    "XDG_DATA_HOME": ".process-home/service/.local/share",
    "XDG_STATE_HOME": ".process-home/service/.local/state",
    "CONDA_PKGS_DIRS": ".process-cache/service/conda-pkgs",
    "CONDA_ENVS_PATH": "environments",
    "UV_CACHE_DIR": ".process-cache/service/uv",
    "CUDA_CACHE_PATH": ".process-cache/service/cuda",
    "MPLCONFIGDIR": ".process-cache/service/matplotlib",
    "NUMBA_CACHE_DIR": ".process-cache/service/numba",
    "PYTHONPYCACHEPREFIX": ".process-cache/service/pycache",
}


def _same_uid_query(monkeypatch) -> None:
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    tracked = {
        *_SERVICE_STORAGE,
        "METARESEARCH_QUERY_HOME",
        "METARESEARCH_QUERY_CODEX_HOME",
        "METARESEARCH_QUERY_CODEX_SQLITE_HOME",
        "METARESEARCH_QUERY_CACHE_HOME",
        "METARESEARCH_STORAGE_ROOT",
        "SSL_CERT_FILE",
    }
    for name in tracked:
        if name in os.environ:
            monkeypatch.setenv(name, os.environ[name])
        else:
            # Record an absent original value so direct production mutations
            # are removed again when pytest unwinds this fixture.
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    monkeypatch.delenv("METARESEARCH_STORAGE_ROOT", raising=False)
    monkeypatch.setenv(
        "METARESEARCH_QUERY_RUN_AS_USER", pwd.getpwuid(os.geteuid()).pw_name)


def test_complete_environment_mapping_preserves_existing_layout(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    root = tmp_path / "shared"
    session = root / ".codex-runtime" / "service" / "sessions" / "keep"
    session.parent.mkdir(parents=True)
    session.write_text("durable", encoding="utf-8")
    environment = root / "environments" / "existing-env"
    environment.mkdir(parents=True)

    configured = configure_process_storage(root, require_external_mount=False)

    canonical_root = str(root.absolute())
    assert os.environ["METARESEARCH_STORAGE_ROOT"] == canonical_root
    assert configured["METARESEARCH_STORAGE_ROOT"] == canonical_root
    for name, relative in _SERVICE_STORAGE.items():
        expected = str(root / relative)
        assert os.environ[name] == expected
        assert Path(expected).is_dir()
    assert os.environ["METARESEARCH_QUERY_HOME"] == str(
        root / ".process-home" / "query")
    assert os.environ["METARESEARCH_QUERY_CODEX_HOME"] == str(
        root / ".codex-runtime" / "query")
    assert os.environ["METARESEARCH_QUERY_CODEX_SQLITE_HOME"] == str(
        root / ".codex-runtime" / "query-sqlite")
    assert os.environ["METARESEARCH_QUERY_CACHE_HOME"] == str(
        root / ".process-cache" / "query")
    assert session.read_text(encoding="utf-8") == "durable"
    assert environment.is_dir()


def test_runtime_storage_synchronizes_newest_auth_across_service_and_query(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    root = tmp_path / "shared"
    service_source = tmp_path / "service-source"
    query_source = tmp_path / "query-source"
    service_source.mkdir()
    query_source.mkdir()
    fresh = (
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-21T05:06:21Z",'
        '"tokens":{"access_token":"fresh"}}\n')
    stale = (
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-11T05:10:54Z",'
        '"tokens":{"access_token":"expired"}}\n')
    (service_source / "auth.json").write_text(fresh)
    (query_source / "auth.json").write_text(stale)
    monkeypatch.setenv("CODEX_HOME", str(service_source))
    monkeypatch.setenv("METARESEARCH_QUERY_CODEX_HOME", str(query_source))

    configure_process_storage(root, require_external_mount=False)

    assert (root / ".codex-runtime" / "service" / "auth.json").read_text() == fresh
    assert (root / ".codex-runtime" / "query" / "auth.json").read_text() == fresh


def test_inherited_marker_reuses_console_root_in_quest_child(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    shared = tmp_path / "registry"
    configure_process_storage(shared, require_external_mount=False)
    quest = shared / "quests" / "q1" / "work"

    second = configure_process_storage(quest, require_external_mount=False)

    assert second["METARESEARCH_STORAGE_ROOT"] == str(shared.absolute())
    assert os.environ["TMPDIR"] == str(shared / ".process-tmp")
    assert not (quest / ".process-tmp").exists()
    assert not (quest / "runtime").exists()


def test_explicit_top_level_root_must_match_inherited_marker_before_mutation(
        vepfs_tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    marker = vepfs_tmp_path / "already-bound"
    requested = vepfs_tmp_path / "explicit-console-root"
    monkeypatch.setenv("METARESEARCH_STORAGE_ROOT", str(marker))
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir

    with pytest.raises(ValueError, match="METARESEARCH_STORAGE_ROOT|不一致"):
        configure_process_storage(
            requested, require_external_mount=True,
            require_requested_root=True)

    assert not marker.exists()
    assert not requested.exists()
    assert dict(os.environ) == before_environment
    assert tempfile.tempdir == before_tempdir


def test_private_work_containment_is_rejected_before_any_storage_mutation(
        vepfs_tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    work = vepfs_tmp_path / "quest"
    nested_storage = work / "private-runtime"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir

    with pytest.raises(ValueError, match="私有|work-root|之外"):
        configure_process_storage(
            nested_storage, require_external_mount=True,
            private_work_root=work)

    assert not work.exists()
    assert dict(os.environ) == before_environment
    assert tempfile.tempdir == before_tempdir


def test_validation_rejection_has_zero_side_effects(monkeypatch):
    _same_uid_query(monkeypatch)
    candidate = Path("/tmp") / f"metaresearch-reject-{uuid.uuid4().hex}"
    before_environment = dict(os.environ)
    before_tempdir = tempfile.tempdir
    try:
        with pytest.raises(ValueError, match="/tmp|根盘|overlay"):
            configure_process_storage(candidate, require_external_mount=True)
        assert not candidate.exists()
        assert dict(os.environ) == before_environment
        assert tempfile.tempdir == before_tempdir
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def test_final_symlink_is_rejected_without_mutating_target(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    target = tmp_path / "target"
    target.mkdir()
    candidate = tmp_path / "candidate"
    candidate.symlink_to(target, target_is_directory=True)
    before_environment = dict(os.environ)

    with pytest.raises(ValueError, match="symlink"):
        configure_process_storage(candidate, require_external_mount=False)

    assert list(target.iterdir()) == []
    assert dict(os.environ) == before_environment


def test_intermediate_symlink_is_rejected_before_child_creation(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    actual = tmp_path / "actual"
    actual.mkdir()
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "alias").symlink_to(actual, target_is_directory=True)
    candidate = outer / "alias" / "child"
    before_environment = dict(os.environ)

    with pytest.raises(ValueError, match="symlink"):
        configure_process_storage(candidate, require_external_mount=False)

    assert not (actual / "child").exists()
    assert dict(os.environ) == before_environment


def test_directory_mode_update_is_fd_pinned_against_final_symlink_swap(
        tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    parent.mkdir()
    candidate = parent / "candidate"
    candidate.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    os.chmod(outside, 0o755)
    displaced = parent / "displaced"
    real_chmod = os.chmod
    real_fchmod = os.fchmod
    swapped = False

    def swap_path_once():
        nonlocal swapped
        if swapped:
            return
        candidate.rename(displaced)
        candidate.symlink_to(outside, target_is_directory=True)
        swapped = True

    def swapping_chmod(path, mode):
        if Path(path) == candidate:
            swap_path_once()
        return real_chmod(path, mode)

    def swapping_fchmod(fd, mode):
        swap_path_once()
        return real_fchmod(fd, mode)

    monkeypatch.setattr(runtime_storage_module.os, "chmod", swapping_chmod)
    monkeypatch.setattr(runtime_storage_module.os, "fchmod", swapping_fchmod)

    with pytest.raises(ValueError, match="symlink|身份|漂移"):
        runtime_storage_module._ensure_directory(
            candidate, service_uid=os.geteuid(),
            uid=os.geteuid(), gid=os.getegid(), mode=0o700)

    assert swapped is True
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_codex_bootstrap_rejects_symlink_source_root_without_copying_or_chown(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    source_auth = source / "auth.json"
    source_auth.write_text('{"secret":"root-auth"}\n')
    original_identity = (source_auth.stat().st_uid, source_auth.stat().st_gid)
    alias = tmp_path / "source-alias"
    alias.symlink_to(source, target_is_directory=True)
    destination = tmp_path / "destination"
    destination.mkdir()
    chowns = []
    monkeypatch.setattr(
        runtime_storage_module.os, "fchown",
        lambda _fd, uid, gid: chowns.append((uid, gid)))

    with pytest.raises(ValueError, match="symlink|source"):
        runtime_storage_module._seed_codex_identity(
            destination, str(alias), service_uid=0, uid=424242, gid=424242)

    assert not (destination / "auth.json").exists()
    assert chowns == []
    assert (source_auth.stat().st_uid, source_auth.stat().st_gid) == original_identity
    assert source_auth.read_text() == '{"secret":"root-auth"}\n'


def test_codex_bootstrap_refreshes_existing_auth_from_newer_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_auth = source / "auth.json"
    source_auth.write_text(
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-21T05:06:21.206763112Z",'
        '"tokens":{"access_token":"fresh"}}\n')
    destination = tmp_path / "destination"
    destination.mkdir()
    destination_auth = destination / "auth.json"
    destination_auth.write_text(
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-11T05:10:54Z",'
        '"tokens":{"access_token":"expired"}}\n')
    destination_auth.chmod(0o600)

    runtime_storage_module._seed_codex_identity(
        destination, str(source), service_uid=os.geteuid(),
        uid=os.geteuid(), gid=os.getegid())

    assert destination_auth.read_bytes() == source_auth.read_bytes()
    assert stat.S_IMODE(destination_auth.stat().st_mode) == 0o600


def test_codex_bootstrap_preserves_existing_auth_newer_than_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    source_auth = source / "auth.json"
    source_auth.write_text(
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-11T05:10:54Z",'
        '"tokens":{"access_token":"stale"}}\n')
    destination = tmp_path / "destination"
    destination.mkdir()
    destination_auth = destination / "auth.json"
    destination_auth.write_text(
        '{"auth_mode":"chatgpt","last_refresh":"2026-07-21T05:06:21Z",'
        '"tokens":{"access_token":"fresh"}}\n')
    destination_auth.chmod(0o600)
    before = destination_auth.read_bytes()

    runtime_storage_module._seed_codex_identity(
        destination, str(source), service_uid=os.geteuid(),
        uid=os.geteuid(), gid=os.getegid())

    assert destination_auth.read_bytes() == before


def test_codex_bootstrap_detects_source_growth_on_the_open_descriptor(
        tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    source_auth = source / "auth.json"
    source_auth.write_bytes(b'{"token":"bounded"}\n')
    destination = tmp_path / "destination"
    destination.mkdir()
    real_read = os.read
    grew = False

    def grow_after_first_read(fd, size):
        nonlocal grew
        payload = real_read(fd, size)
        try:
            opened = Path(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            opened = None
        if payload and not grew and opened == source_auth:
            grew = True
            with source_auth.open("ab") as stream:
                stream.write(b"x")
                stream.flush()
                os.fsync(stream.fileno())
        return payload

    monkeypatch.setattr(runtime_storage_module.os, "read", grow_after_first_read)
    with pytest.raises(ValueError, match="漂移|变化|增长"):
        runtime_storage_module._seed_codex_identity(
            destination, str(source), service_uid=os.geteuid(),
            uid=os.geteuid(), gid=os.getegid())

    assert grew is True
    assert not (destination / "auth.json").exists()


def test_unknown_service_uid_is_reported_as_storage_validation_error(
        vepfs_tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    monkeypatch.delenv("METARESEARCH_QUERY_RUN_AS_USER", raising=False)
    monkeypatch.setattr(runtime_storage_module.os, "geteuid", lambda: 424242)
    monkeypatch.setattr(runtime_storage_module.os, "getegid", lambda: 424242)

    def missing_uid(_uid):
        raise KeyError("unknown uid")

    monkeypatch.setattr(runtime_storage_module.pwd, "getpwuid", missing_uid)
    with pytest.raises(ValueError, match="账户不存在|UID"):
        configure_process_storage(
            vepfs_tmp_path / "storage", require_external_mount=True)

    assert not (vepfs_tmp_path / "storage").exists()


def test_instance_lease_private_quest_does_not_hide_shared_query_paths(
        tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    configure_process_storage(tmp_path, require_external_mount=False)
    quest = tmp_path / "quests" / "q1"
    lease = InstanceLease.acquire(quest, heartbeat_interval_s=0.02)
    try:
        assert stat.S_IMODE(quest.stat().st_mode) == 0o700
        for path in (
                tmp_path, tmp_path / ".process-tmp",
                tmp_path / ".process-cache", tmp_path / ".process-home",
                tmp_path / ".codex-runtime"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o711
        for path in (
                tmp_path / ".process-cache" / "query",
                tmp_path / ".process-home" / "query",
                tmp_path / ".codex-runtime" / "query",
                tmp_path / ".codex-runtime" / "query-sqlite"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert quest not in path.parents
    finally:
        assert lease.close() is None


def test_actual_codexro_can_traverse_shared_query_storage_beside_private_quest(
        vepfs_tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("actual cross-UID traversal requires a root test process")
    account = pwd.getpwnam("codexro")
    _same_uid_query(monkeypatch)
    service_source = vepfs_tmp_path / "service-bootstrap"
    service_source.mkdir(mode=0o700)
    (service_source / "auth.json").write_text("{}\n")
    os.chmod(service_source / "auth.json", 0o600)
    query_source = vepfs_tmp_path / "query-bootstrap"
    query_source.mkdir(mode=0o700)
    query_auth = query_source / "auth.json"
    query_auth.write_text("{}\n")
    os.chmod(query_auth, 0o600)
    os.chown(query_auth, account.pw_uid, account.pw_gid)
    os.chown(query_source, account.pw_uid, account.pw_gid)
    monkeypatch.setenv("CODEX_HOME", str(service_source))
    monkeypatch.setenv("METARESEARCH_QUERY_RUN_AS_USER", account.pw_name)
    monkeypatch.setenv("METARESEARCH_QUERY_CODEX_HOME", str(query_source))
    configure_process_storage(vepfs_tmp_path, require_external_mount=True)
    quest = vepfs_tmp_path / "quests" / "private"
    lease = InstanceLease.acquire(quest, heartbeat_interval_s=0.02)
    query_paths = [
        os.environ["METARESEARCH_QUERY_HOME"],
        os.environ["METARESEARCH_QUERY_CODEX_HOME"],
        os.environ["METARESEARCH_QUERY_CODEX_SQLITE_HOME"],
        os.environ["METARESEARCH_QUERY_CACHE_HOME"],
        str(vepfs_tmp_path / ".process-tmp"),
    ]
    script = (
        "import os,sys\n"
        "for value in sys.argv[1:]: os.stat(value)\n"
        "open(os.path.join(sys.argv[2], 'auth.json'), 'rb').read(1)\n")
    try:
        completed = subprocess.run(
            ["/usr/bin/python3", "-c", script, *query_paths],
            capture_output=True, text=True, check=False,
            user=account.pw_uid, group=account.pw_gid, extra_groups=[])
        assert completed.returncode == 0, completed.stderr
        assert stat.S_IMODE(quest.stat().st_mode) == 0o700
    finally:
        assert lease.close() is None


def test_implicit_tempfile_and_runtime_mcp_use_process_tmp(
        vepfs_tmp_path, monkeypatch):
    _same_uid_query(monkeypatch)
    root = vepfs_tmp_path
    configure_process_storage(root, require_external_mount=True)
    process_tmp = root / ".process-tmp"
    implicit = Path(tempfile.mkdtemp(prefix="storage-proof-"))
    try:
        assert implicit.parent == process_tmp
    finally:
        implicit.rmdir()

    class FilesystemSocketDouble:
        daemon_threads = True

        def __init__(self, address, _handler):
            self.address = address
            Path(address).touch()

        def serve_forever(self):
            return None

        def shutdown(self):
            return None

        def server_close(self):
            return None

    # Exercise tempfile's implicit destination even though the real long
    # VEPFS path correctly selects Linux's abstract Unix-socket namespace.
    monkeypatch.setattr(runtime_mcp_module.os, "fsencode", lambda _value: b"x")
    monkeypatch.setattr(
        runtime_mcp_module, "_UnixBrokerServer", FilesystemSocketDouble)
    broker = RuntimeMCPBroker(object()).start()
    directory = broker._directory
    try:
        assert directory is not None
        assert directory.parent == process_tmp
    finally:
        broker.close()
