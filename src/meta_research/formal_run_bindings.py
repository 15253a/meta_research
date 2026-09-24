"""Resolve per-execution code and local inputs from the accepted completion.

The finalizer and both formal registration paths share the existing snapshot
hash protocol. A declared revision is only an assertion about frozen bytes.
"""
from __future__ import annotations

from meta_research.owners.common import OwnerConflict, canonical_hash

SNAPSHOT_KEYS = ('ordinal', 'role', 'declared_relative_path', 'artifact_kind',
                 'media_type', 'byte_count', 'content_hash', 'tree_hash')


def implementation_binding(entries, paths=None, asserted_revision=None):
    available = [entry for entry in entries if entry['role'] == 'implementation']
    if paths is None:
        if len(available) != 1:
            raise OwnerConflict('target_formal_implementation_selection_required')
        selected = available
    else:
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)):
            raise OwnerConflict('target_formal_implementation_paths_invalid')
        selected = [entry for entry in available if entry['declared_relative_path'] in paths]
        if len(selected) != len(paths):
            raise OwnerConflict('target_formal_implementation_path_not_bound')
    if not selected:
        raise OwnerConflict('target_root_implementation_missing')
    tree_hash = implementation_set_hash(selected)
    revision = 'target_impl_' + tree_hash
    if asserted_revision is not None and asserted_revision != revision:
        raise OwnerConflict('target_formal_implementation_revision_mismatch')
    return revision, tree_hash


def implementation_set_hash(entries):
    if len(entries) == 1:
        return entries[0]['tree_hash']
    return canonical_hash({'schema_ref': 'meta-research/target-root-implementation-set/v1',
        'artifacts': [{key: entry[key] for key in SNAPSHOT_KEYS} for entry in entries]})


def resolve_local_inputs(document, entries, run):
    """Resolve only earlier producers in the original declared execution order."""
    selections = run.get('local_inputs', [])
    if not isinstance(selections, list) or len(selections) > 100:
        raise OwnerConflict('target_formal_local_input_invalid')
    declared = document.get('formal_runs', [])
    indexes = {item['run_key']: index for index, item in enumerate(declared)
               if isinstance(item, dict) and isinstance(item.get('run_key'), str)}
    current = indexes.get(run.get('run_key'))
    result = []
    for choice in selections:
        if (not isinstance(choice, dict) or set(choice) != {'producer_run_key', 'artifact_path'}
                or not all(isinstance(value, str) and value for value in choice.values())):
            raise OwnerConflict('target_formal_local_input_invalid')
        producer_index = indexes.get(choice['producer_run_key'])
        if current is None or producer_index is None or producer_index >= current:
            raise OwnerConflict('target_formal_local_input_order_invalid')
        producer = declared[producer_index]
        if producer.get('variant_run_ref') or producer.get('status', 'executed') not in {'executed', 'failed'}:
            raise OwnerConflict('target_formal_local_input_producer_invalid')
        owned = [*(producer.get('artifact_paths') or []), *(producer.get('checkpoint_paths') or [])]
        if choice['artifact_path'] not in owned:
            raise OwnerConflict('target_formal_local_input_ownership_invalid')
        matches = [entry for entry in entries if entry['declared_relative_path'] == choice['artifact_path']
                   and entry['role'] in {'checkpoint', 'analysis', 'data', 'log'}]
        if len(matches) != 1:
            raise OwnerConflict('target_formal_local_input_path_not_bound')
        result.append(matches[0]['binding']['version_ref'])
    if len(result) != len(set(result)):
        raise OwnerConflict('target_formal_local_input_invalid')
    return result
