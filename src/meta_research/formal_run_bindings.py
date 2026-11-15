"""Resolve per-execution code and local inputs from the accepted completion.

The finalizer and both formal registration paths share the current selection
fingerprint protocol. A declared revision is only an assertion about frozen bytes.
"""
from __future__ import annotations

import json

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.target_content_issue import TargetContentIssue

SELECTION_MANIFEST_SCHEMA = 'meta-research/target-root-completion-manifest/v2'


def _selection_entries(entries, selections, schema):
    if schema != SELECTION_MANIFEST_SCHEMA:
        raise OwnerConflict('target_root_manifest_protocol_unsupported')
    result = []
    for ordinal, item in enumerate(selections):
        original = next((entry for entry in entries if entry['declared_relative_path'] == item['declared_relative_path']), {})
        note = {'research_note': original['research_note']} if original.get('research_note') is not None else {}
        result.append({**item, **note, 'ordinal': ordinal, 'binding': item['asset_binding'],
                       'content_hash': item['asset_binding']['content_hash'],
                       'selection_protocol': SELECTION_MANIFEST_SCHEMA})
    return result


def manifest_selection_entries(manifest):
    return _selection_entries([entry.as_dict() for entry in manifest.entries],
        getattr(manifest, 'selections', ()), getattr(manifest, 'schema_ref', None))


def db_manifest_selection_entries(manifest):
    payload = json.loads(manifest['payload_json'])
    return _selection_entries(json.loads(manifest['entries_json']),
                              payload.get('selections', ()), payload.get('schema_ref'))



def implementation_binding(entries, paths=None, asserted_revision=None):
    available = [entry for entry in entries if entry['role'] == 'implementation']
    if paths is None:
        available = [entry for entry in available if entry.get('path') is None]
        if len(available) != 1:
            raise TargetContentIssue('target_formal_implementation_selection_required')
        selected = available
    else:
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)):
            raise TargetContentIssue('target_formal_implementation_paths_invalid')
        selected = [entry for entry in available if entry['declared_relative_path'] in paths]
        if len(selected) != len(paths):
            raise TargetContentIssue('target_formal_implementation_path_not_bound')
    if not selected:
        raise OwnerConflict('target_root_implementation_missing')
    tree_hash = implementation_set_hash(selected)
    revision = 'target_impl_' + tree_hash
    if asserted_revision is not None and asserted_revision != revision:
        raise TargetContentIssue('target_formal_implementation_revision_mismatch')
    return revision, tree_hash


def implementation_set_hash(entries):
    if not entries or any(entry.get('selection_protocol') != SELECTION_MANIFEST_SCHEMA for entry in entries):
        raise OwnerConflict('target_root_manifest_protocol_unsupported')
    return canonical_hash({'schema_ref': 'meta-research/target-root-implementation-set/v2',
        'artifacts': sorted([{'path': entry['declared_relative_path'],
            'kind': entry['artifact_kind'], 'tree_hash': entry['tree_hash']}
            for entry in entries], key=lambda value: value['path'])})


def resolve_local_inputs(document, entries, run):
    """Resolve only earlier producers in the original declared execution order."""
    selections = run.get('local_inputs', [])
    if not isinstance(selections, list) or len(selections) > 100:
        raise TargetContentIssue('target_formal_local_input_invalid')
    declared = document.get('formal_runs', [])
    indexes = {item['run_key']: index for index, item in enumerate(declared)
               if isinstance(item, dict) and isinstance(item.get('run_key'), str)}
    current = indexes.get(run.get('run_key'))
    result = []
    for choice in selections:
        if (not isinstance(choice, dict) or set(choice) != {'producer_run_key', 'artifact_path'}
                or not all(isinstance(value, str) and value for value in choice.values())):
            raise TargetContentIssue('target_formal_local_input_invalid')
        producer_index = indexes.get(choice['producer_run_key'])
        if current is None or producer_index is None or producer_index >= current:
            raise TargetContentIssue('target_formal_local_input_order_invalid')
        producer = declared[producer_index]
        if producer.get('variant_run_ref') or producer.get('status', 'executed') not in {'executed', 'failed'}:
            raise TargetContentIssue('target_formal_local_input_producer_invalid')
        owned = [*(producer.get('artifact_paths') or []), *(producer.get('checkpoint_paths') or [])]
        if choice['artifact_path'] not in owned:
            raise TargetContentIssue('target_formal_local_input_ownership_invalid')
        matches = [entry for entry in entries if entry['declared_relative_path'] == choice['artifact_path']
                   and entry['role'] in {'checkpoint', 'analysis', 'data', 'log'}]
        if len(matches) != 1:
            raise TargetContentIssue('target_formal_local_input_path_not_bound')
        match = matches[0]
        if match.get('selection_protocol') == SELECTION_MANIFEST_SCHEMA and match.get('path') is not None:
            from meta_research.resource_selections import content_selection_ref
            result.append(content_selection_ref({'asset_binding': match['binding'], 'path': match['path']}))
        else:
            result.append(match['binding']['version_ref'])
    if len(result) != len(set(result)):
        raise TargetContentIssue('target_formal_local_input_invalid')
    return result


def local_input_selections(document, entries, run):
    """Record v2 local scopes in the signed Run input contract, alongside refs."""
    resolve_local_inputs(document, entries, run)
    from meta_research.resource_selections import content_selection_ref
    result = []
    for choice in run.get('local_inputs', []):
        matches = [entry for entry in entries if entry['declared_relative_path'] == choice['artifact_path']]
        if len(matches) != 1 or matches[0].get('selection_protocol') != SELECTION_MANIFEST_SCHEMA:
            continue
        entry = matches[0]
        selection = {'asset_binding': entry['binding'], 'path': entry.get('path')}
        result.append({**choice, **selection, 'selection_ref': content_selection_ref(selection)})
    return result
