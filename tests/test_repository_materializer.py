"""Exact GitHub repository materialization, LFS, and adapter v2/v3."""
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
import urllib.request
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
    _value_hash,
)
from orchestrator.repository_materializer_lfs import _LfsObjectRedirectHandler
from orchestrator.repository_materializer_store import (
    inspect_repository_materialization_index,
    inspect_repository_snapshot_object,
)


SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


def _adapter(*, version=2, dependency_mode="pinned_image_only", dependency_locks=None):
    return {
        "version": version,
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


def _lfs_pointer(payload):
    oid = hashlib.sha256(payload).hexdigest()
    return (b"version https://git-lfs.github.com/spec/v1\n"
            + f"oid sha256:{oid}\nsize {len(payload)}\n".encode()), oid


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
    def __init__(self, repositories, *, lfs_objects=None):
        self.repositories = {repo.full_name: repo for repo in repositories}
        self.lfs_objects = dict(lfs_objects or {})
        self.api_calls = 0
        self.archive_calls = 0
        self.lfs_batch_calls = 0
        self.lfs_object_calls = 0
        self.lfs_batch_repositories = []
        self.lfs_batch_mutator = None
        self.lfs_object_overrides = {}
        self.blob_mutator = None

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
        if parts[3:5] == ["git", "blobs"]:
            blob_sha = parts[5]
            matches = [payload for payload in repo.files.values()
                       if _git_blob_sha1(payload) == blob_sha]
            assert len(matches) == 1
            raw = matches[0]
            response = {
                "sha": blob_sha, "size": len(raw), "encoding": "base64",
                "content": base64.b64encode(raw).decode("ascii"),
            }
            return (self.blob_mutator(copy.deepcopy(response))
                    if self.blob_mutator is not None else response)
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

    def lfs_batch_getter(self, full_name, revision, request):
        self.lfs_batch_calls += 1
        self.lfs_batch_repositories.append(full_name)
        assert full_name in self.repositories
        assert revision == self.repositories[full_name].revision
        assert request["operation"] == "download"
        assert request["transfers"] == ["basic"]
        objects = []
        for item in request["objects"]:
            payload = self.lfs_objects[item["oid"]]
            assert len(payload) == item["size"]
            objects.append({
                "oid": item["oid"], "size": item["size"],
                "authenticated": False,
                "actions": {"download": {
                    "href": ("https://objects.githubusercontent.com/lfs/"
                             f"{item['oid']}?ephemeral={self.lfs_batch_calls}"),
                    "header": {"X-Lfs-Test": "bounded"},
                    "expires_in": 60,
                }},
            })
        response = {
            "transfer": "basic", "hash_algo": "sha256", "objects": objects,
        }
        if self.lfs_batch_mutator is not None:
            response = self.lfs_batch_mutator(copy.deepcopy(response))
        return response

    def lfs_object_fetcher(self, url, headers, destination, maximum):
        self.lfs_object_calls += 1
        assert headers == {"X-Lfs-Test": "bounded"}
        oid = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        payload = self.lfs_object_overrides.get(oid, self.lfs_objects[oid])
        assert len(payload) <= maximum
        destination.write_bytes(payload)
        return {"url": url, "bytes": len(payload), "sha256": _sha256(payload)}


class _HttpResponse:
    def __init__(self, payload, url, *, content_type=None):
        self._stream = io.BytesIO(payload)
        self._url = url
        self.closed = False
        self.headers = {"Content-Length": str(len(payload))}
        if content_type is not None:
            self.headers["Content-Type"] = content_type

    def read(self, maximum=-1):
        return self._stream.read(maximum)

    def geturl(self):
        return self._url

    def close(self):
        self.closed = True


def _runtime_environment():
    compiler = POLICY["import_materialization"]["compiler"]
    return {
        "PYTHON_VERSION": compiler["version"],
        "PYTHON_SHA256": compiler["artifact_sha256"].removeprefix("sha256:"),
    }


def _materializer(tmp_path, provider, *, config=None, dependency_image_builder=None):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    return GitHubRepositoryMaterializer(
        work_root=work,
        config=config or POLICY["import_materialization"],
        sandbox_config=POLICY["execution"]["sandbox"],
        auto_license=POLICY["import_search"]["auto_license"],
        runtime_environment=_runtime_environment(),
        dependency_image_builder=dependency_image_builder,
        api_getter=provider.api_getter,
        archive_fetcher=provider.archive_fetcher,
        lfs_batch_getter=provider.lfs_batch_getter,
        lfs_object_fetcher=provider.lfs_object_fetcher), work


class _FakeDependencyImageBuilder:
    def __init__(self):
        self.config = POLICY["import_materialization"]["dependency_image"]
        self.compiler = POLICY["import_materialization"]["compiler"]
        self.calls = []
        self.result = {
            "version": 1,
            "provider": "python-wheel-image-v1",
            "closure_hash": "sha256:" + "3" * 64,
            "receipt_hash": "sha256:" + "4" * 64,
            "environment_hash": "sha256:" + "5" * 64,
            "image": "sha256:" + "6" * 64,
            "image_id": "sha256:" + "6" * 64,
            "lock_canonical_hash": "sha256:" + "7" * 64,
            "wheel_manifest_hash": "sha256:" + "8" * 64,
            "build_context_hash": "sha256:" + "9" * 64,
            "runtime_receipt_hash": "sha256:" + "a" * 64,
            "image_archive_sha256": "sha256:" + "b" * 64,
            "wheels": [{
                "name": "example", "version": "1.0",
                "filename": "example-1.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/example-1.0-py3-none-any.whl",
                "sha256": "sha256:" + "c" * 64, "bytes": 10,
            }],
        }

    def build(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)

    def resolve(self, capability):
        expected = {key: self.result[key] for key in {
            "version", "provider", "closure_hash", "receipt_hash",
            "environment_hash", "image", "image_id",
        }}
        assert capability == expected

        class _Sandbox:
            environment_hash = self.result["environment_hash"]

        return _Sandbox()


class _FakeAdapterGenerator:
    def __init__(self, adapter=None, *, audit_offset=0):
        self.config = copy.deepcopy(
            POLICY["import_materialization"]["adapter_generation"])
        self.policy_hash = "sha256:" + "d" * 64
        self.adapter = adapter or _adapter()
        self.audit_offset = audit_offset
        self.calls = []

    def generate(self, *, projection, generation_context):
        self.calls.append((projection, generation_context))
        raw = _canonical(self.adapter)
        adapter_hash = _sha256(raw)
        identity_hash = _value_hash({
            "protocol": self.config["provider"],
            "candidate_id": generation_context["candidate_id"],
            "projection_hash": projection["projection_hash"],
            "policy_hash": self.policy_hash,
        })
        return {
            "adapter": copy.deepcopy(self.adapter), "raw": raw,
            "provenance": {
                "version": 1, "provider": self.config["provider"],
                "identity_hash": identity_hash,
                "projection_hash": projection["projection_hash"],
                "policy_hash": self.policy_hash,
                "adapter_sha256": adapter_hash,
                "generation_decision_id": 1 + self.audit_offset,
                "review_decision_id": 2 + self.audit_offset,
                "generation_runner_call_id": 3 + self.audit_offset,
                "review_runner_call_id": 4 + self.audit_offset,
                "review_hash": (
                    "sha256:" + ("2" if self.audit_offset == 0 else "3") * 64),
            },
        }


class _BrokenAdapterGenerator(_FakeAdapterGenerator):
    def generate(self, *, projection, generation_context):
        raise ValueError("tool-free auth unavailable")


class _MisboundAdapterGenerator(_FakeAdapterGenerator):
    def generate(self, *, projection, generation_context):
        result = super().generate(
            projection=projection, generation_context=generation_context)
        result["provenance"]["projection_hash"] = "sha256:" + "0" * 64
        return result


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


def test_missing_adapter_uses_reviewed_sidecar_without_mutating_git_tree(tmp_path):
    files = _repo_files()
    files.pop(".meta-research/import-adapter.json")
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    generator = _FakeAdapterGenerator()
    materialize.bind_adapter_generator(generator)
    context = {
        "cycle_id": "c9", "external_import_id": 10,
        "question_id": 7, "candidate_id": 1,
    }

    first = materialize(
        _candidate(repo), adapter_generation_context=context)
    assert len(generator.calls) == 1
    projection, captured_context = generator.calls[0]
    assert captured_context == context
    assert projection["repository"] == repo.full_name
    assert projection["revision"] == repo.revision
    assert first["adapter_control"]["origin"] == "generated_reviewed"
    assert first["adapter_control"]["value"] == _adapter()
    assert first["supply_chain"]["adapter_control_hash"] == _value_hash(
        first["adapter_control"])
    assert ".meta-research/import-adapter.json" not in {
        item["path"] for item in first["file_ledger"]}
    assert not (Path(first["source_tree"]) /
                ".meta-research/import-adapter.json").exists()

    second = materialize(
        _candidate(repo), adapter_generation_context=context)
    assert second["repository_snapshot_hash"] == first["repository_snapshot_hash"]
    assert len(generator.calls) == 1


def test_explicit_adapter_never_calls_bound_generator(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    generator = _FakeAdapterGenerator()
    materialize.bind_adapter_generator(generator)

    result = materialize(_candidate(repo))

    assert result["adapter_control"]["origin"] == "repository"
    assert generator.calls == []


def test_generated_target_identity_ignores_local_audit_row_ids(tmp_path):
    files = _repo_files()
    files.pop(".meta-research/import-adapter.json")
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    context = {
        "cycle_id": "c9", "external_import_id": 10,
        "question_id": 7, "candidate_id": 1,
    }
    first, _ = _materializer(tmp_path / "first", _Provider([repo]))
    second, _ = _materializer(tmp_path / "second", _Provider([repo]))
    first.bind_adapter_generator(_FakeAdapterGenerator(audit_offset=0))
    second.bind_adapter_generator(_FakeAdapterGenerator(audit_offset=100))

    left = first(_candidate(repo), adapter_generation_context=context)
    right = second(_candidate(repo), adapter_generation_context=context)

    assert left["adapter_control"] != right["adapter_control"]
    assert left["supply_chain"]["adapter_control_hash"] != (
        right["supply_chain"]["adapter_control_hash"])
    assert left["target_set_hash"] == right["target_set_hash"]
    assert left["eval_key"] == right["eval_key"]


def test_missing_adapter_without_reviewed_generator_fails_closed(tmp_path):
    files = _repo_files()
    files.pop(".meta-research/import-adapter.json")
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RuntimeError, match="reviewed generator"):
        materialize(_candidate(repo))


def test_adapter_generator_value_error_is_not_blamed_on_candidate(tmp_path):
    files = _repo_files()
    files.pop(".meta-research/import-adapter.json")
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    materialize.bind_adapter_generator(_BrokenAdapterGenerator())

    with pytest.raises(RuntimeError, match="infrastructure/config"):
        materialize(
            _candidate(repo), adapter_generation_context={
                "cycle_id": "c1", "external_import_id": 1,
                "question_id": 7, "candidate_id": 1,
            })


def test_adapter_generator_provenance_must_bind_current_projection(tmp_path):
    files = _repo_files()
    files.pop(".meta-research/import-adapter.json")
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    materialize.bind_adapter_generator(_MisboundAdapterGenerator())

    with pytest.raises(RuntimeError, match="provenance"):
        materialize(
            _candidate(repo), adapter_generation_context={
                "cycle_id": "c1", "external_import_id": 1,
                "question_id": 7, "candidate_id": 1,
            })


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


def test_unimplemented_dependency_inputs_fail_closed(tmp_path):
    artifact = b"model"
    adapter = _adapter(
        dependency_mode="image_locked", dependency_locks=["requirements.lock"])
    files = _repo_files(adapter=adapter, artifact=artifact)
    files["requirements.lock"] = b"example==1.0 --hash=sha256:" + b"2" * 64
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)
    with pytest.raises(RepositoryMaterializationError, match="pinned_image_only"):
        materialize(_candidate(repo))


def test_adapter_v3_binds_trusted_dependency_image_capability(tmp_path):
    adapter = _adapter(
        version=3, dependency_mode="python_wheel_image_v1",
        dependency_locks=[".meta-research/python-wheel-lock.json"])
    files = _repo_files(adapter=adapter)
    files[".meta-research/python-wheel-lock.json"] = b"{}\n"
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    builder = _FakeDependencyImageBuilder()
    materialize, _work = _materializer(
        tmp_path, provider, dependency_image_builder=builder)

    result = materialize(_candidate(repo))

    assert len(builder.calls) == 1
    assert builder.calls[0]["lock_entry"]["path"] == (
        ".meta-research/python-wheel-lock.json")
    assert result["execution_image"]["receipt_hash"] == builder.result["receipt_hash"]
    assert result["env_hash"] == builder.result["environment_hash"]
    supply = result["supply_chain"]
    assert supply["dependency_lock_hash"] == builder.result["lock_canonical_hash"]
    assert supply["container_image_id"] == builder.result["image_id"]
    assert supply["image_archive_sha256"] == builder.result["image_archive_sha256"]


def test_offline_repository_inspectors_do_not_resolve_dependency_image(tmp_path):
    adapter = _adapter(
        version=3, dependency_mode="python_wheel_image_v1",
        dependency_locks=[".meta-research/python-wheel-lock.json"])
    files = _repo_files(adapter=adapter)
    files[".meta-research/python-wheel-lock.json"] = b"{}\n"
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo])
    builder = _FakeDependencyImageBuilder()
    materialize, work = _materializer(
        tmp_path, provider, dependency_image_builder=builder)
    result = materialize(_candidate(repo))

    def forbidden_resolve(_capability):
        raise AssertionError("offline inspector must not resolve dependency image")

    builder.resolve = forbidden_resolve
    object_path = Path(result["source_tree"]).parent
    inspected = inspect_repository_snapshot_object(object_path)
    index_path = next(
        (work / "state" / "import-materializations" / "indexes").glob("*.json"))
    index = inspect_repository_materialization_index(index_path)

    assert inspected["receipt"]["object_hash"] == result["repository_snapshot_hash"]
    assert inspected["result"]["execution_image"] == result["execution_image"]
    assert index["object_hash"] == result["repository_snapshot_hash"]


def test_offline_repository_inspectors_reject_component_and_index_tamper(
        tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo])
    materialize, work = _materializer(tmp_path, provider)
    result = materialize(_candidate(repo))
    object_path = Path(result["source_tree"]).parent
    spec_path = object_path / "spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    spec["env_hash"] = "sha256:" + "0" * 64
    spec_path.write_bytes(_canonical(spec))

    with pytest.raises(
            RepositoryMaterializationError, match="component hash"):
        inspect_repository_snapshot_object(object_path)

    index_path = next(
        (work / "state" / "import-materializations" / "indexes").glob("*.json"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["config_hash"] = "sha256:" + "0" * 64
    index_path.write_bytes(_canonical(index))

    with pytest.raises(
            RepositoryMaterializationError, match="path identity"):
        inspect_repository_materialization_index(index_path)


def test_offline_repository_inspector_binds_pinned_base_environment(tmp_path):
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    materialize, _work = _materializer(tmp_path, _Provider([repo]))
    result = materialize(_candidate(repo))
    object_path = Path(result["source_tree"]).parent
    receipt_path = object_path / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["environment_hash"] = "sha256:" + "0" * 64
    receipt_path.write_bytes(_canonical(receipt))

    with pytest.raises(
            RepositoryMaterializationError, match="environment identity"):
        inspect_repository_snapshot_object(object_path)


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n"])
def test_explicit_reject_policy_still_fails_closed_on_lfs_pointer(
        tmp_path, line_end):
    payload = b"frozen large object"
    oid = hashlib.sha256(payload).hexdigest().encode("ascii")
    pointer = (b"version https://git-lfs.github.com/spec/v1" + line_end
               + b"oid sha256:" + oid + line_end
               + f"size {len(payload)}".encode())
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid.decode(): payload})
    config = copy.deepcopy(POLICY["import_materialization"])
    config["lfs_policy"] = "reject"
    materialize, _work = _materializer(tmp_path, provider, config=config)

    with pytest.raises(RepositoryMaterializationError, match="lfs_policy=reject"):
        materialize(_candidate(repo))


def test_lfs_batch_object_is_oid_verified_and_enters_supply_chain(tmp_path):
    payload = b"frozen LFS model bytes" * 16
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})
    materialize, work = _materializer(tmp_path, provider)

    first = materialize(_candidate(repo))
    assert (Path(first["source_tree"]) / "artifact.bin").read_bytes() == payload
    artifact = next(
        item for item in first["file_ledger"] if item["path"] == "artifact.bin")
    assert artifact["sha256"] == "sha256:" + oid
    assert artifact["bytes"] == len(payload)
    assert artifact["git_blob_sha1"] == _git_blob_sha1(pointer)
    assert artifact["lfs"] == {
        "oid": "sha256:" + oid, "size": len(payload),
        "pointer_sha256": _sha256(pointer), "pointer_bytes": len(pointer),
    }
    assert first["supply_chain"]["lfs_objects"] == [{
        "path": "artifact.bin", "repository": repo.full_name,
        "revision": repo.revision, "oid": "sha256:" + oid,
        "size": len(payload), "pointer_sha256": _sha256(pointer),
        "pointer_bytes": len(pointer),
        "pointer_git_blob_sha1": _git_blob_sha1(pointer),
    }]
    assert provider.lfs_batch_calls == 1
    assert provider.lfs_object_calls == 1
    snapshot_hash = first["repository_snapshot_hash"]
    transport_text = Path(first["snapshot_receipt"]).with_name(
        "transport.json").read_text(encoding="utf-8")
    assert "ephemeral=" not in transport_text
    assert "X-Lfs-Test" not in transport_text
    assert "objects.githubusercontent.com" in transport_text

    second = materialize(_candidate(repo))
    assert second["repository_snapshot_hash"] == snapshot_hash
    assert provider.lfs_batch_calls == 1
    assert provider.lfs_object_calls == 1

    # Signed action URLs/headers expire and may change.  Re-materialization
    # re-downloads transport evidence but must converge to the same object.
    next((work / "state" / "import-materializations" / "indexes").glob("*.json")).unlink()
    third = materialize(_candidate(repo))
    assert third["repository_snapshot_hash"] == snapshot_hash
    assert provider.lfs_batch_calls == 2
    assert provider.lfs_object_calls == 2
    assert len(list(
        (work / "state" / "import-materializations" / "objects").iterdir())) == 1


def test_production_lfs_http_paths_use_post_basic_auth_and_exact_get(
        tmp_path, monkeypatch):
    payload = b"production transport bytes"
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)
    materialize.lfs_batch_getter = None
    materialize.lfs_object_fetcher = None
    monkeypatch.setenv("METARESEARCH_GITHUB_TOKEN", "test-token")
    batch_url = (
        "https://github.com/acme/model.git/info/lfs/objects/batch")
    object_url = f"https://objects.githubusercontent.com/lfs/{oid}?signed=1"
    batch_payload = _canonical({
        "transfer": "basic", "hash_algo": "sha256",
        "objects": [{
            "oid": oid, "size": len(payload),
            "actions": {"download": {
                "href": object_url, "header": {"X-Object-Token": "opaque"}}},
        }],
    })
    requests = []

    def batch_open(request, timeout):
        requests.append((request, timeout))
        return _HttpResponse(
            batch_payload, batch_url,
            content_type="application/json; charset=utf-8")

    materialize._lfs_batch_opener = batch_open
    actions, _evidence = materialize._lfs_batch_actions(
        repo.full_name, repo.revision,
        [{"oid": "sha256:" + oid, "size": len(payload)}])
    batch_request = requests[0][0]
    assert batch_request.get_method() == "POST"
    assert json.loads(batch_request.data) == {
        "operation": "download", "transfers": ["basic"],
        "objects": [{"oid": oid, "size": len(payload)}],
        "hash_algo": "sha256",
    }
    authorization = batch_request.get_header("Authorization")
    assert authorization.startswith("Basic ")
    assert base64.b64decode(authorization.removeprefix("Basic ")).decode() == (
        "x-access-token:test-token")

    def object_open(request, timeout):
        requests.append((request, timeout))
        return _HttpResponse(payload, object_url)

    materialize._lfs_object_opener = object_open
    destination = tmp_path / "object.bin"
    receipt = materialize._download_lfs_object(
        actions[oid], destination, oid=oid, size=len(payload))
    assert requests[1][0].get_method() == "GET"
    assert requests[1][0].get_header("X-object-token") == "opaque"
    assert receipt == {
        "url": object_url, "bytes": len(payload),
        "sha256": "sha256:" + oid,
    }
    assert destination.read_bytes() == payload


def test_lfs_batch_endpoint_404_is_retryable_transport_failure(tmp_path):
    payload = b"batch endpoint failure"
    _pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo("acme/model", "a" * 40, _repo_files())
    provider = _Provider([repo], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)
    materialize.lfs_batch_getter = None

    def missing_endpoint(_request, timeout):
        raise urllib.error.HTTPError(
            "https://github.com/acme/model.git/info/lfs/objects/batch",
            404, "missing", {}, io.BytesIO(b"not found"))

    materialize._lfs_batch_opener = missing_endpoint
    with pytest.raises(RepositoryTransportError, match="Batch HTTP 404"):
        materialize._lfs_batch_actions(
            repo.full_name, repo.revision,
            [{"oid": "sha256:" + oid, "size": len(payload)}])


def test_lfs_redirect_rejects_cross_origin_headers_and_unknown_host():
    handler = _LfsObjectRedirectHandler([
        "objects.githubusercontent.com", "objects-origin.githubusercontent.com"])
    credential_request = urllib.request.Request(
        "https://objects.githubusercontent.com/lfs/object",
        headers={
            "Authorization": "opaque", "Cookie": "secret=1",
            "X-Object-Token": "bounded",
        })

    with pytest.raises(urllib.error.HTTPError, match="cross-origin"):
        handler.redirect_request(
            credential_request, None, 302, "redirect", {},
            "https://objects-origin.githubusercontent.com/lfs/object")
    headerless = urllib.request.Request(
        "https://objects.githubusercontent.com/lfs/object")
    redirected = handler.redirect_request(
        headerless, None, 302, "redirect", {},
        "https://objects-origin.githubusercontent.com/lfs/object")
    assert not redirected.headers
    with pytest.raises(urllib.error.HTTPError, match="allowlist"):
        handler.redirect_request(
            headerless, None, 302, "redirect", {},
            "https://metadata.google.internal/lfs/object")


def test_duplicate_lfs_oid_is_downloaded_once_but_bound_to_each_path(tmp_path):
    payload = b"shared LFS bytes"
    pointer, oid = _lfs_pointer(payload)
    files = _repo_files(artifact=pointer)
    files["second.bin"] = pointer
    repo = _FrozenRepo("acme/model", "a" * 40, files)
    provider = _Provider([repo], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)

    result = materialize(_candidate(repo))

    assert provider.lfs_batch_calls == 1
    assert provider.lfs_object_calls == 1
    assert [(item["path"], item["oid"]) for item in
            result["supply_chain"]["lfs_objects"]] == [
                ("artifact.bin", "sha256:" + oid),
                ("second.bin", "sha256:" + oid),
            ]


def test_archive_embedded_lfs_object_is_bound_to_git_pointer(tmp_path):
    payload = b"archive-expanded LFS object" * 8
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    repo.archive_overrides["artifact.bin"] = payload
    provider = _Provider([repo], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)

    result = materialize(_candidate(repo))

    assert (Path(result["source_tree"]) / "artifact.bin").read_bytes() == payload
    assert result["supply_chain"]["lfs_objects"][0]["oid"] == "sha256:" + oid
    assert provider.lfs_batch_calls == 0
    assert provider.lfs_object_calls == 0


def test_archive_lfs_git_blob_response_drift_is_retryable(tmp_path):
    payload = b"archive-expanded object"
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    repo.archive_overrides["artifact.bin"] = payload
    provider = _Provider([repo], lfs_objects={oid: payload})
    provider.blob_mutator = lambda response: {**response, "encoding": "utf-8"}
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryTransportError, match="blob response"):
        materialize(_candidate(repo))


def test_submodule_lfs_identity_uses_child_repository_and_prefixed_path(tmp_path):
    payload = b"child LFS payload"
    pointer, oid = _lfs_pointer(payload)
    child = _FrozenRepo("acme/child", "b" * 40, {
        "model.bin": pointer, "LICENSE": b"MIT license evidence\n"})
    root_files = _repo_files()
    root_files[".gitmodules"] = (
        b'[submodule "child"]\n\tpath = deps/child\n'
        b'\turl = https://github.com/acme/child.git\n')
    root = _FrozenRepo(
        "acme/model", "a" * 40, root_files,
        gitlinks={"deps/child": child.revision})
    provider = _Provider([root, child], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)

    result = materialize(_candidate(root))

    assert result["supply_chain"]["lfs_objects"] == [{
        "path": "deps/child/model.bin", "repository": child.full_name,
        "revision": child.revision, "oid": "sha256:" + oid,
        "size": len(payload), "pointer_sha256": _sha256(pointer),
        "pointer_bytes": len(pointer),
        "pointer_git_blob_sha1": _git_blob_sha1(pointer),
    }]
    assert provider.lfs_batch_repositories == [child.full_name]


def test_same_lfs_oid_is_batch_verified_for_each_repository(tmp_path):
    payload = b"cross-repository shared object"
    pointer, oid = _lfs_pointer(payload)
    child = _FrozenRepo("acme/child", "b" * 40, {
        "model.bin": pointer, "LICENSE": b"MIT license evidence\n"})
    root_files = _repo_files(artifact=pointer)
    root_files[".gitmodules"] = (
        b'[submodule "child"]\n\tpath = deps/child\n'
        b'\turl = https://github.com/acme/child.git\n')
    root = _FrozenRepo(
        "acme/model", "a" * 40, root_files,
        gitlinks={"deps/child": child.revision})
    provider = _Provider([root, child], lfs_objects={oid: payload})
    materialize, _work = _materializer(tmp_path, provider)

    result = materialize(_candidate(root))

    assert len(result["supply_chain"]["lfs_objects"]) == 2
    assert provider.lfs_batch_repositories == [root.full_name, child.full_name]
    assert provider.lfs_object_calls == 1


@pytest.mark.parametrize(("mutation", "match"), [
    (lambda response: {**response, "transfer": "ssh"}, "transfer"),
    (lambda response: {
        **response,
        "objects": [{
            **response["objects"][0],
            "size": response["objects"][0]["size"] + 1,
        }],
    }, "identity/size"),
    (lambda response: {
        **response,
        "objects": [{
            **response["objects"][0],
            "actions": {"download": {
                "href": "https://metadata.google.internal/secret"}},
        }],
    }, "传输合同"),
    (lambda response: {
        **response,
        "objects": [{
            **response["objects"][0],
            "actions": {"download": {
                "href": response["objects"][0]["actions"]["download"]["href"],
                "header": {"Host": "metadata.google.internal"}}},
        }],
    }, "传输合同"),
])
def test_lfs_batch_protocol_drift_is_rejected(tmp_path, mutation, match):
    payload = b"batch protocol object"
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})
    provider.lfs_batch_mutator = mutation
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryTransportError, match=match):
        materialize(_candidate(repo))


def test_lfs_download_hash_mismatch_is_retryable_transport_failure(tmp_path):
    payload = b"correct LFS object"
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})
    provider.lfs_object_overrides[oid] = b"x" * len(payload)
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryTransportError, match="OID|receipt|bytes"):
        materialize(_candidate(repo))


@pytest.mark.parametrize(("code", "error_type"), [
    (404, RepositoryMaterializationError),
    (422, RepositoryTransportError),
    (503, RepositoryTransportError),
])
def test_lfs_batch_per_object_error_classification(tmp_path, code, error_type):
    payload = b"missing or unavailable object"
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})

    def fail(response):
        item = response["objects"][0]
        return {**response, "objects": [{
            "oid": item["oid"], "size": item["size"],
            "error": {"code": code, "message": "bounded failure"},
        }]}

    provider.lfs_batch_mutator = fail
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(error_type, match=str(code)):
        materialize(_candidate(repo))


def test_malformed_lfs_like_pointer_is_not_treated_as_regular_payload(tmp_path):
    pointer = (b"version https://git-lfs.github.com/spec/v1\n"
               b"oid sha256:not-a-real-oid\nsize 10\n")
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo])
    materialize, _work = _materializer(tmp_path, provider)

    with pytest.raises(RepositoryMaterializationError, match="LFS-like pointer"):
        materialize(_candidate(repo))


def test_lfs_actual_bytes_count_against_repository_total_limit(tmp_path):
    payload = b"large-but-bounded" * 32
    pointer, oid = _lfs_pointer(payload)
    repo = _FrozenRepo(
        "acme/model", "a" * 40, _repo_files(artifact=pointer))
    provider = _Provider([repo], lfs_objects={oid: payload})
    config = copy.deepcopy(POLICY["import_materialization"])
    non_artifact_bytes = sum(
        len(value) for key, value in repo.files.items() if key != "artifact.bin")
    config["max_total_bytes"] = non_artifact_bytes + len(payload) - 1
    materialize, _work = _materializer(tmp_path, provider, config=config)

    with pytest.raises(RepositoryMaterializationError, match="含 LFS 后总 bytes"):
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
