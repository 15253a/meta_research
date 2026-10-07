from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
import shutil
import stat
from dataclasses import dataclass

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.semantic_mcp import SemanticCallContext, SemanticMcpError, SemanticOperation


_STAGES = ("idea", "plan", "bundle", "reasoning")
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_SCAN_FILES = 10000


@dataclass(frozen=True, slots=True)
class WorkspaceLocation:
    workspace_ref: str
    root_kind: str
    root_session_ref: str
    work_ref: str
    directory: Path
    quest_ref: str | None = None
    cycle_ref: str | None = None
    request_ref: str | None = None
    target_ref: str | None = None
    initialization_id: str | None = None
    context_generation: int | None = None

    def source(self) -> dict[str, object]:
        return {"workspace_ref": self.workspace_ref, "root_kind": self.root_kind,
                "root_session_ref": self.root_session_ref, "work_ref": self.work_ref,
                "quest_ref": self.quest_ref, "cycle_ref": self.cycle_ref,
                "request_ref": self.request_ref, "target_ref": self.target_ref,
                "initialization_id": self.initialization_id, "status": "working_material",
                **({"context_generation": self.context_generation}
                   if self.context_generation is not None else {})}


@dataclass(frozen=True, slots=True)
class WorkspaceBinding:
    location: WorkspaceLocation
    scope_hash: str

    @property
    def directory(self) -> Path:
        return self.location.directory

    def seal(self) -> dict[str, object]:
        return {**self.location.source(), "working_directory": str(self.directory)}


@dataclass(frozen=True, slots=True)
class WorkspaceDestination:
    location: WorkspaceLocation
    owner: str
    request_ref: str
    waiter_ref: str
    creation_context_kind: str | None = None


class RootWorkspaces:
    def __init__(self, *, advancement_engine, agent_runtime, human_collaboration,
                 research_graph, target_run_agent, bases: dict[str, Path],
                 deepfetch_locator=None) -> None:
        self._ae = advancement_engine
        self._ar = agent_runtime
        self._hc = human_collaboration
        self._rg = research_graph
        self._target = target_run_agent
        self._bases = {kind: path.absolute() for kind, path in bases.items()}
        self._deepfetch_locator = deepfetch_locator

    def bind_runtime(self, context: SemanticCallContext) -> WorkspaceBinding:
        try:
            facts = self._ar.verify_root_agent_runtime_scope(
                root_kind=context.root_kind, run_ref=context.run_ref,
                attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
                fence_ref=context.fence_ref,
                runtime_binding_hash=context.capability_binding_hash)
            if facts["run_kind"] == "target":
                location = self._target_location(facts["target_ref"])
                workspace_ref, directory = self._target.resolve_target_workspace(
                    target_ref=facts["target_ref"], target_run_ref=context.run_ref,
                    root_session_ref=context.root_session_ref, attempt_ref=context.attempt_ref,
                    fence_ref=context.fence_ref)
                if (workspace_ref, directory) != (location.workspace_ref, location.directory):
                    raise OwnerConflict("workspace_scope_stale")
            else:
                location = self._runtime_location(context.run_ref)
            if location.root_session_ref != context.root_session_ref:
                raise OwnerConflict("workspace_scope_stale")
            self._ensure_directory(location.directory)
            return WorkspaceBinding(location, canonical_hash({
                "run_ref": context.run_ref, "attempt_ref": context.attempt_ref,
                "root_session_ref": context.root_session_ref, "fence_ref": context.fence_ref,
                "runtime_binding_hash": context.capability_binding_hash}))
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    def _runtime_location(self, run_ref: str) -> WorkspaceLocation:
        managed = self._ar.query_managed_run(run_ref)
        if managed is None:
            raise OwnerConflict("workspace_work_missing")
        kind = managed["run_kind"].removesuffix("_stage")
        request_ref = None
        cycle_ref = None
        quest_ref = managed["quest_ref"]
        if kind in _STAGES:
            run = self._ar.query_stage_run_by_ref(run_ref)
            request = self._ae.query_stage_request_by_ref(run.request_ref)
            if (run.cycle_ref, run.stage, run.epoch) != (request.cycle_ref, request.stage, request.epoch):
                raise OwnerConflict("workspace_lineage_invalid")
            if request.accepted_question.quest_ref != quest_ref:
                raise OwnerConflict("workspace_lineage_invalid")
            request_ref, cycle_ref = request.request_ref, request.cycle_ref
        if kind == "acquisition":
            acquisition = self._ar.query_acquisition_session(session_ref=managed["root_session_ref"])
            if acquisition is not None:
                session = self.bind_acquisition_session(acquisition.session_ref).location
                return WorkspaceLocation(session.workspace_ref, kind, session.root_session_ref,
                    session.work_ref, session.directory, quest_ref=quest_ref, request_ref=run_ref,
                    initialization_id=session.initialization_id)
        if kind == "companion":
            facts = self._hc.query_companion_work_context(managed["root_session_ref"])
            if facts is not None:
                session = self.bind_companion_session(facts["scope_ref"], managed["root_session_ref"]).location
                if session.quest_ref != quest_ref:
                    raise OwnerConflict("workspace_lineage_invalid")
                return WorkspaceLocation(session.workspace_ref, kind, session.root_session_ref,
                    session.work_ref, session.directory, quest_ref=quest_ref, request_ref=run_ref)
        if kind == "deepfetch":
            run = self._ar.query_deepfetch_run_by_ref(run_ref)
            if self._deepfetch_locator is None:
                raise OwnerConflict("workspace_deepfetch_locator_unavailable")
            directory = self._deepfetch_locator(run.provider_operation_ref, run.runtime_binding_hash)
        else:
            base = self._bases.get(kind)
            if base is None:
                raise OwnerConflict("workspace_root_kind_unavailable")
            directory = base / canonical_hash({"work_ref": run_ref})
        return WorkspaceLocation("workspace:" + canonical_hash({"kind": kind, "work_ref": run_ref}),
            kind, managed["root_session_ref"], run_ref, directory,
            quest_ref, cycle_ref, request_ref)

    def _target_locations(self, target_ref: str, *, history: bool = False) -> tuple[WorkspaceLocation, ...]:
        launch = self._ar.query_admitted_target_launch(target_ref)
        if launch is None:
            raise OwnerConflict("workspace_target_launch_missing")
        bundle = self._ar.query_bundle_stage_run(launch.stage_request_ref)
        request = self._ae.query_stage_request_by_ref(launch.stage_request_ref)
        if (bundle is None or bundle.stage != "bundle" or request.stage != "bundle"
            or bundle.request_ref != request.request_ref
            or bundle.cycle_ref != request.cycle_ref
            or request.accepted_question.quest_ref != launch.quest_ref):
            raise OwnerConflict("workspace_lineage_invalid")
        facts = (self._target.read_target_workspace_locations(launch.target_run_ref) if history
                 else (self._target.read_target_workspace_location(launch.target_run_ref),))
        locations = []
        for workspace, directory in facts:
            if workspace.target_ref != target_ref or workspace.target_run_ref != launch.target_run_ref:
                raise OwnerConflict("workspace_lineage_invalid")
            locations.append(WorkspaceLocation(workspace.workspace_ref, "target", workspace.root_session_ref,
                workspace.target_run_ref, directory, launch.quest_ref, bundle.cycle_ref,
                launch.stage_request_ref, target_ref))
        return tuple(locations)

    def _target_location(self, target_ref: str) -> WorkspaceLocation:
        return self._target_locations(target_ref)[0]

    def bind_initialization(self, initialization_id: str, root_session_ref: str) -> WorkspaceBinding:
        creation = self._hc.query_quest_creation(initialization_id)
        session = creation.get("intent_session")
        if not isinstance(session, dict) or session.get("ref") != root_session_ref:
            raise SemanticMcpError("workspace_initialization_scope_invalid")
        location = WorkspaceLocation("workspace:" + canonical_hash({"initialization_id": initialization_id,
            "root_session_ref": root_session_ref}), "companion", root_session_ref,
            root_session_ref, self._bases["companion"] / canonical_hash({
                "initialization_id": initialization_id, "root_session_ref": root_session_ref}),
            initialization_id=initialization_id)
        self._ensure_directory(location.directory)
        return WorkspaceBinding(location, canonical_hash({"owner": "human_collaboration",
            "initialization_id": initialization_id, "root_session_ref": root_session_ref}))

    def destination_for_initialization(self, initialization_id: str, root_session_ref: str) -> WorkspaceDestination:
        return WorkspaceDestination(self.bind_initialization(initialization_id, root_session_ref).location,
            "human_collaboration", initialization_id, root_session_ref, "quest_initialization")

    def bind_manual_creation(self, context_ref: str, root_session_ref: str,
                             context_generation: int) -> WorkspaceBinding:
        return self._manual_creation_binding(context_ref, root_session_ref, context_generation,
            require_open=True)

    def _manual_creation_binding(self, context_ref: str, root_session_ref: str,
                                 context_generation: int, *, require_open: bool) -> WorkspaceBinding:
        if (not isinstance(context_ref, str) or not context_ref
            or not isinstance(root_session_ref, str) or not root_session_ref
            or type(context_generation) is not int or context_generation < 1):
            raise SemanticMcpError("workspace_manual_creation_scope_invalid")
        try:
            creation = self._hc.query_manual_question_creation(context_ref)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
        session = creation.get("drafting_session")
        if (creation.get("context_ref") != context_ref
            or creation.get("generation") != context_generation
            or not isinstance(session, dict) or session.get("ref") != root_session_ref
            or require_open and session.get("status") != "open"):
            raise SemanticMcpError("workspace_manual_creation_scope_invalid")
        identity = {"creation_context_ref": context_ref, "root_session_ref": root_session_ref}
        location = WorkspaceLocation("workspace:" + canonical_hash(identity), "companion",
            root_session_ref, context_ref, self._bases["companion"] / canonical_hash(identity),
            quest_ref=creation["quest_ref"], request_ref=context_ref,
            initialization_id=creation["quest_initialization_id"],
            context_generation=context_generation)
        self._ensure_directory(location.directory)
        return WorkspaceBinding(location, canonical_hash({"owner": "human_collaboration",
            **identity, "context_generation": context_generation}))

    def destination_for_manual_creation(self, context_ref: str, root_session_ref: str,
                                        context_generation: int) -> WorkspaceDestination:
        return WorkspaceDestination(self._manual_creation_binding(context_ref, root_session_ref,
            context_generation, require_open=False).location, "human_collaboration", context_ref, root_session_ref,
            "manual_question_creation")

    def bind_companion_session(self, scope_ref: str, root_session_ref: str) -> WorkspaceBinding:
        return self._companion_session_binding(scope_ref, root_session_ref, require_open=True)

    def _companion_session_binding(self, scope_ref: str, root_session_ref: str,
                                   *, require_open: bool) -> WorkspaceBinding:
        if (not isinstance(scope_ref, str) or not scope_ref
            or not isinstance(root_session_ref, str) or not root_session_ref):
            raise SemanticMcpError("workspace_companion_scope_invalid")
        try:
            facts = self._hc.query_companion_work_context(root_session_ref)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
        if (facts is None or facts["session_ref"] != root_session_ref
            or facts["scope_ref"] != scope_ref or require_open and facts["status"] != "open"):
            raise SemanticMcpError("workspace_companion_scope_invalid")
        identity = {"companion_session_ref": root_session_ref}
        location = WorkspaceLocation("workspace:" + canonical_hash(identity), "companion",
            root_session_ref, root_session_ref, self._bases["companion"] / canonical_hash(identity),
            quest_ref=facts["quest_ref"], request_ref=scope_ref)
        self._ensure_directory(location.directory)
        return WorkspaceBinding(location, canonical_hash({"owner": "human_collaboration",
            **identity, "scope_ref": scope_ref}))

    def bind_acquisition_session(self, session_ref: str) -> WorkspaceBinding:
        session = self._ar.query_acquisition_session(session_ref=session_ref)
        if session is None:
            raise SemanticMcpError("workspace_acquisition_session_missing")
        location = WorkspaceLocation("workspace:" + canonical_hash({"acquisition_session_ref": session.session_ref}),
            "acquisition", session.session_ref, session.session_ref,
            self._bases["acquisition"] / canonical_hash({"acquisition_session_ref": session.session_ref}),
            initialization_id=session.initialization_id)
        self._ensure_directory(location.directory)
        return WorkspaceBinding(location, canonical_hash({"owner": "agent_runtime",
            "acquisition_session_ref": session.session_ref}))

    def destination_for_human_request(self, *, request_ref: str, waiter_ref: str) -> WorkspaceDestination:
        request = self._hc.query_human_request(request_ref)
        if request is None:
            raise SemanticMcpError("workspace_destination_missing")
        waiters = [item for item in request["direct_waiters"] if item["waiter_ref"] == waiter_ref]
        effect = request.get("open_effect")
        if len(waiters) != 1 or not isinstance(effect, dict):
            raise SemanticMcpError("workspace_destination_unbound")
        caller = effect.get("operation_binding")
        if not isinstance(caller, dict) or waiter_ref != "root_run:" + str(caller.get("task_ref")):
            raise SemanticMcpError("workspace_destination_unbound")
        target_ref = request.get("target_assertion", {}).get("root", {}).get("target_ref")
        if target_ref:
            candidates = [source for source in self._target_locations(target_ref, history=True)
                          if source.work_ref == caller.get("task_ref")
                          and source.root_session_ref == caller.get("root_session_ref")]
            if len(candidates) != 1:
                raise SemanticMcpError("workspace_destination_unbound")
            location = candidates[0]
        else:
            location = self._runtime_location(caller["task_ref"])
        original_task_ref = (location.request_ref if location.root_kind in {"acquisition", "companion"} and location.request_ref
                             else location.work_ref)
        if (location.root_session_ref != caller.get("root_session_ref")
            or original_task_ref != caller.get("task_ref")):
            raise SemanticMcpError("workspace_destination_unbound")
        self._ensure_directory(location.directory)
        return WorkspaceDestination(location, request["issuer"], request_ref, waiter_ref)

    def _visible(self, context: SemanticCallContext) -> tuple[WorkspaceLocation, ...]:
        current = self.bind_runtime(context).location
        if current.cycle_ref is None:
            return (current,)
        locations = {current.workspace_ref: current}
        if current.root_kind == "target":
            for source in self._target_locations(current.target_ref, history=True):
                locations[source.workspace_ref] = source
        rank = 2 if current.root_kind == "target" else _STAGES.index(current.root_kind)
        for request in self._ae.query_cycle_stage_requests(current.cycle_ref):
            if _STAGES.index(request.stage) >= rank:
                continue
            run = getattr(self._ar, "query_" + request.stage + "_stage_run")(request.request_ref)
            if run is not None:
                source = self._runtime_location(run.run_ref)
                if source.cycle_ref != current.cycle_ref or source.quest_ref != current.quest_ref:
                    raise SemanticMcpError("workspace_lineage_invalid")
                locations[source.workspace_ref] = source
        if current.root_kind in {"bundle", "reasoning"}:
            for request in self._ae.query_cycle_stage_requests(current.cycle_ref):
                if request.stage != "bundle":
                    continue
                graph = self._rg.query_target_graph(request.request_ref)
                if graph is None:
                    continue
                for target in graph.targets:
                    if self._ar.query_admitted_target_launch(target.target_ref) is None:
                        continue
                    try:
                        sources = self._target_locations(target.target_ref, history=True)
                    except OwnerConflict as error:
                        if error.code == "target_run_workspace_unavailable":
                            continue
                        raise
                    for source in sources:
                        if source.cycle_ref != current.cycle_ref or source.quest_ref != current.quest_ref:
                            raise SemanticMcpError("workspace_lineage_invalid")
                        locations[source.workspace_ref] = source
        return tuple(locations[key] for key in sorted(locations))

    def discover(self, context: SemanticCallContext, *, prefix: str = "", offset: int = 0,
                 limit: int = 50) -> dict[str, object]:
        return self._discover(self._visible(context), prefix=prefix, offset=offset, limit=limit)

    def discover_initialization(self, initialization_id: str, root_session_ref: str, **arguments):
        return self._discover((self.bind_initialization(initialization_id, root_session_ref).location,), **arguments)

    def discover_manual_creation(self, context_ref: str, root_session_ref: str,
                                 context_generation: int, **arguments):
        return self._discover((self._manual_creation_binding(context_ref, root_session_ref,
            context_generation, require_open=False).location,), **arguments)

    def discover_companion_session(self, scope_ref: str, root_session_ref: str, **arguments):
        return self._discover((self._companion_session_binding(scope_ref, root_session_ref,
            require_open=False).location,), **arguments)

    def _discover(self, locations, *, prefix="", offset=0, limit=50):
        if prefix:
            _relative_parts(prefix.rstrip("/"))
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise SemanticMcpError("workspace_page_invalid")
        files = []
        scanned = 0
        for location in locations:
            if not location.directory.exists():
                continue
            for directory, children, names in os.walk(location.directory, followlinks=False):
                scanned += len(children) + len(names)
                if scanned > _MAX_SCAN_FILES:
                    raise SemanticMcpError("workspace_scan_limit_exceeded", {"maximum_entries": _MAX_SCAN_FILES})
                children[:] = sorted(child for child in children if not child.startswith(".delivery-") and child != ".delivery.json"
                    and not (location.root_kind == "target" and Path(directory) == location.directory and child == "inputs"))
                for name in sorted(names):
                    path = (Path(directory) / name).relative_to(location.directory).as_posix()
                    if name.startswith(".delivery-") or name == ".delivery.json" or not path.startswith(prefix):
                        continue
                    try:
                        content = _read_bytes(location.directory, path)
                    except SemanticMcpError as error:
                        if error.code == "workspace_file_unsafe":
                            continue
                        if error.code == "workspace_file_too_large":
                            files.append({**location.source(), "path": path,
                                "bytes": error.details.get("bytes"), "sha256": None,
                                "readable": False, "read_error": {"code": error.code,
                                    "maximum_bytes": _MAX_FILE_BYTES}})
                            continue
                        raise
                    files.append({**location.source(), "path": path, "bytes": len(content),
                                  "sha256": hashlib.sha256(content).hexdigest(), "readable": True})
        files.sort(key=lambda item: (item["workspace_ref"], item["path"]))
        return {"files": files[offset:offset + limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(files) else None,
                "limits": {"maximum_file_bytes": _MAX_FILE_BYTES,
                    "maximum_chunk_bytes": 65536, "maximum_scan_entries": _MAX_SCAN_FILES}}

    def read(self, context: SemanticCallContext, **arguments) -> dict[str, object]:
        return self._read(self._visible(context), **arguments)

    def read_initialization(self, initialization_id: str, root_session_ref: str, **arguments):
        return self._read((self.bind_initialization(initialization_id, root_session_ref).location,), **arguments)

    def read_manual_creation(self, context_ref: str, root_session_ref: str,
                             context_generation: int, **arguments):
        return self._read((self._manual_creation_binding(context_ref, root_session_ref,
            context_generation, require_open=False).location,), **arguments)

    def read_companion_session(self, scope_ref: str, root_session_ref: str, **arguments):
        return self._read((self._companion_session_binding(scope_ref, root_session_ref,
            require_open=False).location,), **arguments)

    def _read(self, locations, *, workspace_ref, path, expected_sha256=None, offset=0, max_bytes=65536):
        if type(offset) is not int or offset < 0 or type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise SemanticMcpError("workspace_page_invalid")
        source = next((item for item in locations if item.workspace_ref == workspace_ref), None)
        if source is None:
            raise SemanticMcpError("workspace_not_visible")
        parts = _relative_parts(path)
        if any(part.startswith(".delivery-") or part == ".delivery.json" for part in parts) or (source.root_kind == "target" and parts[0] == "inputs"):
            raise SemanticMcpError("workspace_file_unsafe")
        content = _read_bytes(source.directory, path)
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise SemanticMcpError("workspace_content_changed")
        chunk = content[offset:offset + max_bytes]
        next_offset = offset + len(chunk) if offset + len(chunk) < len(content) else None
        return {**source.source(), "path": path, "sha256": digest, "offset": offset,
                "total_bytes": len(content), "next_offset": next_offset, "content": chunk}

    def deliver(self, destination: WorkspaceDestination, *, delivery_ref: str,
                files: tuple[tuple[str, bytes], ...]) -> dict[str, object]:
        if not isinstance(delivery_ref, str) or not delivery_ref or len(delivery_ref) > 256:
            raise SemanticMcpError("workspace_delivery_ref_invalid")
        if destination.creation_context_kind == "quest_initialization":
            fresh = self.destination_for_initialization(destination.request_ref, destination.waiter_ref)
        elif destination.creation_context_kind == "manual_question_creation":
            fresh = self.destination_for_manual_creation(destination.request_ref, destination.waiter_ref,
                destination.location.context_generation)
        else:
            fresh = self.destination_for_human_request(request_ref=destination.request_ref, waiter_ref=destination.waiter_ref)
        if fresh != destination:
            raise SemanticMcpError("workspace_destination_changed")
        if not files or len(files) > 100:
            raise SemanticMcpError("workspace_delivery_invalid")
        manifest = []
        names = set()
        for path, content in files:
            parts = _relative_parts(path)
            if (any(part.startswith(".delivery-") or part == ".delivery.json" for part in parts)
                or path in names or not isinstance(content, bytes) or len(content) > _MAX_FILE_BYTES):
                raise SemanticMcpError("workspace_delivery_invalid")
            names.add(path)
            manifest.append({"path": path, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
        manifest.sort(key=lambda item: item["path"])
        value = {"delivery_ref": delivery_ref, "files": manifest}
        root = destination.location.directory
        published = root / "inbox" / canonical_hash({"delivery_ref": delivery_ref})
        temporary = ".delivery-" + secrets.token_hex(16)
        with _directory_fd(root) as root_fd, _child_directory(root_fd, "inbox", create=True) as inbox_fd:
            os.mkdir(temporary, mode=0o700, dir_fd=root_fd)
            try:
                with _child_directory(root_fd, temporary) as temporary_fd:
                    for path, content in (*files, (".delivery.json", canonical_json(value).encode("utf-8"))):
                        _write_delivery_file(temporary_fd, _relative_parts(path), content)
                    os.fsync(temporary_fd)
                _check_directory_identity(root, root_fd)
                _check_directory_identity(root / "inbox", inbox_fd)
                try:
                    os.rename(temporary, published.name, src_dir_fd=root_fd, dst_dir_fd=inbox_fd)
                except OSError:
                    with _child_directory(inbox_fd, published.name):
                        pass
                _check_directory_identity(root, root_fd)
                _check_directory_identity(root / "inbox", inbox_fd)
                os.fsync(inbox_fd)
                existing = json.loads(_read_bytes(root, published.relative_to(root).as_posix() + "/.delivery.json"))
                if existing != value:
                    raise SemanticMcpError("workspace_delivery_conflict")
                for item in manifest:
                    actual = _read_bytes(root, (published.relative_to(root) / item["path"]).as_posix())
                    if hashlib.sha256(actual).hexdigest() != item["sha256"]:
                        raise SemanticMcpError("workspace_delivery_conflict")
                return {**destination.location.source(), "delivery_ref": delivery_ref, "files": [
                    {**item, "path": (published.relative_to(root) / item["path"]).as_posix()} for item in manifest]}
            except OSError as error:
                raise SemanticMcpError("workspace_file_unsafe") from error
            finally:
                shutil.rmtree(temporary, dir_fd=root_fd, ignore_errors=True)

    @staticmethod
    def _ensure_directory(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            if current.is_symlink() or (current.exists() and not current.is_dir()):
                raise SemanticMcpError("workspace_file_unsafe")
            current.mkdir(exist_ok=True, mode=0o700)


def _relative_parts(path: str) -> tuple[str, ...]:
    if (not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path
        or ":" in path or "\0" in path or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise SemanticMcpError("workspace_path_invalid")
    return PurePosixPath(path).parts


@contextmanager
def _directory_fd(path: Path):
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SemanticMcpError("workspace_safe_reader_unavailable")
    with ExitStack() as stack:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
        stack.callback(os.close, fd)
        try:
            for part in path.parts[1:]:
                fd = stack.enter_context(_child_directory(fd, part))
            yield fd
        except OSError as error:
            raise SemanticMcpError("workspace_file_unsafe") from error


@contextmanager
def _child_directory(parent: int, name: str, *, create=False):
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent)
        except FileExistsError:
            pass
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield fd
    finally:
        os.close(fd)


def _check_directory_identity(path: Path, held: int) -> None:
    with _directory_fd(path) as fresh:
        if (os.fstat(fresh).st_dev, os.fstat(fresh).st_ino) != (os.fstat(held).st_dev, os.fstat(held).st_ino):
            raise SemanticMcpError("workspace_namespace_changed")


def _write_delivery_file(parent: int, parts: tuple[str, ...], content: bytes) -> None:
    with ExitStack() as stack:
        for part in parts[:-1]:
            parent = stack.enter_context(_child_directory(parent, part, create=True))
        fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)


def _read_bytes(root: Path, path: str) -> bytes:
    parts = _relative_parts(path)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        raise SemanticMcpError("workspace_safe_reader_unavailable")
    descriptors = []
    try:
        fd = os.open(root.anchor, os.O_RDONLY | os.O_DIRECTORY)
        descriptors.append(fd)
        for part in (*root.parts[1:], *parts[:-1]):
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            descriptors.append(fd)
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise SemanticMcpError("workspace_file_unsafe")
        if before.st_size > _MAX_FILE_BYTES:
            raise SemanticMcpError("workspace_file_too_large", {"path": path,
                "bytes": before.st_size, "maximum_bytes": _MAX_FILE_BYTES})
        content = bytearray()
        while chunk := os.read(fd, min(65536, _MAX_FILE_BYTES + 1 - len(content))):
            content.extend(chunk)
            if len(content) > _MAX_FILE_BYTES:
                raise SemanticMcpError("workspace_file_too_large", {"path": path,
                    "maximum_bytes": _MAX_FILE_BYTES})
        after = os.fstat(fd)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise SemanticMcpError("workspace_content_changed")
        return bytes(content)
    except OSError as error:
        raise SemanticMcpError("workspace_file_unsafe") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def workspace_operations(workspaces: RootWorkspaces | None) -> tuple[SemanticOperation, ...]:
    def call(context, arguments):
        if workspaces is None:
            raise SemanticMcpError("workspace_service_unavailable")
        try:
            if context.operation_id == "research_workspace.discover":
                return workspaces.discover(context, **arguments)
            value = workspaces.read(context, **arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
        content = value.pop("content")
        try:
            value.update(encoding="utf-8", text=content.decode("utf-8"))
        except UnicodeDecodeError:
            value.update(encoding="base64", base64=base64.b64encode(content).decode("ascii"))
        return value

    return (
        SemanticOperation("research_workspace.discover", "research_workspace",
            "Discover pending working files in this actual work and eligible earlier work in the same Cycle. Returns source work, relative paths, observed SHA-256 and limits. Files above 64 MiB remain listed with readable=false, sha256=null and read_error=workspace_file_too_large. Scans stop at 10000 directory entries. These files are not formal assets.", call,
            {"type": "object", "properties": {"prefix": {"type": "string", "maxLength": 1024},
                "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
             "additionalProperties": False}, {"type": "object"}),
        SemanticOperation("research_workspace.read", "research_workspace",
            "Read at most 65536 bytes from a discovered working file or HumanRequest delivery.reply_reader/uploaded_readers using its workspace_ref, path and expected_sha256. Rechecks original work visibility and hash, with or without a Cycle. UTF-8 or base64 content includes next_offset. Linked locators remain at their host paths for bounded native reads. Reply delivery does not register formal assets.", call,
            {"type": "object", "properties": {"workspace_ref": {"type": "string", "maxLength": 256},
                "path": {"type": "string", "maxLength": 1024}, "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
                "offset": {"type": "integer", "minimum": 0}, "max_bytes": {"type": "integer", "minimum": 1, "maximum": 65536}},
             "required": ["workspace_ref", "path"], "additionalProperties": False}, {"type": "object"}),
    )
