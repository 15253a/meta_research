"""Shared immutable selection contracts for Dataset and Environment indexes."""
from meta_research.content_selection import ContentSelection
from meta_research.dataset_contract import dataset_asset_binding
from meta_research.owners.common import OwnerConflict, canonical_hash


def content_selection_ref(selection):
    value = selection.as_dict() if isinstance(selection, ContentSelection) else ContentSelection.from_dict(selection).as_dict()
    return "content_selection_" + canonical_hash(value)


def selection_readers(record, source_ref):
    return {"selection_readers": [{"selection_ref": content_selection_ref(value),
        "selection": value, "reader": {"source_ref": source_ref,
            "version_ref": value["asset_binding"]["version_ref"],
            "selection_path": value["path"]}} for value in record["selections"]]}


def resource_selections(values, *, asset_bindings=None):
    """Normalize logical selections and derive their retained parent dependencies."""
    if values is None:
        if asset_bindings is not None and not isinstance(asset_bindings, (list, tuple)):
            raise OwnerConflict("resource_selections_invalid")
        values = [ContentSelection(dataset_asset_binding(value), None) for value in asset_bindings or []]
    if not isinstance(values, (list, tuple)) or len(values) > 256:
        raise OwnerConflict("resource_selections_invalid")
    selections, parents = {}, {}
    for value in values:
        selection = value if isinstance(value, ContentSelection) else ContentSelection.from_dict(value)
        binding = selection.asset_binding
        prior = parents.get(binding.version_ref)
        if prior is not None and prior != binding:
            raise OwnerConflict("resource_selection_parent_conflict")
        parents[binding.version_ref] = binding
        selections[(binding.version_ref, selection.path)] = selection
    bindings = [parents[key] for key in sorted(parents)]
    if asset_bindings is not None:
        if not isinstance(asset_bindings, (list, tuple)):
            raise OwnerConflict("resource_selection_parent_conflict")
        supplied = [dataset_asset_binding(value) for value in asset_bindings]
        if sorted(supplied, key=lambda item: item.version_ref) != bindings:
            raise OwnerConflict("resource_selection_parent_conflict")
    ordered = sorted(selections.values(), key=lambda item: (item.asset_binding.version_ref, item.path or ""))
    return ordered, bindings


def verify_resource_selections(verifier, values, *, asset_bindings):
    selections, _ = resource_selections(values, asset_bindings=asset_bindings)
    for selection in selections:
        verifier.describe_selection(selection.asset_binding, selection.path)
    return selections
