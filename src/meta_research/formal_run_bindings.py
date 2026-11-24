"""Resolve per-execution code and local inputs from the accepted completion.

The finalizer and both formal registration paths share the current selection
fingerprint protocol. A declared revision is only an assertion about frozen bytes.
"""
from __future__ import annotations

import json

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.target_content_issue import TargetContentIssue, target_content_issue

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



def implementation_binding(entries, paths=None, asserted_revision=None, *, declared_runs=(), run_key=None):
    available = [entry for entry in entries if entry['role'] == 'implementation']
    run_label = f' for Run {run_key!r}' if run_key else ''
    if paths is None:
        available = [entry for entry in available if entry.get('path') is None]
        if len(available) != 1:
            raise target_content_issue(
                'target_formal_implementation_selection_required',
                f'implementation_paths must be supplied{run_label} when multiple implementation selections exist '
                'or no unique whole implementation directory is retained. Declare implementation_paths as a list '
                'of distinct retained implementation relative paths and finish another normal turn in this Root.',
            )
        selected = available
    else:
        if (not isinstance(paths, list) or not paths
                or any(not isinstance(path, str) for path in paths)
                or len(set(paths)) != len(paths)):
            raise target_content_issue(
                'target_formal_implementation_paths_invalid',
                f'implementation_paths{run_label} must be a nonempty list of distinct relative path strings '
                'pointing to retained implementation assets. Correct implementation_paths and finish another normal turn.',
            )
        selected = [entry for entry in available if entry['declared_relative_path'] in paths]
        if len(selected) != len(paths):
            missing = [path for path in paths if path not in {entry['declared_relative_path'] for entry in available}]
            raise target_content_issue(
                'target_formal_implementation_path_not_bound',
                f'implementation_paths{run_label} references unbound implementation paths {missing!r}. '
                'Select exact retained implementation paths from this completion and finish another normal turn.',
            )
    if not selected:
        raise OwnerConflict('target_root_implementation_missing')
    tree_hash = implementation_set_hash(selected)
    revision = 'target_impl_' + tree_hash
    if asserted_revision is not None and asserted_revision != revision:
        error = TargetContentIssue('target_formal_implementation_revision_mismatch')
        for index, run in enumerate(declared_runs):
            if isinstance(run, dict) and run.get('run_key') == run_key:
                field = f'$.formal_runs[{index}].implementation_revision_ref'
                supplied = json.dumps(asserted_revision, ensure_ascii=False)
                if len(supplied) > 256:
                    supplied = f'a value of {len(asserted_revision)} characters'
                error.feedback = (
                    f'{field} asserts {supplied}. '
                    f'The accepted implementation selection for this Run resolves to {revision}. '
                    'Check implementation_paths against the code actually used by this Run. '
                    'This revision assertion is optional: omit it to let the Owner bind the frozen '
                    'selection, or provide its verified revision. Preserve the recorded work.')
                break
        if not getattr(error, 'feedback', None):
            error.feedback = (
                f'implementation_revision_ref asserts {asserted_revision!r}, but the accepted implementation '
                f'selection{run_label} resolves to {revision!r}. Omit implementation_revision_ref or update it.'
            )
        raise error
    return revision, tree_hash


def implementation_set_hash(entries):
    if not entries or any(entry.get('selection_protocol') != SELECTION_MANIFEST_SCHEMA for entry in entries):
        raise OwnerConflict('target_root_manifest_protocol_unsupported')
    return canonical_hash({'schema_ref': 'meta-research/target-root-implementation-set/v2',
        'artifacts': sorted([{'path': entry['declared_relative_path'],
            'kind': entry['artifact_kind'], 'tree_hash': entry['tree_hash']}
            for entry in entries], key=lambda value: value['path'])})


def resolve_local_inputs(document, entries, run):
    """Resolve earlier producers once into input refs and signed selection scopes."""
    from meta_research.resource_selections import content_selection_ref

    selections = run.get('local_inputs', [])
    run_key = run.get('run_key')
    if not isinstance(selections, list) or len(selections) > 100:
        raise target_content_issue(
            'target_formal_local_input_invalid',
            f'local_inputs for Run {run_key!r} must be a list of at most 100 objects with '
            'producer_run_key and artifact_path. Correct local_inputs and finish another normal turn.',
        )
    declared = document.get('formal_runs', [])
    indexes = {item['run_key']: index for index, item in enumerate(declared)
               if isinstance(item, dict) and isinstance(item.get('run_key'), str)}
    current = indexes.get(run_key)
    refs, scopes = [], []
    for choice in selections:
        if (not isinstance(choice, dict) or set(choice) != {'producer_run_key', 'artifact_path'}
                or not all(isinstance(value, str) and value for value in choice.values())):
            raise target_content_issue(
                'target_formal_local_input_invalid',
                f'Each local_inputs item in Run {run_key!r} must be an object with exact nonempty strings '
                'producer_run_key and artifact_path. Correct local_inputs and finish another normal turn.',
            )
        producer_index = indexes.get(choice['producer_run_key'])
        if current is None or producer_index is None or producer_index >= current:
            raise target_content_issue(
                'target_formal_local_input_order_invalid',
                f'Run {run_key!r} local_inputs references producer_run_key {choice["producer_run_key"]!r}, '
                'which must appear strictly earlier in $.formal_runs. Reorder or fix local_inputs and finish another turn.',
            )
        producer = declared[producer_index]
        if producer.get('variant_run_ref') or producer.get('status', 'executed') not in {'executed', 'failed'}:
            raise target_content_issue(
                'target_formal_local_input_producer_invalid',
                f'Run {run_key!r} local_inputs references producer_run_key {choice["producer_run_key"]!r}, '
                'which must be a new executed/failed Run (not a reused variant_run_ref or unexecuted Run).',
            )
        owned = [*(producer.get('artifact_paths') or []), *(producer.get('checkpoint_paths') or [])]
        if choice['artifact_path'] not in owned:
            raise target_content_issue(
                'target_formal_local_input_ownership_invalid',
                f'Run {run_key!r} local_inputs path {choice["artifact_path"]!r} is not declared in '
                f'producer {choice["producer_run_key"]!r} artifact_paths or checkpoint_paths.',
            )
        matches = [entry for entry in entries if entry['declared_relative_path'] == choice['artifact_path']
                   and entry['role'] in {'checkpoint', 'analysis', 'data', 'log'}]
        if len(matches) != 1:
            raise target_content_issue(
                'target_formal_local_input_path_not_bound',
                f'Run {run_key!r} local_inputs path {choice["artifact_path"]!r} is not uniquely bound '
                'to a retained checkpoint, analysis, data, or log asset in this completion.',
            )
        match = matches[0]
        selection = {'asset_binding': match['binding'], 'path': match['path']}
        selection_ref = content_selection_ref(selection)
        refs.append(selection_ref if match['path'] is not None else match['binding']['version_ref'])
        scopes.append({**choice, **selection, 'selection_ref': selection_ref})
    if len(refs) != len(set(refs)):
        raise target_content_issue(
            'target_formal_local_input_invalid',
            f'Run {run_key!r} local_inputs contains duplicate local input selections resolving to the same asset. '
            'Remove duplicates and finish another normal turn.',
        )
    return refs, scopes
