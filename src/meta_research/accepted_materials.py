"""Exact accepted input access; reuse Owner authority and RM byte export."""
from __future__ import annotations

import hashlib
from pathlib import Path

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticCallContext


def selected_material_path(memory, asset, workspace_root: Path, input_key: str, *,
                           directory: str, error_prefix: str) -> Path:
    """Reuse an exact exported input, refusing aliases and edited copies."""
    root = workspace_root.resolve()
    description = memory.describe_asset_export(asset.version_ref)
    destination = (root / directory / canonical_hash({"request": input_key})
                   / canonical_hash({"version_ref": asset.version_ref}) / description.file_name)
    if not destination.resolve().is_relative_to(root):
        raise OwnerConflict(error_prefix + "_path_invalid")
    if any(path.is_symlink() for path in (destination, *destination.parents) if path != root):
        raise OwnerConflict(error_prefix + "_path_invalid")
    try:
        return memory.export_asset(asset.version_ref, destination).path
    except OwnerConflict as error:
        if error.code != "asset_export_destination_exists":
            raise
    # A lost enqueue may retry after export. Never replace an edited copy.
    try:
        expected = set()
        for entry in description.entries:
            path = destination / entry.path if description.kind == "directory" else destination
            expected.add(path)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry.size:
                raise ValueError()
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry.sha256:
                    raise ValueError()
        if description.kind == "directory":
            actual = set()
            for path in destination.rglob("*"):
                if path.is_symlink():
                    raise ValueError()
                if path.is_file():
                    actual.add(path)
            if actual != expected:
                raise ValueError()
    except (OSError, ValueError):
        raise OwnerConflict(error_prefix + "_changed") from None
    return destination


class DeepFetchAcceptedMaterials:
    """Read only the current initialization request's frozen accepted inputs.

    This derives permission from HC/AR on every call. It records neither a new
    grant nor a scientific adoption and does not enable no-Quest discovery.
    """
    def __init__(self, agent_runtime, human_collaboration, research_memory):
        self.runtime = agent_runtime
        self.human = human_collaboration
        self.memory = research_memory

    def _current(self, context):
        scope = self.runtime.verify_root_agent_runtime_scope(
            root_kind=context.root_kind, run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        if context.root_kind != "deepfetch" or scope.get("quest_ref"):
            raise OwnerConflict("content_quest_scope_required")
        run = self.runtime.query_deepfetch_run_by_ref(context.run_ref)
        request = self.human.query_deepfetch_request(run.request_ref)
        if request is None or request.creation_context_kind != "quest_initialization":
            raise OwnerConflict("content_source_unbound")
        return request

    def _asset(self, binding):
        asset = self.memory.query_asset_version(binding["version_ref"])
        if asset is None or asset.as_binding().as_dict() != binding:
            raise OwnerConflict("content_source_unbound")
        self.memory.verify_asset_binding(asset_ref=asset.asset_ref, version_ref=asset.version_ref,
            content_hash=asset.content_hash, manifest_hash=asset.manifest_hash, receipt=asset.receipt)
        return asset

    def read(self, context, arguments):
        request = self._current(context)
        if "source_quest_ref" in arguments:
            raise OwnerConflict("content_quest_not_authorized")
        source, version = arguments["source_ref"], arguments["version_ref"]
        binding = next((item for item in request.accepted_material_bindings
                        if item["version_ref"] == version), None)
        if source != version or binding is None:
            raise OwnerConflict("content_source_unbound")
        asset = self._asset(binding)
        return self.memory.read_asset_content_page(asset.version_ref,
            **{key: value for key, value in arguments.items() if key not in {"source_ref", "version_ref"}})

    def prepare(self, request, workspace_root):
        context = SemanticCallContext(request.run_ref, request.attempt_ref, request.root_session_ref,
            request.fence_ref, canonical_hash(request.runtime_binding.as_dict()),
            "deepfetch", "primary", "research_memory.content.read")
        current = self._current(context)
        if (current.request_ref != request.request_ref
                or current.accepted_material_bindings != request.accepted_material_bindings):
            raise OwnerConflict("content_source_unbound")
        materials = []
        for binding in current.accepted_material_bindings:
            asset = self._asset(binding)
            path = selected_material_path(self.memory, asset, workspace_root, current.request_ref,
                directory="provided-materials", error_prefix="deepfetch_input")
            materials.append({"binding": binding, "path": str(path), "media_type": asset.media_type,
                "reader": {"operation": "research_memory.content.read", "args": {
                    "source_ref": asset.version_ref, "version_ref": asset.version_ref}}})
        return materials
