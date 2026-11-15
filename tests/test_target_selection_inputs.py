"""Real RM parent custody feeds distinct exact child inputs through one cache."""
from dataclasses import replace
import json
import os
from types import SimpleNamespace

import pytest

from meta_research.bundle_protocol import projection_plain_value, validate_closed_bundle_projection
from meta_research.content_selection import ContentSelection
from meta_research.owners.common import OwnerConflict
from meta_research.resource_selections import content_selection_ref
from test_content_selections import memory, intake, tree
from test_target_root_frozen_inputs import _Workspace
from test_target_run_owner import _records


def test_two_children_and_parent_share_cache_without_merging_scope(memory, tmp_path, monkeypatch):
    parent = intake(memory, tree(tmp_path))
    original = _records()[2]
    template = original.accepted_input_asset_proofs[0]
    selections = [ContentSelection(parent, path) for path in (None, "single", "single/config.txt", "empty")]
    proofs = tuple(replace(template, asset_ref=parent.asset_ref,
        selection_ref=content_selection_ref(selection), selection_path=selection.path,
        asset_version_ref=parent.version_ref) for selection in selections)
    handle = replace(original, accepted_input_target_commit_refs=(), accepted_input_asset_proofs=proofs)
    workspace = _Workspace(tmp_path / "target" / "work", handle)
    description = memory.describe_asset_export(parent.version_ref)
    calls = []
    export = memory.export_asset
    def counted(*args):
        calls.append(args[0])
        return export(*args)
    workspace._memory = SimpleNamespace(
        describe_input_asset=lambda **kw: (parent, None, description),
        describe_selection=memory.describe_selection, export_selection=memory.export_selection,
        export_asset=counted)
    workspace._graph = SimpleNamespace(input_asset_source_refs=lambda ref: {},
        selected_evidence_target_commits=lambda ref: {})
    paths = workspace.materialize_target_workspace_inputs(handle=handle)
    manifest = json.loads(open(paths[0], encoding="utf-8").read())
    entries = manifest['entries']
    assert len(entries) == 4
    assert [entry['selection_path'] for entry in entries] == [None, 'single', 'single/config.txt', 'empty']
    assert len(calls) == 1
    from pathlib import Path
    root = Path(paths[0]).parent
    delivered = [root / entry['relative_path'] for entry in entries]
    assert os.path.samefile(delivered[0] / 'single/config.txt', delivered[1] / 'config.txt')
    assert os.path.samefile(delivered[2], delivered[1] / 'config.txt')
    assert delivered[3].is_dir() and list(delivered[3].iterdir()) == []
    assert not (delivered[1] / 'sibling.txt').exists()
    workspace.verify_target_workspace_inputs(handle=handle)
    delivered[2].chmod(0o600)
    delivered[2].write_bytes(b'tamper')
    with pytest.raises(OwnerConflict, match='integrity'):
        workspace.verify_target_workspace_inputs(handle=handle)


def test_selection_proof_roundtrips_both_issued_handle_readers():
    from meta_research.owners.agent_runtime import _target_work_handle_from_json
    from meta_research.owners.target_run_runtime import _decode_stored_record
    from meta_research.owners.common import canonical_hash, canonical_json
    from meta_research.bundle_protocol import TargetWorkHandle
    handle = _records()[2]
    for selected in (False, True):
        proof = handle.accepted_input_asset_proofs[0]
        if selected:
            proof = replace(proof, selection_ref='content_selection_' + 'a' * 64,
                selection_path='nested/file', asset_version_ref='asset_version_one')
        value = replace(handle, accepted_input_asset_proofs=(proof,))
        document = projection_plain_value(value)
        encoded = canonical_json(document)
        assert _target_work_handle_from_json(encoded) == value
        assert _decode_stored_record(encoded, canonical_hash(document), TargetWorkHandle) == value
        validate_closed_bundle_projection(value)
