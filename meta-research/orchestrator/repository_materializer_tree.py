"""Exact Git object traversal and tree identity verification."""
from __future__ import annotations

from typing import Any, Dict

from .repository_materialization_common import (
    _COMMIT_RE, RepositoryMaterializationError, _git_tree_sha1,
    _safe_component,
)


class _RepositoryTreeMixin:
    """Host contract: config plus bounded ``_get_json`` transport."""

    def _commit_tree(self, full_name: str, revision: str) -> str:
        payload = self._get_json(
            f"https://api.github.com/repos/{full_name}/git/commits/{revision}",
            label=f"commit {full_name}@{revision}")
        tree = payload.get("tree") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("sha") != revision
                or not isinstance(tree, dict)
                or not isinstance(tree.get("sha"), str)
                or _COMMIT_RE.fullmatch(tree["sha"]) is None):
            raise RepositoryMaterializationError(
                f"GitHub commit {full_name}@{revision} identity/tree 非法")
        return tree["sha"]

    def _walk_tree(
            self, *, full_name: str, tree_sha: str, prefix: str,
            depth: int, files: Dict[str, Dict[str, Any]],
            submodules: list[Dict[str, Any]], tree_counter: list[int]) -> None:
        if depth > int(self.config["max_tree_depth"]):
            raise RepositoryMaterializationError("Git tree 深度超过 policy")
        tree_counter[0] += 1
        if tree_counter[0] > int(self.config["max_tree_objects"]):
            raise RepositoryMaterializationError(
                "Git tree object 请求数超过 policy")
        payload = self._get_json(
            f"https://api.github.com/repos/{full_name}/git/trees/{tree_sha}",
            label=f"tree {full_name}:{tree_sha}")
        entries = payload.get("tree") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("sha") != tree_sha
                or payload.get("truncated") is not False
                or not isinstance(entries, list)):
            raise RepositoryMaterializationError(
                f"Git tree {full_name}:{tree_sha} 缺失/截断/身份错配")
        normalized = []
        names = set()
        for index, raw in enumerate(entries):
            if not isinstance(raw, dict):
                raise RepositoryMaterializationError("Git tree entry 非 object")
            name = _safe_component(raw.get("path"), field=f"tree[{index}].path")
            mode, kind, sha = raw.get("mode"), raw.get("type"), raw.get("sha")
            if (mode, kind) not in {
                    ("100644", "blob"), ("100755", "blob"),
                    ("040000", "tree"), ("160000", "commit"),
                    ("120000", "blob")}:
                raise RepositoryMaterializationError(
                    f"Git tree entry {name} mode/type 非法")
            if not isinstance(sha, str) or _COMMIT_RE.fullmatch(sha) is None:
                raise RepositoryMaterializationError(
                    f"Git tree entry {name} sha 非法")
            if name in names:
                raise RepositoryMaterializationError(
                    f"Git tree entry name 重复: {name}")
            names.add(name)
            size = raw.get("size")
            if kind == "blob" and mode != "120000" and (
                    isinstance(size, bool) or not isinstance(size, int)
                    or size < 0 or size > int(self.config["max_file_bytes"])):
                raise RepositoryMaterializationError(
                    f"Git blob {name} size 非法/超限")
            normalized.append({
                "name": name, "mode": mode, "type": kind, "sha": sha,
                "size": size,
            })
        if _git_tree_sha1(normalized) != tree_sha:
            raise RepositoryMaterializationError(
                f"Git tree {full_name}:{tree_sha} 对象 SHA 重算不一致")
        for entry in normalized:
            rel = f"{prefix}/{entry['name']}" if prefix else entry["name"]
            if entry["mode"] == "040000":
                self._walk_tree(
                    full_name=full_name, tree_sha=entry["sha"], prefix=rel,
                    depth=depth + 1, files=files, submodules=submodules,
                    tree_counter=tree_counter)
                continue
            if entry["mode"] == "160000":
                if len(submodules) >= int(self.config["max_submodules"]):
                    raise RepositoryMaterializationError("Git submodule 数超过 policy")
                submodules.append({"path": rel, "revision": entry["sha"]})
                continue
            if entry["mode"] == "120000":
                raise RepositoryMaterializationError(
                    f"repository 含 symlink {rel}；当前 snapshot capability 不接受")
            if len(files) >= int(self.config["max_files"]):
                raise RepositoryMaterializationError("repository 文件数超过 policy")
            files[rel] = {
                "git_blob_sha1": entry["sha"], "git_mode": entry["mode"],
                "declared_bytes": entry["size"], "repository": full_name,
            }
