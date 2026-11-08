"""Resolve selected files against existing Workspace and human-input authority."""
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from meta_research.owners.common import OwnerConflict
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.server_materials import relative_parts


@dataclass(frozen=True)
class SelectedIntakeSource:
    locator: str
    provenance: dict[str, object]
    inspect: Callable[[], dict[str, object]]


def _selector_parts(selector):
    if not isinstance(selector, dict):
        raise OwnerConflict("asset_source_selector_invalid", {
            "message": "source must be a workspace or material selector object.",
            "correction": "If previously submitted, reconcile the original arguments first. Provide kind, path and the matching workspace_ref or reference_ref.",
            "execution_stage": "source_selection", "call_execution": "not_started", "effect_status": "unknown",
        })
    try:
        parts = relative_parts(selector.get("path"))
    except OwnerConflict as error:
        raise OwnerConflict(error.code, {
            "message": "source.path must be a safe relative path.",
            "correction": "If previously submitted, reconcile the original arguments first. Supply path without absolute, parent or dot components.",
            "execution_stage": "source_selection", "call_execution": "not_started", "effect_status": "unknown",
        }) from error
    if selector.get("kind") not in ("workspace", "material"):
        raise OwnerConflict("asset_source_selector_invalid", {
            "message": "source.kind must be workspace or material.",
            "correction": "If previously submitted, reconcile the original arguments first. Select the source kind and its matching reference field.",
            "execution_stage": "source_selection", "call_execution": "not_started", "effect_status": "unknown",
        })
    reference_name = {"workspace": "workspace_ref", "material": "reference_ref"}.get(selector.get("kind"))
    if reference_name is None or set(selector) != {"kind", reference_name, "path"}:
        raise OwnerConflict("asset_source_selector_invalid", {
            "message": f"This source.kind requires only kind, {reference_name} and path.",
            "correction": f"If previously submitted, reconcile the original arguments first. Supply {reference_name} and path; omit fields for other source kinds.",
            "execution_stage": "source_selection", "call_execution": "not_started", "effect_status": "unknown",
        })
    reference = selector[reference_name]
    if not isinstance(reference, str) or not reference or len(reference) > 16000:
        raise OwnerConflict("asset_source_selector_invalid", {
            "message": f"source.{reference_name} must contain 1–16000 characters.",
            "correction": "If previously submitted, reconcile the original arguments first. Use the reference returned by the authorized source reader.",
            "execution_stage": "source_selection", "call_execution": "not_started", "effect_status": "unknown",
        })
    if selector["kind"] == "workspace" and (not parts or any(
        part.startswith(".delivery-") or part in {".delivery.json", ".creation-context"} for part in parts
    )):
        raise OwnerConflict("workspace_path_invalid")
    return parts


def reconcile_intake_source(memory, *, effect_key, quest_ref, selector, values):
    """Read an exact prior operation after its caller's reconcile scope check.

    Reconciliation does not reopen source authority or perform new admission.
    Stored source facts are usable only when the complete submitted request
    still matches RM's existing immutable operation hash.
    """
    _selector_parts(selector)
    record = memory.query_asset_intake_effect_record(effect_key)
    if record is None:
        return {"status": "not_found", "result": None}
    stored = record["request"]
    if stored is None:
        asset = record["result"]["asset"]
        source_kind = asset["source_kind"]
        # RM adds source_kind to accepted provenance; it was not part of the
        # selected source's submitted provenance. Other source facts remain
        # receipt-verified and need no live workspace or original to recover.
        provenance = {key: value for key, value in asset["provenance"].items() if key != "source_kind"}
        locators = record["source_locators"]
    else:
        source_kind = stored.get("source_kind")
        provenance = stored.get("provenance", {})
        locators = [stored["source_locator"]]
    if (source_kind not in {"local_path", "directory"}
        or not isinstance(provenance, dict)
        or provenance.get("kind") not in {"workspace_selection", "material_selection"}
        or provenance.get("source") != selector):
        raise OwnerConflict("asset_intake_idempotency_conflict")
    for locator in locators:
        request = AssetIntakeRequest(**{**values,
            "source_locator": locator,
            "provenance": {**provenance, "declared_provenance": values.get("provenance", {})},
            "origin_quest_ref": quest_ref, "effect_scope_required": True})
        try:
            result = memory.query_asset_intake_by_idempotency_key(effect_key, request)
        except OwnerConflict as error:
            if error.code != "asset_intake_idempotency_conflict":
                raise
            continue
        return {"status": "not_found" if result is None else result.status,
                "result": None if result is None else result.as_public_dict()}
    raise OwnerConflict("asset_intake_idempotency_conflict")


def resolve_intake_source(human, context, selector):
    if human is None or human._root_workspaces is None:
        raise OwnerConflict("asset_source_authority_unavailable")
    parts = _selector_parts(selector)
    workspaces = human._root_workspaces
    if selector.get("kind") == "workspace" and set(selector) == {"kind", "workspace_ref", "path"}:
        location = next((item for item in workspaces._visible(context)
                         if item.workspace_ref == selector["workspace_ref"]), None)
        if location is None:
            raise OwnerConflict("workspace_not_visible")
        # Target notes retain their dedicated, validated producer contract.
        if location.root_kind == "target":
            raise OwnerConflict("target_note_selector_required")
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
