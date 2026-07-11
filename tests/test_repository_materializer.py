"""CP11.4c.2a: exact GitHub repository materialization and adapter v2."""
from __future__ import annotations

import base64
import copy
import errno
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import urllib.parse
import urllib.error
from pathlib import Path, PurePosixPath

import pytest
import yaml

from orchestrator.repository_materializer import (
    GitHubRepositoryMaterializer,
    RepositoryCacheError,
    RepositoryMaterializationError,
    RepositoryTransportError,
    _canonical,
    _git_blob_sha1,
    _git_tree_sha1,
    _sha256,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _adapter(*, dependency_mode="pinned_image_only", dependency_locks=None):
    return {
        "version": 2,
        "artifact_relpath": "artifact.bin",
        "artifact_type": "external_model",
        "smoke_argv": ["python", "{repo}/smoke.py"],
        "eval_argv": ["python", "{repo}/eval.py", "{artifact}"],
        "dependency_mode": dependency_mode,
        "dependency_locks": dependency_locks or [],
        "factory_protocol": {
            "name": "repository factory",
            "version": 1,
            "scope_spec": {"dataset": "frozen-toy", "split": "factory"},
            "metrics": [{
                "log_key": "accuracy", "name": "accuracy", "version": 1,
                "direction": "higher", "unit": "ratio",
                "compute_spec": "adapter aggregate",
                "readout_rule": "higher is better",
            }],
            "required": ["accuracy"],
        },
    }


def _repo_files(*, adapter=None, artifact=b"frozen model"):
    return {
        ".meta-research/import-adapter.json": json.dumps(
            adapter or _adapter(), sort_keys=True).encode(),
        "artifact.bin": artifact,
        "smoke.py": b"print('smoke ok')\n",
        "eval.py": b"print('metric_value: accuracy=0.91')\n",
        "LICENSE": b"MIT license evidence\n",
    }


class _FrozenRepo:
    def __init__(self, full_name, revision, files, *, modes=None, gitlinks=None):
        self.full_name = full_name
        self.revision = revision
        self.files = dict(files)
        self.modes = dict(modes or {})
        self.gitlinks = dict(gitlinks or {})
        self.trees = {}
        self.root_tree = self._build_tree("")
        self.archive_serial = 0
        self.archive_overrides = {}

    def _children(self, directory):
        prefix = directory + "/" if directory else ""
        children = set()
        for path in list(self.files) + list(self.gitlinks):
            if not path.startswith(prefix):
                continue
            tail = path[len(prefix):]
            if tail:
                children.add(tail.split("/", 1)[0])
        return sorted(children)

    def _build_tree(self, directory):
        prefix = directory + "/" if directory else ""
        normalized = []
        api_entries = []
        for name in self._children(directory):
            rel = prefix + name
            descendants = [
                path for path in list(self.files) + list(self.gitlinks)
                if path.startswith(rel + "/")]
            if descendants:
                sha = self._build_tree(rel)
                mode, kind, size = "040000", "tree", None
            elif rel in self.gitlinks:
                sha = self.gitlinks[rel]
                mode, kind, size = "160000", "commit", None
            else:
                payload = self.files[rel]
                sha = _git_blob_sha1(payload)
                mode = self.modes.get(rel, "100644")
                kind, size = "blob", len(payload)
            normalized.append({
                "name": name, "mode": mode, "type": kind, "sha": sha})
            item = {"path": name, "mode": mode, "type": kind, "sha": sha}
            if size is not None:
                item["size"] = size
            api_entries.append(item)
        tree_sha = _git_tree_sha1(normalized)
        self.trees[tree_sha] = {
            "sha": tree_sha, "truncated": False, "tree": api_entries}
        return tree_sha

    def commit_payload(self):
        return {"sha": self.revision, "tree": {"sha": self.root_tree}}

    def write_archive(self, destination):
        self.archive_serial += 1
        root = self.full_name.replace("/", "-") + "-" + self.revision[:7]
        files = {**self.files, **self.archive_overrides}
        directories = sorted({
            "/".join(PurePosixPath(path).parts[:index])
            for path in files for index in range(1, len(PurePosixPath(path).parts))
        } | {
            "/".join(PurePosixPath(path).parts[:index])
            for path in self.gitlinks
            for index in range(1, len(PurePosixPath(path).parts) + 1)
        })
        with tarfile.open(destination, mode="w:gz") as archive:
            root_info = tarfile.TarInfo(root)
            root_info.type = tarfile.DIRTYPE
            root_info.mode = 0o755
            root_info.mtime = self.archive_serial
            archive.addfile(root_info)
            for directory in directories:
                info = tarfile.TarInfo(f"{root}/{directory}")
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = self.archive_serial
                archive.addfile(info)
            for rel, payload in sorted(files.items()):
                info = tarfile.TarInfo(f"{root}/{rel}")
                info.size = len(payload)
                info.mode = 0o755 if self.modes.get(rel) == "100755" else 0o644
                info.mtime = self.archive_serial
                archive.addfile(info, io.BytesIO(payload))
        raw = destination.read_bytes()
        return {
            "url": (f"https://api.github.com/repos/{self.full_name}/tarball/"
                    f"{self.revision}"),
            "bytes": len(raw), "sha256": _sha256(raw),
        }


class _Provider:
    def __init__(self, repositories):
        self.repositories = {repo.full_name: repo for repo in repositories}
        self.api_calls = 0
        self.archive_calls = 0

    def api_getter(self, url, _label):
        self.api_calls += 1
        parts = urllib.parse.urlsplit(url).path.strip("/").split("/")
        full_name = "/".join(parts[1:3])
        repo = self.repositories[full_name]
        if parts[3:5] == ["git", "commits"]:
            assert parts[5] == repo.revision
            return repo.commit_payload()
        if parts[3:5] == ["git", "trees"]:
            return copy.deepcopy(repo.trees[parts[5]])
        if parts[3] == "license":
            raw = b"MIT license evidence\n"
            return {
                "license": {"spdx_id": "MIT"}, "encoding": "base64",
                "content": base64.b64encode(raw).decode(), "path": "LICENSE",
            }
        raise AssertionError(f"unexpected API URL: {url}")

    def archive_fetcher(self, full_name, revision, destination, _maximum):
        self.archive_calls += 1
        repo = self.repositories[full_name]
        assert revision == repo.revision
        return repo.write_archive(destination)


def _runtime_environment():
    compiler = POLICY["import_materialization"]["compiler"]
    return {
        "PYTHON_VERSION": compiler["version"],
        "PYTHON_SHA256": compiler["artifact_sha256"].removeprefix("sha256:"),
    }


def _materializer(tmp_path, provider, *, config=None):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    return GitHubRepositoryMaterializer(
        work_root=work,
        config=config or POLICY["import_materialization"],
        sandbox_config=POLICY["execution"]["sandbox"],
        auto_license=POLICY["import_search"]["auto_license"],
        runtime_environment=_runtime_environment(),
        api_getter=provider.api_getter,
        archive_fetcher=provider.archive_fetcher), work


def _candidate(repo, *, candidate_id=1):
    license_hash = _sha256(b"MIT license evidence\n")
    snapshot = {
        "version": 1, "provider": "github_rest_v1", "query": "adapter",
        "provider_result_id": "R_1", "retrieved_at": "2026-07-11T00:00:00+00:00",
        "ranking": {"rank": 0, "recipe": "github-stars", "scale": "small"},
        "repository": {
            "full_name": repo.full_name, "default_branch": "main",
            "stars": 10, "updated_at": "2026-07-01T00:00:00Z"},
        "canonical_uri": f"https://github.com/{repo.full_name}",
        "revision": repo.revision,
        "license": {
            "spdx_id": "MIT", "lookup_status": "found",
            "evidence_ref": (f"https://api.github.com/repos/{repo.full_name}/"
                             f"contents/LICENSE?ref={repo.revision}"),
            "content_sha256": license_hash},
        "policy_hash": "sha256:" + "1" * 64,
    }
    raw = _canonical(snapshot).decode()
    return {
        "id": candidate_id, "question_id": 7,
        "canonical_uri": snapshot["canonical_uri"],
        "revision": repo.revision, "source_kind": "repo",
        "search_snapshot_json": raw,
        "search_snapshot_hash": _sha256(raw.encode()),
    }


def test_exact_commit_materializes_once_and_reuses_content_identity(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, work = _materializer(tmp_path, provider)

    first = materialize(_candidate(repo))
    assert {item["path"] for item in first["file_ledger"]} == set(repo.files)
    assert first["requires_adversarial_sandbox"] is True
    assert first["supply_chain"]["revision"] == repo.revision
    assert first["supply_chain"]["network_isolation"] is True
    assert all("archive_sha256" not in source for source in
               first["supply_chain"]["artifact_download_sources"])
    artifact = Path(first["source_tree"]) / "artifact.bin"
    assert stat.S_IMODE(os.lstat(artifact).st_mode) == 0o444
    calls = (provider.api_calls, provider.archive_calls)

    second = materialize(_candidate(repo))
    assert second["repository_snapshot_hash"] == first["repository_snapshot_hash"]
    assert (provider.api_calls, provider.archive_calls) == calls

    # A regenerated compressed archive is transport evidence, not source identity.
    next((work / "state" / "import-materializations" / "indexes").glob("*.json")).unlink()
    third = materialize(_candidate(repo))
    assert provider.archive_calls == calls[1] + 1
    assert third["repository_snapshot_hash"] == first["repository_snapshot_hash"]
    assert len(list((work / "state" / "import-materializations" / "objects").iterdir())) == 1


def test_publish_race_reuses_only_the_verified_winning_object(tmp_path, monkeypatch):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    real_replace = os.replace
    raced = False

    def racing_replace(source, destination, *args, **kwargs):
        nonlocal raced
        if (not raced and not kwargs
                and Path(destination).parent.name == "objects"):
            raced = True
            shutil.copytree(source, destination, copy_function=shutil.copy2)
            raise FileExistsError(errno.EEXIST, "simulated concurrent publisher")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", racing_replace)
    result = materialize(_candidate(repo))

    assert raced is True
    assert Path(result["source_tree"]).is_dir()
    assert result["repository_snapshot_hash"].startswith("sha256:")


def test_submodule_commit_license_and_files_are_closed(tmp_path):
    child = _FrozenRepo("acme/child", "b" * 40, {
        "child.py": b"VALUE = 1\n", "LICENSE": b"MIT license evidence\n"})
    root_files = _repo_files()
    root_files[".gitmodules"] = (
        b'[submodule "child"]\n\tpath = deps/child\n'
        b'\turl = https://github.com/acme/child.git\n'
        b'\tbranch = stable\n\tshallow = true\n')
    root = _FrozenRepo(
        "acme/model", "a" * 40, root_files,
        gitlinks={"deps/child": child.revision})
    provider = _Provider([root, child])
    materialize, _work = _materializer(tmp_path, provider)

    result = materialize(_candidate(root))
    assert any(item["path"] == "deps/child/child.py"
               for item in result["file_ledger"])
    assert result["supply_chain"]["submodules"] == [{
        "path": "deps/child", "repository": "acme/child",
        "revision": child.revision, "root_tree_sha1": child.root_tree,
    }]
    sources = result["supply_chain"]["artifact_download_sources"]
    assert sources[0]["license"]["spdx_id"] == "MIT"
    assert sources[1]["license"]["spdx_id"] == "MIT"


@pytest.mark.parametrize(("artifact", "adapter", "match"), [
    (b"version https://git-lfs.github.com/spec/v1\n"
     b"oid sha256:" + b"0" * 64 + b"\nsize 123\n", _adapter(), "Git LFS"),
    (b"version https://git-lfs.github.com/spec/v1\r\n"
     b"oid sha256:" + b"0" * 64 + b"\r\nsize 123", _adapter(), "Git LFS"),
    (b"model", _adapter(
        dependency_mode="image_locked", dependency_locks=["requirements.lock"]),
     "pinned_image_only"),
])
def test_unimplemented_dependency_inputs_fail_closed(
        tmp_path, artifact, adapter, match):
    files = _repo_files(adapter=adapter, artifact=artifact)
    if adapter["dependency_locks"]:
        files["requirements.lock"] = b"example==1.0 --hash=sha256:" + b"2" * 64
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    with pytest.raises(RepositoryMaterializationError, match=match):
        materialize(_candidate(repo))


@pytest.mark.parametrize(("field", "match"), [
    ("scope_spec", "非空 object"),
    ("compute_spec", "非空字符串"),
    ("readout_rule", "非空字符串"),
])
def test_adapter_requires_explicit_metric_semantics(tmp_path, field, match):
    adapter = _adapter()
    if field == "scope_spec":
        adapter["factory_protocol"][field] = {}
    else:
        adapter["factory_protocol"]["metrics"][0][field] = None
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files(adapter=adapter))
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryMaterializationError, match=match):
        materialize(_candidate(repo))


def test_gitmodules_default_inheritance_is_rejected(tmp_path):
    child = _FrozenRepo("acme/child", "b" * 40, {
        "child.py": b"VALUE = 1\n", "LICENSE": b"MIT license evidence\n"})
    root_files = _repo_files()
    root_files[".gitmodules"] = (
        b"[DEFAULT]\npath = deps/child\n"
        b"url = https://github.com/acme/child.git\n"
        b'[submodule "child"]\n')
    root = _FrozenRepo(
        "acme/model", "a" * 40, root_files,
        gitlinks={"deps/child": child.revision})
    provider = _Provider([root, child])
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryMaterializationError, match="DEFAULT"):
        materialize(_candidate(root))


def test_archive_tree_mismatch_and_symlink_are_rejected(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    repo.archive_overrides["artifact.bin"] = b"tampered mod"
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    with pytest.raises(RepositoryMaterializationError, match="Git blob SHA"):
        materialize(_candidate(repo))

    symlink_repo = _FrozenRepo(
        "acme/link", "c" * 40, _repo_files(),
        modes={"artifact.bin": "120000"})
    symlink_provider = _Provider([symlink_repo])
    symlink_materialize, _work = _materializer(
        tmp_path / "symlink", symlink_provider)
    with pytest.raises(RepositoryMaterializationError, match="symlink"):
        symlink_materialize(_candidate(symlink_repo, candidate_id=2))


def test_published_tree_tamper_and_compiler_drift_are_rejected(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    result = materialize(_candidate(repo))
    artifact = Path(result["source_tree"]) / "artifact.bin"
    os.chmod(artifact, 0o644)
    artifact.write_bytes(b"tampered")
    with pytest.raises(RepositoryCacheError, match="hash|tree"):
        materialize(_candidate(repo))

    work = tmp_path / "bad-runtime"
    work.mkdir()
    with pytest.raises(ValueError, match="compiler environment"):
        GitHubRepositoryMaterializer(
            work_root=work, config=POLICY["import_materialization"],
            sandbox_config=POLICY["execution"]["sandbox"],
            auto_license=POLICY["import_search"]["auto_license"],
            runtime_environment={"PYTHON_VERSION": "0.0.0", "PYTHON_SHA256": "0" * 64},
            api_getter=provider.api_getter,
            archive_fetcher=provider.archive_fetcher)


def test_corrupt_local_index_is_cache_failure_not_candidate_failure(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, work = _materializer(tmp_path, provider)
    materialize(_candidate(repo))
    index = next(
        (work / "state" / "import-materializations" / "indexes").glob("*.json"))
    index.write_text("{broken", encoding="utf-8")

    with pytest.raises(RepositoryCacheError, match="index"):
        materialize(_candidate(repo))


def test_license_api_evidence_must_match_same_commit_file_ledger(tmp_path):
    files = _repo_files()
    files["LICENSE"] = b"different license bytes\n"
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryMaterializationError, match="root license evidence"):
        materialize(_candidate(repo))


def test_protocol_families_are_stable_and_transport_is_retryable(tmp_path):
    first_repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    changed_adapter = _adapter()
    changed_adapter["factory_protocol"]["scope_spec"]["split"] = "changed"
    second_repo = _FrozenRepo(
        "acme/model", "b" * 40, _repo_files(adapter=changed_adapter))
    first_provider = _Provider([first_repo])
    second_provider = _Provider([second_repo])
    first_materialize, _ = _materializer(tmp_path / "first", first_provider)
    second_materialize, _ = _materializer(tmp_path / "second", second_provider)

    first = first_materialize(_candidate(first_repo))
    second = second_materialize(_candidate(second_repo, candidate_id=2))
    assert first["protocol_id"] == second["protocol_id"]
    assert first["factory_protocol"]["metrics"] == second["factory_protocol"]["metrics"]
    stable_ids = [first["protocol_id"], *(
        pair[0] for pair in first["factory_protocol"]["metrics"])]
    assert all(0 < value <= (1 << 53) - 1 for value in stable_ids)
    assert first["target_set_hash"] != second["target_set_hash"]

    retry_materialize, _ = _materializer(tmp_path / "retry", first_provider)
    retry_materialize.api_getter = None
    retry_materialize._api_opener = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        urllib.error.URLError("temporary outage"))
    with pytest.raises(RepositoryTransportError, match="读取失败"):
        retry_materialize._get_json(
            "https://api.github.com/repos/acme/model/git/commits/" + "a" * 40,
            label="commit")
