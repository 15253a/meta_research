"""Resolve selected files against existing Workspace and human-input authority."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from meta_research.owners.common import OwnerConflict
from meta_research.server_materials import relative_parts


@dataclass(frozen=True)
class SelectedIntakeSource:
    locator: str
    provenance: dict[str, object]
    inspect: Callable[[], dict[str, object]]


def resolve_intake_source(human, context, selector):
    if human is None or human._root_workspaces is None:
        raise OwnerConflict("asset_source_authority_unavailable")
    if not isinstance(selector, dict):
        raise OwnerConflict("asset_source_selector_invalid")
    parts = relative_parts(selector.get("path"))
    reference = selector.get("workspace_ref", selector.get("reference_ref"))
    if not isinstance(reference, str) or not reference or len(reference) > 16000:
        raise OwnerConflict("asset_source_selector_invalid")
    workspaces = human._root_workspaces
    if selector.get("kind") == "workspace" and set(selector) == {"kind", "workspace_ref", "path"}:
        location = next((item for item in workspaces._visible(context)
                         if item.workspace_ref == selector["workspace_ref"]), None)
        if location is None:
            raise OwnerConflict("workspace_not_visible")
        # Target notes retain their dedicated, validated producer contract.
        if location.root_kind == "target":
            raise OwnerConflict("target_note_selector_required")
        if not parts or any(part.startswith(".delivery-") or part in {".delivery.json", ".creation-context"}
                            for part in parts):
            raise OwnerConflict("workspace_path_invalid")
        locator = str(location.directory.joinpath(*parts))
        provenance = {"kind": "workspace_selection", "source": dict(selector),
                      "source_work": location.source()}

        def inspect():
            with workspaces.server_files._open(locator) as (_, observed):
                return observed
    elif selector.get("kind") == "material" and set(selector) == {"kind", "reference_ref", "path"}:
        reference = human.query_work_material(selector["reference_ref"])
        human._authorize_material(reference, context)
        locator = str(Path(reference["source"]["absolute_path"]).joinpath(*parts))
        provenance = {"kind": "material_selection", "source": dict(selector),
                      "receiver": reference["receiver"], "original_source": reference["source"]}

        def inspect():
            with workspaces.server_files._open_reference(reference["source"], selector["path"]) as (_, observed):
                return observed
    else:
        raise OwnerConflict("asset_source_selector_invalid")
    return SelectedIntakeSource(locator, provenance, inspect)
