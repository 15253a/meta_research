"""Exact GitHub commit materialization into a content-addressed host snapshot.

Discovery freezes repository metadata and a 40-hex commit, but deliberately
does not clone or execute it.  This module closes the production hand-off:

* obtain the commit's root tree and every subtree through the read-only Git
  database API, rejecting truncated/ambiguous trees;
* download a commit-addressed source archive, extract it without ``tarfile``
  path/link semantics, and match every regular file to its Git blob SHA-1;
* recursively materialize pinned GitHub submodules under an explicit license
  policy and replace Git LFS pointers only after Batch/OID/size verification;
* validate the repository's declarative adapter v2, allocate stable numeric
  protocol/metric identities, and generate the full supply-chain manifest; and
* atomically publish a file-backed, read-only content-addressed snapshot plus
  a deterministic candidate index.  Database rows contain only bounded hashes.

Network calls are read-only and occur outside SQLite transactions.  A killed
download can be repeated; only a fully verified snapshot receives a durable
index and may enter the adversarial execution sandbox.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import secrets
import shutil
import stat
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .repository_materialization_common import (
    _ADAPTER_VERSION, _ARTIFACT_TYPES, _COMMIT_RE, _CONTROL_ENV_KEYS,
    _FULL_NAME_RE, _GITHUB_URI_RE, _LFS_VERSION, _LOG_KEY_RE,
    _MAX_ADAPTER_BYTES, _MAX_GITMODULES_BYTES, _MAX_RECEIPT_BYTES,
    _MAX_SEARCH_SNAPSHOT_BYTES, _PROTOCOL, _SHA256_RE,
    RepositoryCacheError, RepositoryMaterializationError, RepositoryTransportError,
    _atomic_write_json, _bounded_string, _canonical, _fsync_directory,
    _git_blob_sha1, _git_tree_sha1, _parse_lfs_pointer, _positive_int,
    _remove_private_tree, _safe_component, _safe_relpath, _sha256, _stable_id,
    _strict_json, _value_hash,
)
from .repository_materializer_adapter import _RepositoryAdapterMixin
from .repository_materializer_archive import _RepositoryArchiveMixin
from .repository_materializer_lfs import (
    _LfsBatchRedirectHandler, _LfsObjectRedirectHandler, _RepositoryLfsMixin,
)
from .repository_materializer_transport import (
    _ApiRedirectHandler, _ArchiveRedirectHandler, _RepositoryTransportMixin,
)
from .repository_materializer_tree import _RepositoryTreeMixin
from .repository_materializer_store import _RepositoryStoreMixin


class GitHubRepositoryMaterializer(
        _RepositoryTransportMixin, _RepositoryTreeMixin, _RepositoryLfsMixin,
        _RepositoryArchiveMixin, _RepositoryAdapterMixin, _RepositoryStoreMixin):
    """Materialize one exact GitHub commit and repository adapter v2."""

    name = "github_archive_v1"

    def __init__(
            self, *, work_root: Path | str, config: Mapping[str, Any],
            sandbox_config: Mapping[str, Any], auto_license: Mapping[str, Any],
            runtime_environment: Mapping[str, str],
            owner_guard: Optional[Callable[[], None]] = None,
            api_getter: Optional[Callable[[str, str], Any]] = None,
            archive_fetcher: Optional[
                Callable[[str, str, Path, int], Mapping[str, Any]]] = None,
            lfs_batch_getter: Optional[
                Callable[[str, str, Mapping[str, Any]], Any]] = None,
            lfs_object_fetcher: Optional[
                Callable[[str, Mapping[str, str], Path, int],
                         Mapping[str, Any]]] = None,
            token_env: str = "METARESEARCH_GITHUB_TOKEN"):
        self.work_root = Path(os.path.abspath(os.fspath(work_root)))
        self.config = dict(config)
        self.sandbox_config = dict(sandbox_config)
        self.auto_license = dict(auto_license)
        self.runtime_environment = dict(runtime_environment)
        self.owner_guard = owner_guard or (lambda: None)
        self.token_env = token_env
        self._validate_config()
        self.api_getter = api_getter
        self.archive_fetcher = archive_fetcher
        self.lfs_batch_getter = lfs_batch_getter
        self.lfs_object_fetcher = lfs_object_fetcher
        self._api_opener = urllib.request.build_opener(_ApiRedirectHandler()).open
        self._archive_opener = urllib.request.build_opener(
            _ArchiveRedirectHandler(self.config["allowed_archive_hosts"])).open
        self._lfs_batch_opener = urllib.request.build_opener(
            _LfsBatchRedirectHandler()).open
        self._lfs_object_opener = urllib.request.build_opener(
            _LfsObjectRedirectHandler(self.config["allowed_lfs_hosts"])).open

    def _validate_config(self) -> None:
        required = {
            "provider", "adapter_path", "timeout_s", "max_api_response_bytes",
            "max_archive_bytes", "max_file_bytes", "max_total_bytes", "max_files",
            "max_tree_depth", "max_tree_objects", "max_submodules", "lfs_policy",
            "max_lfs_objects", "lfs_batch_size", "allowed_archive_hosts",
            "allowed_lfs_hosts", "dependency_lock_names", "compiler",
        }
        if set(self.config) != required or self.config.get("provider") != self.name:
            raise ValueError("policy.import_materialization 字段闭包/provider 非法")
        if self.config.get("adapter_path") != ".meta-research/import-adapter.json":
            raise ValueError("import adapter_path 非冻结 v2 路径")
        bounds = {
            "max_api_response_bytes": (65536, 16777216),
            "max_archive_bytes": (1048576, 68719476736),
            "max_file_bytes": (1, 17179869184),
            "max_total_bytes": (1, 1099511627776),
            "max_files": (1, 100000),
            "max_tree_depth": (1, 128),
            "max_tree_objects": (1, 100000),
            "max_submodules": (0, 256),
            "max_lfs_objects": (0, 100000),
            "lfs_batch_size": (1, 100),
        }
        for key, (minimum, maximum) in bounds.items():
            value = self.config[key]
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not minimum <= value <= maximum):
                raise ValueError(
                    f"import_materialization.{key} 越出封闭边界")
        timeout = self.config["timeout_s"]
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or not 1 <= float(timeout) <= 300):
            raise ValueError("import_materialization.timeout_s 非正有限数")
        # The checked-in production schema requires ``fetch``.  Keep ``reject``
        # as a fail-closed constructor compatibility mode for frozen older
        # policies and isolated tests; it can never enable an unverified object.
        if self.config["lfs_policy"] not in ("reject", "fetch"):
            raise ValueError("import_materialization.lfs_policy 非封闭值")
        for key, minimum, maximum in (
                ("allowed_archive_hosts", 1, 8),
                ("allowed_lfs_hosts", 1, 32)):
            hosts = self.config[key]
            if (not isinstance(hosts, list) or not minimum <= len(hosts) <= maximum
                    or len(set(hosts)) != len(hosts)
                    or any(not isinstance(host, str) or host != host.lower()
                           or re.fullmatch(
                               r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host) is None
                           for host in hosts)):
                raise ValueError(
                    f"import_materialization {key} host allowlist 非法")
        locks = self.config["dependency_lock_names"]
        if (not isinstance(locks, list) or not 1 <= len(locks) <= 64
                or len(set(locks)) != len(locks)
                or any(not isinstance(name, str)
                       or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", name) is None
                       for name in locks)):
            raise ValueError("import_materialization dependency_lock_names 非法")
        compiler = self.config["compiler"]
        if (not isinstance(compiler, dict)
                or set(compiler) != {"implementation", "version", "artifact_sha256"}
                or compiler.get("implementation") != "CPython"
                or not isinstance(compiler.get("version"), str)
                or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", compiler["version"]) is None
                or not isinstance(compiler.get("artifact_sha256"), str)
                or _SHA256_RE.fullmatch(compiler["artifact_sha256"]) is None):
            raise ValueError("import_materialization.compiler identity 非法")
        if (not isinstance(self.auto_license.get("allow_spdx"), list)
                or not isinstance(self.auto_license.get("scope"), dict)):
            raise ValueError("import materializer auto_license 非法")
        for key in ("image", "image_id", "python_path"):
            if key not in self.sandbox_config:
                raise ValueError(f"sandbox config 缺 {key}")
        compiler = self.config["compiler"]
        expected_runtime = {
            "PYTHON_VERSION": compiler["version"],
            "PYTHON_SHA256": compiler["artifact_sha256"].removeprefix("sha256:"),
        }
        if (not all(isinstance(key, str) and isinstance(value, str)
                    for key, value in self.runtime_environment.items())
                or any(self.runtime_environment.get(key) != value
                       for key, value in expected_runtime.items())):
            raise ValueError(
                "pinned sandbox image compiler environment 与 import policy 不一致")

    @property
    def environment_hash(self) -> str:
        return _sha256(_canonical(self.sandbox_config))

    @property
    def config_hash(self) -> str:
        return _value_hash({
            "materialization": self.config,
            "sandbox": self.sandbox_config,
            "auto_license": self.auto_license,
        })

    def _validate_search_snapshot(
            self, snapshot: Any, *, repository: str, revision: str,
            canonical_uri: str) -> Dict[str, Any]:
        if not isinstance(snapshot, dict) or snapshot.get("version") not in (1, 2):
            raise RepositoryMaterializationError(
                "candidate search snapshot version 非法")
        expected = {
            "version", "provider", "query", "provider_result_id", "retrieved_at",
            "ranking", "repository", "canonical_uri", "revision", "license",
            "policy_hash",
        }
        if snapshot["version"] == 2:
            expected.add("source_authority_hash")
        if set(snapshot) != expected or snapshot.get("provider") != "github_rest_v1":
            raise RepositoryMaterializationError(
                "candidate search snapshot 字段闭包/provider 非法")
        for field, maximum in (
                ("query", 8192), ("provider_result_id", 128),
                ("retrieved_at", 128)):
            _bounded_string(snapshot[field], field=f"snapshot.{field}", max_bytes=maximum)
        ranking = snapshot["ranking"]
        if (not isinstance(ranking, dict)
                or set(ranking) != {"rank", "recipe", "scale"}
                or isinstance(ranking.get("rank"), bool)
                or not isinstance(ranking.get("rank"), int)
                or ranking["rank"] < 0):
            raise RepositoryMaterializationError("snapshot.ranking 非法")
        _bounded_string(ranking["recipe"], field="snapshot.ranking.recipe", max_bytes=512)
        _bounded_string(ranking["scale"], field="snapshot.ranking.scale", max_bytes=128)
        repo = snapshot["repository"]
        if (not isinstance(repo, dict)
                or set(repo) != {"full_name", "default_branch", "stars", "updated_at"}
                or repo.get("full_name") != repository
                or isinstance(repo.get("stars"), bool)
                or not isinstance(repo.get("stars"), int) or repo["stars"] < 0):
            raise RepositoryMaterializationError("snapshot.repository 权威非法")
        _bounded_string(
            repo["default_branch"], field="snapshot.repository.default_branch",
            max_bytes=512)
        _bounded_string(
            repo["updated_at"], field="snapshot.repository.updated_at",
            max_bytes=128)
        if (snapshot.get("canonical_uri") != canonical_uri
                or snapshot.get("revision") != revision
                or not isinstance(snapshot.get("policy_hash"), str)
                or _SHA256_RE.fullmatch(snapshot["policy_hash"]) is None):
            raise RepositoryMaterializationError(
                "candidate search snapshot repository/revision/policy 权威不一致")
        if snapshot["version"] == 2:
            authority_hash = snapshot["source_authority_hash"]
            if authority_hash is not None and (
                    not isinstance(authority_hash, str)
                    or _SHA256_RE.fullmatch(authority_hash) is None):
                raise RepositoryMaterializationError(
                    "snapshot.source_authority_hash 非法")
        license_value = snapshot["license"]
        if (not isinstance(license_value, dict)
                or set(license_value) != {
                    "spdx_id", "lookup_status", "evidence_ref", "content_sha256"}
                or not isinstance(license_value.get("spdx_id"), str)
                or re.fullmatch(r"(?:[A-Za-z0-9.+-]{1,128}|NOASSERTION)",
                                license_value["spdx_id"]) is None
                or license_value.get("lookup_status") not in ("found", "missing")):
            raise RepositoryMaterializationError("snapshot.license 非法")
        evidence = license_value.get("evidence_ref")
        if not isinstance(evidence, str):
            raise RepositoryMaterializationError("snapshot.license.evidence_ref 非法")
        parsed_evidence = urllib.parse.urlsplit(evidence)
        evidence_query = urllib.parse.parse_qs(
            parsed_evidence.query, keep_blank_values=True)
        if (parsed_evidence.scheme != "https"
                or parsed_evidence.hostname != "api.github.com"
                or parsed_evidence.username or parsed_evidence.password
                or parsed_evidence.port not in (None, 443)
                or not parsed_evidence.path.startswith(f"/repos/{repository}/")
                or parsed_evidence.fragment
                or set(evidence_query) != {"ref"}
                or evidence_query.get("ref") != [revision]):
            raise RepositoryMaterializationError(
                "snapshot.license.evidence_ref 未绑定同一 commit")
        content_hash = license_value.get("content_sha256")
        if ((license_value["lookup_status"] == "found")
                != (isinstance(content_hash, str)
                    and _SHA256_RE.fullmatch(content_hash) is not None)):
            raise RepositoryMaterializationError(
                "snapshot.license lookup/content hash 矛盾")
        if license_value["lookup_status"] == "missing" and content_hash is not None:
            raise RepositoryMaterializationError(
                "snapshot.license missing 不得带 content hash")
        normalized_license = dict(license_value)
        if license_value["lookup_status"] == "found":
            prefix = f"/repos/{repository}/contents/"
            if not parsed_evidence.path.startswith(prefix):
                raise RepositoryMaterializationError(
                    "snapshot.license found evidence 非 commit contents path")
            encoded_path = parsed_evidence.path[len(prefix):]
            try:
                repository_path = urllib.parse.unquote_to_bytes(
                    encoded_path).decode("utf-8")
            except UnicodeDecodeError as error:
                raise RepositoryMaterializationError(
                    "snapshot.license repository path 非 UTF-8") from error
            normalized_license["repository_path"] = _safe_relpath(
                repository_path, field="snapshot.license repository path",
                max_depth=int(self.config["max_tree_depth"]))
        else:
            if parsed_evidence.path != f"/repos/{repository}/license":
                raise RepositoryMaterializationError(
                    "snapshot.license missing evidence 非 pinned license endpoint")
            normalized_license["repository_path"] = None
        return normalized_license

    def __call__(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        self.owner_guard()
        required_candidate = {
            "id", "question_id", "canonical_uri", "revision", "source_kind",
            "search_snapshot_json", "search_snapshot_hash",
        }
        if not isinstance(candidate, dict) or set(candidate) != required_candidate:
            raise RepositoryMaterializationError("repository candidate 字段闭包非法")
        if (isinstance(candidate["id"], bool) or not isinstance(candidate["id"], int)
                or candidate["id"] <= 0
                or isinstance(candidate["question_id"], bool)
                or not isinstance(candidate["question_id"], int)
                or candidate["question_id"] <= 0
                or candidate["source_kind"] != "repo"):
            raise RepositoryMaterializationError("repository candidate id/source_kind 非法")
        uri_match = (_GITHUB_URI_RE.fullmatch(candidate["canonical_uri"])
                     if isinstance(candidate["canonical_uri"], str) else None)
        if uri_match is None:
            raise RepositoryMaterializationError("repository candidate URI 非规范 GitHub URI")
        repository = uri_match.group(1)
        revision = candidate["revision"]
        if not isinstance(revision, str) or _COMMIT_RE.fullmatch(revision) is None:
            raise RepositoryMaterializationError("repository candidate revision 非 40-hex commit")
        raw_snapshot = candidate["search_snapshot_json"]
        if not isinstance(raw_snapshot, str):
            raise RepositoryMaterializationError("candidate search snapshot 非字符串")
        try:
            snapshot_bytes = raw_snapshot.encode("utf-8")
        except UnicodeEncodeError as error:
            raise RepositoryMaterializationError(
                "candidate search snapshot 非合法 UTF-8") from error
        if len(snapshot_bytes) > _MAX_SEARCH_SNAPSHOT_BYTES:
            raise RepositoryMaterializationError(
                "candidate search snapshot 超过物化上限")
        if (not isinstance(candidate["search_snapshot_hash"], str)
                or _SHA256_RE.fullmatch(candidate["search_snapshot_hash"]) is None
                or _sha256(snapshot_bytes) != candidate["search_snapshot_hash"]):
            raise RepositoryMaterializationError("candidate search snapshot hash 不一致")
        snapshot = _strict_json(snapshot_bytes, label="candidate search snapshot")
        root_license = self._validate_search_snapshot(
            snapshot, repository=repository, revision=revision,
            canonical_uri=candidate["canonical_uri"])
        identity = {
            "candidate_id": candidate["id"], "canonical_uri": candidate["canonical_uri"],
            "revision": revision, "search_snapshot_hash": candidate["search_snapshot_hash"],
            "config_hash": self.config_hash, "environment_hash": self.environment_hash,
        }
        identity_hash = _value_hash(identity).removeprefix("sha256:")
        self._authority_directories()
        reused = self._load_index(candidate, identity_hash)
        if reused is not None:
            return reused

        _base, staging_parent, objects, indexes = self._authority_directories()
        for stale in staging_parent.glob(identity_hash + ".*"):
            stale_info = os.lstat(stale)
            if (not stat.S_ISDIR(stale_info.st_mode)
                    or stat.S_ISLNK(stale_info.st_mode)
                    or stale_info.st_uid != os.geteuid()):
                raise RepositoryCacheError(
                    "stale materialization authority 非法")
            _remove_private_tree(stale)
        staging = staging_parent / f"{identity_hash}.{secrets.token_hex(8)}"
        staging.mkdir(mode=0o700)
        tree_root = staging / "tree"
        downloads = staging / "downloads"
        tree_root.mkdir(mode=0o700)
        downloads.mkdir(mode=0o700)
        try:
            ledger: list[Dict[str, Any]] = []
            sources: list[Dict[str, Any]] = []
            submodules: list[Dict[str, Any]] = []
            root_tree_sha = self._snapshot_repo(
                full_name=repository, revision=revision, prefix="",
                tree_root=tree_root, downloads=downloads,
                seen_repositories=set(), all_ledger=ledger,
                sources=sources, submodule_records=submodules,
                tree_counter=[0], global_lfs_objects={}, is_root=True)
            if not sources or sources[0].get("repository") != repository:
                raise RepositoryMaterializationError(
                    "root repository source record 缺失")
            if root_license["repository_path"] is not None:
                root_license_matches = [
                    item for item in ledger
                    if (item["path"] == root_license["repository_path"]
                        and item["repository"] == repository
                        and item["revision"] == revision)]
                if (len(root_license_matches) != 1
                        or "lfs" in root_license_matches[0]
                        or root_license_matches[0]["sha256"]
                        != root_license["content_sha256"]):
                    raise RepositoryMaterializationError(
                        "root license evidence 与 commit 文件 ledger 不一致")
            sources[0]["license"] = root_license
            ledger.sort(key=lambda item: item["path"])
            if len({item["path"] for item in ledger}) != len(ledger):
                raise RepositoryMaterializationError(
                    "recursive repository 文件路径冲突")
            if len(ledger) > int(self.config["max_files"]):
                raise RepositoryMaterializationError("repository 总文件数超过 policy")
            total = sum(item["bytes"] for item in ledger)
            if total > int(self.config["max_total_bytes"]):
                raise RepositoryMaterializationError("repository 总 bytes 超过 policy")
            spec = self._adapter_spec(
                tree_root=tree_root, ledger=ledger, repository=repository,
                revision=revision, root_tree_sha=root_tree_sha,
                sources=sources, submodules=submodules)
            file_ledger_hash = _value_hash(ledger)
            spec_hash = _value_hash(spec)
            transport_evidence_hash = _value_hash(sources)
            snapshot_identity = {
                "protocol": _PROTOCOL, "repository": repository,
                "revision": revision, "root_tree_sha1": root_tree_sha,
                "file_ledger_hash": file_ledger_hash,
                "spec_hash": spec_hash,
                "config_hash": self.config_hash,
            }
            object_hash = _value_hash(snapshot_identity)
            receipt = {
                "protocol": _PROTOCOL, "version": 1,
                "repository": repository, "revision": revision,
                "root_tree_sha1": root_tree_sha, "object_hash": object_hash,
                "config_hash": self.config_hash,
                "environment_hash": self.environment_hash,
                "file_count": len(ledger), "total_bytes": total,
                "file_ledger_hash": file_ledger_hash,
                "spec_hash": spec_hash,
                "transport_evidence_hash": transport_evidence_hash,
            }
            shutil.rmtree(downloads)
            self.owner_guard()
            _atomic_write_json(
                staging / "ledger.json", ledger, maximum=_MAX_RECEIPT_BYTES)
            _atomic_write_json(
                staging / "spec.json", spec, maximum=16 * 1024 * 1024)
            _atomic_write_json(
                staging / "transport.json", sources, maximum=4 * 1024 * 1024)
            _atomic_write_json(
                staging / "receipt.json", receipt, maximum=256 * 1024)
            object_path = objects / object_hash.removeprefix("sha256:")
            if os.path.lexists(object_path):
                self.owner_guard()
                existing = self._verify_published(
                    object_path=object_path, repository=repository,
                    revision=revision)
                _remove_private_tree(staging)
            else:
                for current, dirs, files in os.walk(
                        tree_root, topdown=False, followlinks=False):
                    for name in files:
                        path = Path(current) / name
                        mode = os.lstat(path).st_mode
                        os.chmod(path, 0o555 if mode & 0o111 else 0o444)
                    for name in dirs:
                        os.chmod(Path(current) / name, 0o555)
                    os.chmod(Path(current), 0o555)
                    _fsync_directory(Path(current))
                    self.owner_guard()
                self.owner_guard()
                try:
                    os.replace(staging, object_path)
                except OSError as error:
                    if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                        raise
                    # Another publisher won after the existence check.  Only
                    # exact verified content is reusable; our private staging
                    # tree remains ours to remove.
                    existing = self._verify_published(
                        object_path=object_path, repository=repository,
                        revision=revision)
                    _remove_private_tree(staging)
                    _fsync_directory(staging_parent)
                else:
                    _fsync_directory(objects)
                    _fsync_directory(staging_parent)
                    existing = self._verify_published(
                        object_path=object_path, repository=repository,
                        revision=revision)
            self.owner_guard()
            _atomic_write_json(
                indexes / f"{identity_hash}.json",
                {"version": 1, **identity, "object_hash": object_hash},
                maximum=128 * 1024)
            return existing
        except BaseException:
            if os.path.lexists(staging):
                try:
                    _remove_private_tree(staging)
                except OSError:
                    pass
            raise

class ProductionCandidateFetcher:
    """Preserve registered legacy snapshots while enabling real GitHub candidates."""

    def __init__(self, *, legacy_fetcher, repository_fetcher):
        if not callable(legacy_fetcher) or not callable(repository_fetcher):
            raise ValueError(
                "ProductionCandidateFetcher requires callable legacy/repository fetchers")
        self.legacy_fetcher = legacy_fetcher
        self.repository_fetcher = repository_fetcher

    def __call__(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        raw = candidate.get("search_snapshot_json")
        if isinstance(raw, str):
            try:
                snapshot = json.loads(raw)
            except json.JSONDecodeError:
                snapshot = None
            if isinstance(snapshot, dict) and "materialization" in snapshot:
                return self.legacy_fetcher(candidate)
        return self.repository_fetcher(candidate)
