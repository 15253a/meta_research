"""Transaction-local persistence for formal work, independent of execution phases.

Owners verify authority and exact source receipts before using these writers.
The root and execution-port adapters share the same native entity tables.
"""
from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass
from sqlalchemy import text
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.target_content_issue import TargetContentIssue, target_content_issue

_REUSED_EVALUATION_SOURCES = ContextVar('reused_evaluation_sources', default=frozenset())


def verified_target_input_asset_refs(owner, *, target_ref, proofs, target_commit_refs=()):
    """Resolve exact input proofs, their selected aliases, and frozen commits."""
    from meta_research.bundle_protocol import projection_plain_value

    refs = set()
    direct_assets = []
    for proof in proofs:
        if owner is None:
            raise OwnerConflict("target_launch_asset_proof_verifier_unavailable")
        source_ref = proof.get("selection_ref") or proof["asset_ref"]
        verified = owner.query_bundle_input_asset_proof(target_ref=target_ref, asset_ref=source_ref)
        if verified is None or projection_plain_value(verified) != proof:
            raise OwnerConflict("target_run_input_asset_proof_invalid")
        if proof.get("selection_ref"):
            refs.add(source_ref)
            continue
        projection = owner.query_target_input_asset_projection(
            target_ref=target_ref, asset_ref=proof["asset_ref"])
        if projection is None or projection.target_ref != target_ref:
            raise OwnerConflict("target_run_input_asset_proof_invalid")
        refs.update((projection.asset.asset_ref, projection.asset.version_ref))
        direct_assets.append(projection.asset)
    alias_reader = getattr(owner, "query_target_input_asset_aliases", None)
    if direct_assets and callable(alias_reader):
        refs.update(alias_reader(target_ref=target_ref, assets=tuple(direct_assets)))
    if target_commit_refs and owner is not None:
        bindings = owner.query_target_commit_input_asset_bindings(
            target_ref=target_ref, target_commit_refs=tuple(target_commit_refs))
        for assets in bindings.values():
            for asset in assets:
                refs.update((asset.asset_ref, asset.version_ref))
    return refs


def insert_variant_run(connection, values):
    _insert(connection, 'rg_variant_runs', values)


def insert_evaluation_attempt(connection, values):
    _insert(connection, 'rg_evaluation_attempts', values)


def insert_metric_result(connection, values):
    _insert(connection, 'rg_metric_results', values)


def _insert(connection, table, values):
    # Only internal adapters supply identifiers; all research values are bound.
    columns = ', '.join(values)
    placeholders = ', '.join(':' + key for key in values)
    connection.execute(text(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})'), values)


def _ref(kind, *parts):
    return kind + '_' + canonical_hash(list(parts))[:32]


def _receipt(kind, subject_ref, bindings):
    from meta_research.owners.research_graph import RECEIPT_SCHEMA
    return canonical_hash({'schema_ref': RECEIPT_SCHEMA, 'issuer': 'research_graph', 'kind': kind,
                           'subject_ref': subject_ref, 'bindings': bindings})


def explicit_unexecuted_root_evidence(result_document):
    """Recognize explicit frozen non-execution, never infer it from a metric label."""
    hierarchy = result_document.get('execution_hierarchy')
    admission = result_document.get('input_admission')
    if not isinstance(hierarchy, dict) or not isinstance(admission, dict):
        return None
    run, attempt = hierarchy.get('variant_run'), hierarchy.get('evaluation_attempt')
    if (not isinstance(run, dict) or run.get('status') != 'not_instantiated'
            or not isinstance(attempt, dict) or attempt.get('status') != 'not_started'
            or any(type(admission.get(key)) is not int or admission[key] != 0
                   for key in ('variant_run_count', 'evaluation_attempt_count'))
            or any(admission.get(key) is not False for key in
                   ('training_process_started', 'evaluation_process_started'))):
        return None
    return {
        'execution_hierarchy': {'variant_run': {'status': 'not_instantiated'},
                                'evaluation_attempt': {'status': 'not_started'}},
        'input_admission': {key: admission[key] for key in (
            'variant_run_count', 'evaluation_attempt_count',
            'training_process_started', 'evaluation_process_started')},
    }


def _checkpoint_paths(value, *, verify_only=False):
    if value is not None and (not isinstance(value, list)
            or any(not isinstance(path, str) or not path or path != path.strip() for path in value)
            or len(set(value)) != len(value)):
        raise target_content_issue(
            'target_formal_checkpoint_paths_invalid',
            'checkpoint_paths must be omitted or a list of distinct nonempty trimmed path/ref strings.',
            verify_only=verify_only,
        )
    return value


def _artifact_paths(value):
    from meta_research.target_implementation_bundle import (
        TargetImplementationBundleError, validate_bundle_relative_path,
    )
    try:
        _checkpoint_paths(value)
        for path in value or []:
            validate_bundle_relative_path(path)
    except (OwnerConflict, TargetImplementationBundleError) as cause:
        error = TargetContentIssue('target_root_commit_domain_invalid')
        error.feedback = (
            'Run and Evaluation artifact_paths must be a list of distinct canonical '
            'relative workspace paths, without traversal, absolute prefixes or unsupported characters. '
            'Use the exact existing log, analysis or data files or directories produced by that work. '
            'Correct the declaration, preserve all files and finish another normal root turn.'
        )
        raise error from cause
    return value


def explicit_run_only(document, *, work=None):
    """Classify explicit actual work with no completed assessment yet.

    Failed attempts are real attempts but supply no MetricResult. Full item,
    identity and provenance validation still occurs before the RG transaction.
    """
    if 'formal_runs' not in document or explicit_unexecuted_root_evidence(document) is not None:
        return False
    try:
        work = _declared_work(document) if work is None else work
    except OwnerConflict:
        return False
    return all(attempt is None or attempt.get('status') == 'failed' for _, attempt in work)


def _declaration_value_summary(value):
    if isinstance(value, (list, dict)):
        return type(value).__name__ + ' with ' + str(len(value)) + ' entries'
    rendered = repr(value)
    return type(value).__name__ + ' ' + (rendered[:120] + '...' if len(rendered) > 120 else rendered)


def _declared_work(result_document):
    declared = result_document.get('formal_runs')
    if declared is None:
        declared = [{'run_key': 'primary', 'evaluations': [
            {'attempt_key': 'primary', 'metrics': result_document.get('metrics')}]}]
    problems = []
    problem_code = 'target_formal_work_inventory_invalid'

    def problem(path, value, requirement):
        problems.append(path + ': received ' + _declaration_value_summary(value) + '; ' + requirement + '.')

    def paths(value, path, validator):
        nonlocal problem_code
        try:
            validator(value)
        except OwnerConflict as error:
            if not problems:
                problem_code = error.code
            problem(path, value, getattr(error, 'feedback', 'must be a list of distinct valid workspace paths'))

    def reject_problems():
        if not problems:
            return
        # The existing root feedback channel admits at most 4096 characters.
        selected, used = [], 0
        for message in problems:
            if used + len(message) + 1 > 3000:
                break
            selected.append(message)
            used += len(message) + 1
        omitted = len(problems) - len(selected)
        error = TargetContentIssue(problem_code)
        error.feedback = ('\n'.join(selected)
            + (f'\n{omitted} additional declaration errors omitted from this bounded feedback.' if omitted else '')
            + '\nCorrect the declaration and finish another normal turn in the same Root. '
            'For lists over the limit of 100, merge overly fine Run or Evaluation declarations only '
            'according to the actual research. Preserve complete research results, original evidence '
            'and sources; do not invent Runs, discard evidence or arbitrarily average results. '
            'The whole declaration is rejected without truncation or partial acceptance. '
            'Keep existing artifacts and checkpoints; this correction does not require repeating the research.')
        raise error

    if not isinstance(declared, list) or not declared:
        problem('$.formal_runs', declared, 'must be a nonempty list of actual Runs; limit 100')
        reject_problems()
    if len(declared) > 100:
        problem('$.formal_runs', declared, f'actual count {len(declared)} exceeds the limit 100')
        # Report independent per-Run errors too; never accept a prefix of this declaration.
    measured, failed, unassessed, keys = [], [], [], set()
    for run_ordinal, run in enumerate(declared):
        run_path = '$.formal_runs[' + str(run_ordinal) + ']'
        if not isinstance(run, dict):
            problem(run_path, run, 'must be an object describing this actual Run')
            continue
        state = run.get('status', 'executed')
        if not isinstance(state, str) or state not in {'executed', 'failed', 'blocked', 'cancelled', 'not_executed'}:
            problem(run_path + '.status', state,
                'must be executed, failed, blocked, cancelled or not_executed according to the actual work')
            continue
        if state in {'blocked', 'cancelled', 'not_executed'}:
            continue
        key = run.get('run_key')
        if not isinstance(key, str) or not key or len(key) > 128 or key in keys:
            problem(run_path + '.run_key', key,
                'must be a nonempty string of at most 128 characters, unique among the actual Runs')
        else:
            keys.add(key)
        implementation_revision_ref = run.get('implementation_revision_ref')
        if implementation_revision_ref is not None and (
                not isinstance(implementation_revision_ref, str)
                or not implementation_revision_ref or len(implementation_revision_ref) > 1024):
            problem(run_path + '.implementation_revision_ref', implementation_revision_ref,
                'must be omitted or a nonempty exact revision string of at most 1024 characters')
        paths(run.get('checkpoint_paths'), run_path + '.checkpoint_paths', _checkpoint_paths)
        paths(run.get('artifact_paths'), run_path + '.artifact_paths', _artifact_paths)
        evaluations = run.get('evaluations', [])
        if not isinstance(evaluations, list):
            problem(run_path + '.evaluations', evaluations,
                'must be a list of actual evaluations; limit 100; use [] for a Run not yet assessed')
            continue
        if len(evaluations) > 100:
            problem(run_path + '.evaluations', evaluations,
                f'actual count {len(evaluations)} exceeds the per-Run limit 100')
        attempt_keys = set()
        for attempt_ordinal, attempt in enumerate(evaluations):
            attempt_path = run_path + '.evaluations[' + str(attempt_ordinal) + ']'
            if not isinstance(attempt, dict):
                problem(attempt_path, attempt, 'must be an object describing this actual evaluation')
                continue
            status = attempt.get('status', 'executed')
            if not isinstance(status, str) or status not in {'executed', 'failed', 'blocked', 'cancelled', 'not_executed'}:
                problem(attempt_path + '.status', status,
                    'must be executed, failed, blocked, cancelled or not_executed according to the actual assessment')
                continue
            if status in {'blocked', 'cancelled', 'not_executed'}:
                continue
            key, metrics = attempt.get('attempt_key'), attempt.get('metrics')
            if not isinstance(key, str) or not key or len(key) > 128 or key in attempt_keys:
                problem(attempt_path + '.attempt_key', key,
                    'must be a nonempty string of at most 128 characters, unique within this Run')
            else:
                attempt_keys.add(key)
            if ((status == 'executed' and not isinstance(metrics, dict))
                    or (status == 'failed' and metrics is not None and not isinstance(metrics, dict))):
                problem(attempt_path + '.metrics', metrics,
                    'must be an object for an executed assessment; a failed assessment may omit it')
            if status == 'failed' and (attempt.get('evaluation_attempt_ref') or attempt.get('metric_result_ref')):
                raise target_content_issue(
                    'target_formal_reused_evaluation_invalid',
                    f"{attempt_path}: a failed evaluation must not specify evaluation_attempt_ref or metric_result_ref. "
                    "Omit those reused refs or record status='executed' when reusing an accepted evaluation.",
                )
            paths(attempt.get('checkpoint_paths'), attempt_path + '.checkpoint_paths', _checkpoint_paths)
            paths(attempt.get('artifact_paths'), attempt_path + '.artifact_paths', _artifact_paths)
            (measured if status == 'executed' else failed).append((run, attempt))
        if not attempt_keys:
            unassessed.append((run, None))
    reject_problems()
    if not measured and not failed and not unassessed:
        raise target_content_issue(
            'target_formal_work_has_no_executed_result',
            '$.formal_runs contains no executed or failed Run (all declared entries were skipped). '
            'Either mark the unexecuted work explicitly via unexecuted_root_evidence or include an executed Run.',
        )
    return [*measured, *failed, *unassessed]


def has_variant_declaration(result_document, run_key):
    """Whether one declared run carries its own method identity declaration."""
    declared = result_document.get('formal_runs') if isinstance(result_document, dict) else None
    if not isinstance(declared, list):
        return False
    for run in declared:
        if isinstance(run, dict) and run.get('run_key') == run_key:
            return bool(run.get('variant_ref')) or _variant_declaration(run) is not None
    return False


def _variant_declaration(run, *, path=None):
    """A run may carry its actual method identity instead of the bundle draft.

    ``baseline_forward_contract`` follows the same envelope the Bundle
    acceptance path already uses (an exact ``baseline_ref`` or a named
    method_key/version/contract); ``variant_recipe`` is the concrete recipe.
    Both are optional and independent of run/attempt identity fields.
    """
    forward = run.get('baseline_forward_contract')
    recipe = run.get('variant_recipe')
    if forward is None and recipe is None:
        return None
    if not isinstance(forward, dict) or not isinstance(recipe, dict) or not recipe:
        error = TargetContentIssue('target_formal_variant_declaration_invalid')
        run_key = run.get('run_key')
        location = path or '$.formal_runs entry'
        error.feedback = (
            f"{location} (Run {run_key!r}) has an incomplete variant declaration. "
            "baseline_forward_contract and variant_recipe must be supplied together. "
            "baseline_forward_contract must be an object and variant_recipe must be a "
            "nonempty object. Correct this formal_runs entry, preserve the actual "
            "work and artifacts, and finish another normal turn in the same Root."
        )
        raise error
    return {'forward': forward, 'recipe': recipe}


def _lookup_declared_baseline(connection, forward):
    """Resolve a declared contract to an existing Baseline without writing."""
    from meta_research.baseline_identity import _method_envelope, verify_baseline_method_identity
    exact = connection.execute(text(
        'SELECT * FROM rg_experiment_baselines WHERE forward_contract_hash = :hash'),
        {'hash': canonical_hash(forward)}).first()
    if exact is not None:
        method = connection.execute(text(
            'SELECT * FROM rg_baseline_method_versions WHERE baseline_ref = :ref'),
            {'ref': exact.baseline_ref}).first()
        verify_baseline_method_identity(forward, exact, method)
        return str(exact.baseline_ref)
    envelope = _method_envelope(forward)
    if envelope is not None and 'baseline_ref' in envelope:
        row = connection.execute(text(
            'SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref'),
            {'ref': envelope['baseline_ref']}).first()
        if row is not None:
            method = connection.execute(text(
                'SELECT * FROM rg_baseline_method_versions WHERE baseline_ref = :ref'),
                {'ref': row.baseline_ref}).first()
            verify_baseline_method_identity(forward, row, method)
            return str(row.baseline_ref)
    if envelope is not None:
        method = connection.execute(text(
            'SELECT * FROM rg_baseline_method_versions WHERE method_key = :method_key '
            'AND method_version = :method_version'), envelope).first()
        if method is not None:
            row = connection.execute(text(
                'SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref'),
                {'ref': method.baseline_ref}).first()
            verify_baseline_method_identity(forward, row, method)
            return str(method.baseline_ref)
    raise OwnerConflict('target_formal_entity_missing')


def _declared_method_content_conflict(connection, *, run_key, forward, error, selector):
    """Turn one correctable declaration conflict into Target correction feedback.

    Only the live registration of a declared method name/version is revisable.
    Integrity, unknown-identity and verification-only faults stay OwnerConflict.
    The Owner rejection signs this text alone, so the declared identity, the
    registered contract hash and the exact declaration path live in it.
    """
    from meta_research.baseline_identity import BASELINE_METHOD_REJECTION_FEEDBACK

    method_key = forward.get('method_key')
    method_version = forward.get('method_version')
    # The conflict is raised only for an already verified envelope, so a supplied
    # contract is a nonempty object and the registry row was verified moments ago
    # in this same transaction: read its hash as it stands. A reference-only
    # declaration may name its version only by reference, and a legacy row may
    # have no registered method version at all.
    submitted = (canonical_hash(forward['method_contract'])
                 if 'method_contract' in forward else None)
    if method_key is None:
        row = connection.execute(text(
            'SELECT method_key, method_version, method_contract_hash '
            'FROM rg_baseline_method_versions WHERE baseline_ref = :ref'),
            {'ref': forward['baseline_ref']}).first()
    else:
        row = connection.execute(text(
            'SELECT method_key, method_version, method_contract_hash '
            'FROM rg_baseline_method_versions WHERE method_key = :method_key '
            'AND method_version = :method_version'),
            {'method_key': method_key, 'method_version': method_version}).first()
    registered = None if row is None else row.method_contract_hash
    if row is None and 'baseline_ref' in forward:
        # A byte-addressed legacy Baseline keeps its contract in the Baseline
        # itself; that stored hash is the one the declaration must match.
        registered = connection.execute(text(
            'SELECT forward_contract_hash FROM rg_experiment_baselines '
            'WHERE baseline_ref = :ref'), {'ref': forward['baseline_ref']}).scalar_one_or_none()
    issue = TargetContentIssue(error.code, {
        'run_key': run_key,
        'selector': selector,
        'method_key': method_key if row is None else row.method_key,
        'method_version': method_version if row is None else row.method_version,
        'registered_method_contract_hash': registered,
        'submitted_method_contract_hash': submitted,
    })
    declared = ', '.join(part for part in (
        f'method_key {method_key!r}' if method_key is not None else None,
        f'method_version {method_version!r}' if method_version is not None else None,
        f'baseline_ref {forward["baseline_ref"]!r}' if 'baseline_ref' in forward else None,
    ) if part is not None)
    if row is not None and method_key is None:
        declared += (f', registered as method_key {row.method_key!r} '
                     f'method_version {row.method_version!r}')
    issue.feedback = (
        f"{selector} (Run {run_key!r}) declares {declared}, a method version that is "
        f"already registered with different immutable semantics. "
        f"{BASELINE_METHOD_REJECTION_FEEDBACK[error.code]} "
        f"Registered method_contract hash: {registered or 'unavailable'}; "
        f"submitted method_contract hash: {submitted or 'unavailable'}. "
        "Reuse the registered contract exactly for this run, or finish this Root "
        "with a new method_version when the meaning differs."
    )
    return issue


def _declared_method_identity_issue(*, run_key, forward, error, selector):
    """Turn a revisable method declaration error into root correction feedback.

    ``resolve_baseline_method_identity`` also validates the identity envelope
    before it can register a Baseline.  Those validation failures describe the
    Target's declaration, so they must travel through the same durable root
    rejection path as an immutable method-version conflict.  Integrity and
    execution failures are intentionally left out by the caller.
    """
    from meta_research.baseline_identity import (
        BASELINE_METHOD_REJECTION_FEEDBACK, _ENVELOPE_KEYS,
    )

    method_key = forward.get('method_key')
    method_version = forward.get('method_version')
    declared = ', '.join(part for part in (
        f'method_key {method_key!r}' if method_key is not None else None,
        f'method_version {method_version!r}' if method_version is not None else None,
        f'baseline_ref {forward["baseline_ref"]!r}'
        if 'baseline_ref' in forward else None,
        'method_contract present' if 'method_contract' in forward else 'method_contract missing',
    ) if part is not None)
    unexpected = sorted(set(forward) - _ENVELOPE_KEYS)
    if unexpected:
        declared += f', unexpected fields {unexpected!r}'
    issue = TargetContentIssue(error.code, {
        'run_key': run_key,
        'selector': selector,
        'method_key': method_key,
        'method_version': method_version,
        'baseline_ref': forward.get('baseline_ref'),
    })
    issue.feedback = (
        f"{selector} (Run {run_key!r}) declares {declared or 'no method identity'}. "
        f"{BASELINE_METHOD_REJECTION_FEEDBACK[error.code]} "
        "Correct this declaration and finish another normal turn in the same Root. "
        "Preserve the valid artifacts and actual work; this declaration correction "
        "does not require repeating the research."
    )
    return issue


def _resolve_declared_variants(connection, *, items, result_document, root, accepted_at, verify_only):
    """Give declared work items their real Baseline/Variant identity.

    The Target, not the Bundle draft, owns the actual method attribution: a
    declared contract resolves or registers the Baseline through the same RG
    seam the Bundle uses, then gets-or-creates the Variant by recipe hash.
    """
    declared = result_document.get('formal_runs')
    if not isinstance(declared, list):
        declared = []
    declarations = {}
    for ordinal, run in enumerate(declared):
        if isinstance(run, dict) and run.get('status') in {'blocked', 'cancelled', 'not_executed'}:
            continue
        if isinstance(run, dict) and run.get('run_key'):
            declarations[run['run_key']] = (
                _variant_declaration(
                    run, path='$.formal_runs[' + str(ordinal) + ']'),
                '$.formal_runs[' + str(ordinal) + ']')
    if not any(declaration is not None for declaration, _ in declarations.values()):
        return
    from meta_research.baseline_identity import (
        BASELINE_METHOD_REJECTION_FEEDBACK, resolve_baseline_method_identity,
    )
    from meta_research.owners.research_graph import _get_or_create_target_measurement_identity
    quest_ref = connection.execute(text(
        'SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref '
        'WHERE t.target_ref=:ref'), {'ref': root['target_ref']}).scalar_one_or_none()
    if not isinstance(quest_ref, str) or not quest_ref:
        raise OwnerConflict('target_formal_variant_declaration_invalid')
    for item in items:
        selected = declarations.get(item['run_key'])
        if selected is None:
            continue
        declaration, selector = selected
        if declaration is None:
            continue
        if item['reuse_variant_run']:
            raise target_content_issue(
                'target_formal_variant_declaration_invalid',
                f"{selector} (Run {item['run_key']!r}) combines variant_run_ref reuse with an inline variant "
                "declaration (baseline_forward_contract/variant_recipe). Remove the inline declaration when reusing a VariantRun.",
                verify_only=verify_only,
            )
        forward, recipe = declaration['forward'], declaration['recipe']
        if verify_only:
            # Verification only resolves what live acceptance registered;
            # it never registers a new identity itself.
            baseline_ref = _lookup_declared_baseline(connection, forward)
            row = connection.execute(text(
                'SELECT variant_ref FROM rg_experiment_variants WHERE baseline_ref=:baseline '
                'AND recipe_hash=:hash'), {'baseline': baseline_ref,
                                           'hash': canonical_hash(recipe)}).first()
            if row is None:
                raise OwnerConflict('target_formal_entity_missing')
            item['variant_ref'] = str(row[0])
            continue
        try:
            baseline_ref, _created = resolve_baseline_method_identity(
                connection, forward=forward, quest_ref=quest_ref, accepted_at=accepted_at)
        except OwnerConflict as error:
            if error.code == 'baseline_method_version_content_conflict':
                raise _declared_method_content_conflict(
                    connection, run_key=item['run_key'], forward=forward, error=error,
                    selector=selector) from None
            if error.code in BASELINE_METHOD_REJECTION_FEEDBACK:
                raise _declared_method_identity_issue(
                    run_key=item['run_key'], forward=forward, error=error,
                    selector=selector) from None
            raise
        variant_ref, _variant_created = _get_or_create_target_measurement_identity(
            connection, table='rg_experiment_variants', ref_column='variant_ref',
            ref_prefix='variant', natural={'baseline_ref': baseline_ref,
                                            'recipe_hash': canonical_hash(recipe)},
            immutable={'recipe_json': canonical_json(recipe)},
            insert_only={'accepted_at': accepted_at})
        item['variant_ref'] = variant_ref


def _derive_variant_evaluations(connection, *, items, identities, root, accepted_at, verify_only):
    """Pair each evaluated item's real Variant with the authority protocol.

    An item whose actual Variant differs from the Bundle draft cannot reuse the
    draft's Evaluation (that pair belongs to another Variant). The Evaluation
    for (item Variant x authority ProtocolVersion) is get-or-created instead;
    an explicitly supplied evaluation_ref keeps its own pairing and is
    validated by the definition join as before.
    """
    pending = [item for item in items
               if item['evaluation_ref'] == identities['evaluation_ref']
               and item['evaluation_attempt_ref'] is not None
               and not item.get('evaluation_ref_explicit')
               and item['variant_ref'] != identities['variant_ref']]
    if not pending:
        return
    from meta_research.owners.research_graph import _get_or_create_target_measurement_identity
    for item in pending:
        natural = {'variant_ref': item['variant_ref'],
                   'protocol_version_ref': identities['protocol_version_ref']}
        if verify_only:
            row = connection.execute(text(
                'SELECT evaluation_ref FROM rg_evaluations WHERE variant_ref=:variant '
                'AND protocol_version_ref=:protocol'),
                {'variant': natural['variant_ref'],
                 'protocol': natural['protocol_version_ref']}).first()
            if row is None:
                raise OwnerConflict('target_formal_entity_missing')
            item['evaluation_ref'] = str(row[0])
            continue
        item['evaluation_ref'], _created = _get_or_create_target_measurement_identity(
            connection, table='rg_evaluations', ref_column='evaluation_ref',
            ref_prefix='evaluation', natural=natural, immutable={},
            insert_only={'accepted_at': accepted_at})


def root_work_items(*, payload, identities, result_document):
    """Resolve real executions and assessments independently of Target grain.

    A measured fact leads the handoff when present. Otherwise an actual failed
    attempt or an unassessed execution is sufficient to finish this work unit.
    """
    return _root_work_items_from_declared(
        work=_declared_work(result_document),
        payload=payload,
        identities=identities,
        declared_runs=result_document.get('formal_runs') if isinstance(result_document, dict) else None,
    )


def _root_work_items_from_declared(*, work, payload, identities, declared_runs=None):
    primary_key = work[0][0]['run_key']
    run_paths = (
        {id(run): '$.formal_runs[' + str(index) + ']' for index, run in enumerate(declared_runs)}
        if isinstance(declared_runs, list) else {}
    )
    items = []
    for ordinal, (run, attempt) in enumerate(work):
        run_key = run['run_key']
        run_path = run_paths.get(id(run))
        declaration = _variant_declaration(run, path=run_path)
        if declaration is not None and (run.get('variant_ref') or run.get('variant_run_ref')):
            # A reused run keeps its original Variant; an explicit variant_ref
            # needs no separate declaration for the same fact.
            error = TargetContentIssue('target_formal_variant_declaration_invalid')
            location = run_path or '$.formal_runs entry'
            error.feedback = (
                f"{location} (Run {run_key!r}) combines an inline variant declaration "
                "(baseline_forward_contract/variant_recipe) with an explicit variant_ref or "
                "variant_run_ref. Supply either the explicit reference or the inline declaration, "
                "not both. Correct this formal_runs entry, preserve the actual work and artifacts, "
                "and finish another normal turn in the same Root."
            )
            raise error
        run_ref = run.get('variant_run_ref') or (payload['variant_run_ref'] if run_key == primary_key
            else _ref('variant_run', payload['measurement_ref'], run_key))
        status = 'pending' if attempt is None else ('failed' if attempt.get('status') == 'failed' else 'completed')
        attempt_key = attempt['attempt_key'] if attempt is not None else None
        reused_attempt = attempt.get('evaluation_attempt_ref') if attempt is not None else None
        reused_metric = attempt.get('metric_result_ref') if attempt is not None else None
        if bool(reused_attempt) != bool(reused_metric):
            raise target_content_issue(
                'target_formal_reused_evaluation_invalid',
                f"Run {run_key!r}, Evaluation {attempt_key!r} must supply both evaluation_attempt_ref and "
                "metric_result_ref together when reusing an accepted evaluation, or omit both.",
            )
        items.append({
            'ordinal': ordinal, 'run_key': run_key, 'attempt_key': attempt_key,
            'run_status': run.get('status', 'executed'), 'evaluation_status': status,
            'variant_ref': run.get('variant_ref', identities['variant_ref']),
            'evaluation_ref': attempt.get('evaluation_ref', identities['evaluation_ref']) if attempt is not None else None,
            'variant_run_ref': run_ref, 'reuse_variant_run': bool(run.get('variant_run_ref')),
            'evaluation_attempt_ref': None if attempt is None else (reused_attempt or (
                payload['evaluation_attempt_ref'] if ordinal == 0 else
                _ref('evaluation_attempt', payload['measurement_ref'], run_key, attempt_key))),
            'metric_result_ref': (reused_metric or (payload['metric_result_ref'] if ordinal == 0 else
                _ref('metric_result', payload['measurement_ref'], run_key, attempt_key))) if status == 'completed' else None,
            'reuse_evaluation_attempt': bool(reused_attempt),
            'metrics': attempt['metrics'] if status == 'completed' else None,
            'input_refs': run.get('input_refs'), 'checkpoint_paths': run.get('checkpoint_paths'),
            'implementation_revision_ref': run.get('implementation_revision_ref'),
            'implementation_paths': run.get('implementation_paths'),
            'local_inputs': run.get('local_inputs', []),
            'evaluation_ref_explicit': attempt is not None and 'evaluation_ref' in attempt,
            'checkpoint_role_refs': run.get('checkpoint_role_refs'),
            'checkpoint_version_refs': run.get('checkpoint_version_refs'),
            'evaluation_checkpoint_role_refs': attempt.get('checkpoint_role_refs') if attempt is not None else None,
            'evaluation_checkpoint_version_refs': attempt.get('checkpoint_version_refs') if attempt is not None else None,
            'evaluation_checkpoint_paths': attempt.get('checkpoint_paths') if attempt is not None else None,
            'artifact_paths': run.get('artifact_paths'),
            'evaluation_artifact_paths': attempt.get('artifact_paths') if attempt is not None else None,
        })
    if items[0]['variant_run_ref'] != payload['variant_run_ref']:
        raise OwnerConflict('target_formal_primary_run_reference_invalid')
    return items


def formal_inventory(items):
    return [{k: v for k, v in item.items() if k not in ('metrics', 'input_refs')}
            | {'metrics_hash': canonical_hash(item['metrics']) if item['metrics'] is not None else None}
            for item in items]


def primary_reused_run(result_document):
    return primary_reused_references(result_document).get('variant_run_ref')


def primary_work_metrics(document, *, work=None):
    """The selected actual assessment owns metrics; summaries need not repeat them."""
    if 'formal_runs' not in document:
        return document.get('metrics', {})
    _, attempt = (_declared_work(document) if work is None else work)[0]
    return attempt['metrics'] if attempt is not None and attempt.get('status', 'executed') == 'executed' else {}


def primary_evaluation_status(document, *, work=None):
    _, attempt = (_declared_work(document) if work is None else work)[0]
    return 'pending' if attempt is None else ('failed' if attempt.get('status') == 'failed' else 'completed')


@dataclass(frozen=True, slots=True)
class PrimaryMetricKeyCheck:
    """One deterministic verdict on the selected primary evaluation's metric keys.

    Names only: this is the frozen Protocol's key set against the one draft, not
    an assessment of values, artifacts or scientific adequacy.
    """

    run_key: str
    attempt_key: str | None
    missing_keys: tuple[str, ...]
    unexpected_keys: tuple[str, ...]
    run_only: bool

    @property
    def conforms(self) -> bool:
        return not self.missing_keys and not self.unexpected_keys


def check_primary_metric_keys(document, *, required_metric_keys, optional_metric_keys, work=None):
    """Apply exactly the metric-key predicate formal acceptance applies.

    The selected primary actual assessment owns the metrics; a summary-only
    document uses its top-level ``metrics``. Explicit actual work with no
    completed assessment yet carries no MetricResult and is not checked. Every
    other key, value, artifact and identity rule stays with its own boundary.
    """
    work = _declared_work(document) if work is None else work
    run, attempt = work[0]
    run_only = explicit_run_only(document, work=work)
    actual = primary_work_metrics(document, work=work)
    metric_keys = set(actual)
    required_keys = set(required_metric_keys)
    allowed_keys = required_keys | set(optional_metric_keys)
    return PrimaryMetricKeyCheck(
        run_key=run['run_key'],
        attempt_key=attempt['attempt_key'] if attempt is not None else None,
        missing_keys=() if run_only else tuple(sorted(required_keys - metric_keys)),
        unexpected_keys=() if run_only else tuple(sorted(metric_keys - allowed_keys)),
        run_only=run_only,
    )


def verify_report_only_assessments(document, entries, *, work=None):
    """Check report attribution before Owner acceptance can issue correction feedback."""
    work = _declared_work(document) if work is None else work
    _verify_declared_subject_artifact_bindings(work, entries)
    new_attempts = [attempt for _, attempt in work
                    if attempt is not None and not attempt.get('evaluation_attempt_ref')]
    for attempt in new_attempts:
        if attempt.get('status', 'executed') != 'executed' or attempt.get('metrics') != {}:
            continue
        paths = attempt.get('artifact_paths')
        _require_assessment_report(entries, paths or [])


def _require_assessment_report(entries, paths):
    reports = _selected_subject_artifacts(entries, paths)
    for entry in reports:
        if entry['role'] != 'analysis' or _is_target_note(entry):
            continue
        byte_count = entry['byte_count']
        metadata = entry.get('research_note')
        if (entry.get('artifact_kind') == 'directory'
                and entry.get('declared_relative_path') == 'outputs/analysis'
                and isinstance(metadata, dict) and 'other_content_bytes' in metadata):
            byte_count = metadata['other_content_bytes']
        if type(byte_count) is int and byte_count > 0:
            return
    raise OwnerConflict('target_formal_evaluation_report_required')


def _is_target_note(entry):
    """Only a canonical note file or a proven note-only snapshot is Target content."""
    from meta_research.research_notes import FINAL_STATEMENT_PATH

    metadata = entry.get('research_note')
    if entry.get('role') != 'analysis' or not isinstance(metadata, dict) or not metadata:
        return False
    if entry.get('artifact_kind') == 'file':
        return entry.get('declared_relative_path') in {
                'outputs/analysis/research-note.md', FINAL_STATEMENT_PATH,
            }
    return (entry.get('artifact_kind') == 'directory'
            and entry.get('declared_relative_path') == 'outputs/analysis'
            and metadata.get('note_only') is True)


def primary_reused_references(result_document, *, work=None):
    if 'formal_runs' not in result_document:
        return {}
    run, attempt = (_declared_work(result_document) if work is None else work)[0]
    return {'variant_run_ref': run.get('variant_run_ref'),
            'evaluation_attempt_ref': attempt.get('evaluation_attempt_ref') if attempt is not None else None,
            'metric_result_ref': attempt.get('metric_result_ref') if attempt is not None else None}


def register_root_entities(connection, *, root, authority, manifest, completion, commit_ref,
                           work_items=None, verify_only=False, source_owner=None):
    """Persist or verify native entities from one immutable issuer-verified completion.

    Used by live acceptance and historical backfill. Never creates an AR phase,
    training run, synthetic checkpoint, or independent reviewer.
    """
    payload = json.loads(root['measurement_payload_json'])
    identities = {key: authority[key] for key in ('baseline_ref', 'variant_ref', 'evaluation_ref', 'protocol_version_ref')}
    result = json.loads(manifest['result_document_json'])
    from meta_research.formal_run_bindings import db_manifest_selection_entries
    entries = db_manifest_selection_entries(manifest)
    result_entries = [entry for entry in entries if entry['role'] == 'result']
    if len(result_entries) != 1:
        raise OwnerConflict('target_formal_result_asset_invalid')
    result_binding = result_entries[0]['binding']
    non_execution = explicit_unexecuted_root_evidence(result)
    if non_execution is not None:
        _ensure_execution_registration(connection, root=root, manifest=manifest, commit_ref=commit_ref,
                                       result_entry=result_entries[0], items=[],
                                       non_execution=non_execution, verify_only=verify_only)
        links = connection.execute(text('SELECT count(*) FROM rg_target_root_formal_entities '
                                        'WHERE measurement_ref=:ref'), {'ref': root['measurement_ref']}).scalar_one()
        if links:
            raise OwnerConflict('target_formal_unexecuted_work_has_entities')
        return []
    items = work_items or root_work_items(payload=payload, identities=identities, result_document=result)
    at = root['accepted_at']
    # The Target owns the actual method attribution of its work items: declared
    # method contracts resolve here, and evaluated items pair their real
    # Variant with the authority protocol when the Bundle draft differs.
    _resolve_declared_variants(connection, items=items, result_document=result,
                               root=root, accepted_at=at, verify_only=verify_only)
    verify_formal_variant_scope(connection, target_ref=root['target_ref'],
                                variant_refs=[item['variant_ref'] for item in items],
                                verify_only=verify_only)
    _derive_variant_evaluations(connection, items=items, identities=identities,
                                root=root, accepted_at=at, verify_only=verify_only)
    variant_proof = json.loads(root['variant_input_binding_json'])
    evaluation_proof = json.loads(root['evaluation_input_binding_json'])
    handle = json.loads(completion['handle_json'])
    # Selections in variant_proof may come from this result proposal. Only
    # frozen Owner sources and implementation bytes authorize new input use.
    allowed_inputs = set(handle.get('accepted_input_target_commit_refs', [])) | {
        manifest['implementation_revision_ref'],
    } | verified_target_input_asset_refs(
        source_owner, target_ref=root['target_ref'],
        proofs=handle.get('accepted_input_asset_proofs', []))
    if any(isinstance(item.get('input_refs'), list)
           and any(isinstance(ref, str) and ref not in allowed_inputs for ref in item['input_refs'])
           for item in items if not item['reuse_variant_run']):
        allowed_inputs |= verified_target_input_asset_refs(
            source_owner, target_ref=root['target_ref'], proofs=[],
            target_commit_refs=handle.get('accepted_input_target_commit_refs', ()))
    written_runs = set()
    inserted = {}
    checkpoint_entries = [entry for entry in entries if entry['role'] == 'checkpoint']
    run_checkpoint_cache = {}
    run_artifact_cache = set()
    # The immutable completion document names new production selections.
    # Historical replays use authenticated recorded productions, including
    # earlier accepted default selections and later attribution corrections.
    documented_items = items if work_items is None else root_work_items(
        payload=payload, identities=identities, result_document=result)
    checkpoint_defaults = _default_checkpoint_paths(
        {item['variant_run_ref']: item.get('checkpoint_paths') for item in documented_items
         if not item['reuse_variant_run']}, checkpoint_entries,
        require_explicit=not verify_only, verify_only=verify_only)
    artifact_defaults = (_recorded_subject_artifacts(connection, documented_items, entries)
                         if verify_only else {})
    verified_artifact_rows = {}

    def ensure(table, key, values, writer=None):
        if verify_only and table == 'rg_experiment_asset_roles':
            # Accepted readback checks the same exact fields, but one subject
            # read covers all retained states and outputs. Per-artifact SELECTs
            # multiply database I/O at every dependent Target boundary.
            subject = (values['subject_kind'], values['subject_ref'])
            if subject not in verified_artifact_rows:
                rows = connection.execute(text(
                    'SELECT * FROM rg_experiment_asset_roles '
                    'WHERE subject_kind=:kind AND subject_ref=:ref'),
                    {'kind': subject[0], 'ref': subject[1]}).mappings().all()
                verified_artifact_rows[subject] = {row['role_ref']: row for row in rows}
            existing = verified_artifact_rows[subject].get(values[key])
            if existing is None:
                # A role whose current attribution was later corrected no
                # longer sits under its original subject; read it by identity.
                by_ref = connection.execute(text(
                    'SELECT * FROM rg_experiment_asset_roles WHERE role_ref = :ref'),
                    {'ref': values[key]}).mappings().first()
                if by_ref is not None:
                    existing = by_ref
                    verified_artifact_rows[subject][values[key]] = by_ref
        else:
            existing = connection.execute(text(f'SELECT * FROM {table} WHERE {key} = :ref'),
                                          {'ref': values[key]}).mappings().first()
        if existing is not None:
            differing = {k for k, v in values.items() if existing[k] != v}
            if differing and differing <= {'subject_kind', 'subject_ref'} and table == 'rg_experiment_asset_roles':
                # A later Owner attribution correction moved this role's
                # current subject. The immutable identity, content version and
                # receipts must still match exactly.
                adjusted = connection.execute(text(
                    'SELECT 1 FROM rg_experiment_asset_role_adjustments WHERE role_ref = :ref LIMIT 1'),
                    {'ref': values['role_ref']}).first()
                if adjusted is None:
                    raise OwnerConflict('target_formal_entity_integrity_invalid')
            elif differing:
                raise OwnerConflict('target_formal_entity_integrity_invalid')
        elif verify_only:
            raise OwnerConflict('target_formal_entity_missing')
        elif writer:
            writer(connection, values)
            inserted[table] = inserted.get(table, 0) + 1
        else:
            _insert(connection, table, values)
            inserted[table] = inserted.get(table, 0) + 1

    for item in items:
        run_without_evaluation = item['evaluation_attempt_ref'] is None
        if run_without_evaluation:
            definition = connection.execute(text(
                'SELECT variant_ref,baseline_ref,recipe_json,recipe_hash FROM rg_experiment_variants '
                'WHERE variant_ref=:ref'), {'ref': item['variant_ref']}).mappings().first()
        else:
            definition = connection.execute(text(
                'SELECT e.*, p.required_metrics_json, p.required_metrics_hash, p.protocol_json, p.protocol_hash, '
                'v.baseline_ref,v.recipe_json,v.recipe_hash '
                'FROM rg_evaluations e JOIN rg_protocol_versions p USING (protocol_version_ref) '
                'JOIN rg_experiment_variants v USING (variant_ref) WHERE e.evaluation_ref=:ref'),
                {'ref': item['evaluation_ref']}).mappings().first()
        if definition is None or definition['variant_ref'] != item['variant_ref']:
            code = ('target_formal_variant_definition_invalid' if run_without_evaluation
                    else 'target_formal_evaluation_definition_invalid')
            raise target_content_issue(
                code,
                f"Run {item['run_key']!r}: variant_ref={item['variant_ref']!r} or evaluation_ref={item['evaluation_ref']!r} "
                "does not resolve to a matching accepted RG definition. Use a registered Variant/Evaluation ref for this Quest "
                "or omit overrides to inherit the authority binding.",
                verify_only=verify_only,
            )
        # The Variant must resolve to a registered Baseline, but it does not
        # have to be the Bundle draft's Baseline: the Target may reuse or
        # register any real method for its actual work.
        metrics = item['metrics']
        if canonical_hash(json.loads(definition['recipe_json'])) != definition['recipe_hash']:
            raise OwnerConflict('target_formal_definition_integrity_invalid')
        if not run_without_evaluation:
            from meta_research.target_execution_contract import valid_target_metric_value
            required = json.loads(definition['required_metrics_json'])
            protocol = json.loads(definition['protocol_json'])
            if (canonical_hash(required) != definition['required_metrics_hash'] or
                    canonical_hash(protocol) != definition['protocol_hash']):
                raise OwnerConflict('target_formal_definition_integrity_invalid')
            # Required metrics are names in both native and current authority schema.
            names = set(required) if isinstance(required, (list, dict)) else set()
            optional = protocol.get('optional_metrics', [])
            optional_names = {entry['metric_key'] for entry in optional if isinstance(entry, dict) and 'metric_key' in entry}
            if metrics is not None:
                metric_keys = set(metrics)
                invalid_values = [str(key) for key, value in metrics.items()
                                  if not isinstance(key, str) or not valid_target_metric_value(value)]
                if not names <= metric_keys <= names | optional_names or invalid_values:
                    conflict = TargetContentIssue('target_formal_metric_definition_invalid')
                    conflict.feedback = (
                        f"Run {item['run_key']!r}, Evaluation {item['attempt_key']!r} has "
                        f"missing required metric keys {sorted(names - metric_keys)!r}, "
                        f"unexpected metric keys {sorted(str(key) for key in metric_keys - names - optional_names)!r} "
                        f"and invalid JSON metric values at {sorted(invalid_values)!r} under "
                        f"ProtocolVersion {definition['protocol_version_ref']!r}. Correct only "
                        "that evaluation's metric declaration or add its genuinely missing "
                        "research evidence; preserve the other valid evaluations, artifacts "
                        "and actual work, which need no rerun for this declaration error. "
                        "Finish another normal turn in this Root."
                    )
                    raise conflict
            if metrics == {} and not item['reuse_evaluation_attempt']:
                report_paths = item.get('evaluation_artifact_paths')
                if report_paths is None:
                    report_paths = artifact_defaults.get(('evaluation_attempt', item['evaluation_attempt_ref']), [])
                _require_assessment_report(entries, report_paths)
        run_ref, attempt_ref = item['variant_run_ref'], item['evaluation_attempt_ref']
        vb = variant_proof['binding_ref'] if item['ordinal'] == 0 else _ref('variant_input', run_ref)
        eb = None if attempt_ref is None else (evaluation_proof['binding_ref'] if item['ordinal'] == 0 else _ref('evaluation_input', attempt_ref))
        from meta_research.formal_run_bindings import implementation_binding, resolve_local_inputs
        local_inputs, selected_local = resolve_local_inputs(result, entries, item)
        input_selection_recorded = item['input_refs'] is not None
        legacy_input_selection = False
        if verify_only and not item['reuse_variant_run']:
            recorded = connection.execute(text(
                'SELECT b.inputs_json FROM rg_variant_runs r JOIN rg_experiment_input_bindings b '
                'ON b.binding_ref=r.input_binding_ref WHERE r.variant_run_ref=:ref'),
                {'ref': run_ref}).scalar_one_or_none()
            # Old frozen bindings keep their original receipt and selections.
            # Only new admissions stop inferring use from available sources.
            legacy_input_selection = (recorded is not None
                                      and 'input_selection_recorded' not in json.loads(recorded))
        input_refs = (item['input_refs'] if item['input_refs'] is not None else
                      (variant_proof['input_refs'] if legacy_input_selection else []))
        if (not isinstance(input_refs, list) or any(not isinstance(ref, str) for ref in input_refs)
                or len(set(input_refs)) != len(input_refs)
                or not item['reuse_variant_run'] and not set(input_refs) <= allowed_inputs):
            raise TargetContentIssue('target_formal_input_reference_invalid')
        input_refs = list(dict.fromkeys([*input_refs, *local_inputs]))
        if item['reuse_variant_run']:
            if item.get('implementation_paths') is not None or local_inputs:
                raise target_content_issue(
                    'target_formal_reused_run_input_mismatch',
                    f"Run {item['run_key']!r} reuses variant_run_ref={run_ref!r} and must omit "
                    "implementation_paths and local_inputs because a reused Run keeps its original frozen inputs.",
                    verify_only=verify_only,
                )
            implementation_revision_ref = implementation_tree_hash = None
        else:
            implementation_revision_ref, implementation_tree_hash = implementation_binding(
                entries, item.get('implementation_paths'), item.get('implementation_revision_ref'),
                declared_runs=result.get('formal_runs', ()), run_key=item['run_key'])
        inputs = {
            'schema_ref': 'meta-research/formal-execution-inputs/v1',
            'input_refs': input_refs, 'target_ref': root['target_ref'],
            'target_run_ref': root['target_run_ref'], 'completion_ref': root['completion_ref'],
            'manifest_ref': root['manifest_ref'], 'manifest_payload_hash': root['manifest_payload_hash'],
            'implementation_revision_ref': implementation_revision_ref,
            'implementation_tree_hash': implementation_tree_hash,
            'accepted_input_asset_proofs': handle.get('accepted_input_asset_proofs', []),
            'accepted_input_target_commit_refs': handle.get('accepted_input_target_commit_refs', []),
            'run_key': item['run_key'],
        }
        inputs['produced_selections'] = produced_selections(entries,
            [*(item.get('artifact_paths') or []), *(item.get('checkpoint_paths') or [])])
        if selected_local:
            inputs['local_input_selections'] = selected_local
        if not legacy_input_selection:
            inputs['input_selection_recorded'] = input_selection_recorded
        if item['reuse_variant_run']:
            old = connection.execute(text('SELECT variant_ref,status,input_binding_ref FROM rg_variant_runs WHERE variant_run_ref=:ref'), {'ref': run_ref}).mappings().first()
            if old is None or old['variant_ref'] != item['variant_ref'] or old['status'] != item['run_status']:
                raise target_content_issue(
                    'target_formal_reused_run_invalid',
                    f"Run {item['run_key']!r} reuses variant_run_ref={run_ref!r}, which does not exist or "
                    f"differs in variant_ref ({item['variant_ref']!r}) or status ({item['run_status']!r}).",
                    verify_only=verify_only,
                )
            vb = old['input_binding_ref']
            from types import SimpleNamespace
            from meta_research.owners.research_graph import _accepted_experiment_input_binding
            old_input = connection.execute(text('SELECT * FROM rg_experiment_input_bindings WHERE binding_ref=:ref'),
                                           {'ref': vb}).mappings().first()
            if old_input is None:
                raise OwnerConflict('target_formal_reused_run_invalid')
            accepted_input = _accepted_experiment_input_binding(SimpleNamespace(**old_input))
            if accepted_input.subject_kind != 'variant_run' or accepted_input.subject_ref != run_ref:
                raise OwnerConflict('target_formal_reused_run_invalid')
            if item['input_refs'] is not None and item['input_refs'] != accepted_input.inputs.get('input_refs'):
                raise target_content_issue(
                    'target_formal_reused_run_input_mismatch',
                    f"Run {item['run_key']!r} reuses variant_run_ref={run_ref!r} with input_refs={item['input_refs']!r}, "
                    f"which mismatches the frozen inputs {accepted_input.inputs.get('input_refs')!r}. Omit input_refs or match them.",
                    verify_only=verify_only,
                )
            if (item.get('implementation_revision_ref') is not None
                    and item['implementation_revision_ref'] != accepted_input.inputs.get('implementation_revision_ref')):
                raise target_content_issue(
                    'target_formal_implementation_revision_mismatch',
                    f"Run {item['run_key']!r} reuses variant_run_ref={run_ref!r} with implementation_revision_ref="
                    f"{item.get('implementation_revision_ref')!r}, which differs from frozen revision "
                    f"{accepted_input.inputs.get('implementation_revision_ref')!r}. Omit implementation_revision_ref or match it.",
                    verify_only=verify_only,
                )
            if source_owner is not None:
                from meta_research.run_only_registration import _verify_original_binding_source
                _verify_original_binding_source(source_owner, accepted_input, run_ref)
                source_target = accepted_input.inputs.get('target_ref')
                if source_target and source_target != root['target_ref']:
                    source_owner.query_target_root_commit_transition(source_target)
        elif run_ref not in written_runs:
            _ensure_input(ensure, vb, 'variant_run', run_ref, inputs, at)
            ensure('rg_variant_runs', 'variant_run_ref', {
                'variant_run_ref': run_ref, 'variant_ref': item['variant_ref'], 'input_binding_ref': vb,
                'implementation_revision_ref': implementation_revision_ref,
                'status': item['run_status'], 'created_at': at, 'updated_at': at}, insert_variant_run)
        written_runs.add(run_ref)
        run_paths = item.get('artifact_paths')
        if run_paths is None and not item['reuse_variant_run']:
            run_paths = artifact_defaults.get(('variant_run', run_ref), [])
        artifact_key = (run_ref, None if run_paths is None else tuple(run_paths))
        if artifact_key not in run_artifact_cache:
            _register_subject_artifacts(connection, ensure=ensure, subject_kind='variant_run',
                subject_ref=run_ref, paths=run_paths, entries=entries,
                accepted_at=at, reused=item['reuse_variant_run'], verify_only=verify_only)
            run_artifact_cache.add(artifact_key)
        if item['reuse_evaluation_attempt']:
            previous = connection.execute(text(
                'SELECT a.evaluation_ref,a.variant_run_ref,a.status,a.input_binding_ref, '
                'a.checkpoint_role_refs_json,a.checkpoint_role_refs_hash,m.* '
                'FROM rg_evaluation_attempts a JOIN rg_metric_results m USING (evaluation_attempt_ref) '
                'WHERE a.evaluation_attempt_ref=:ref'), {'ref': attempt_ref}).mappings().first()
            if (previous is None or previous['evaluation_ref'] != item['evaluation_ref']
                    or previous['variant_run_ref'] != run_ref or previous['status'] != 'measurement_accepted'
                    or previous['metric_result_ref'] != item['metric_result_ref']
                    or previous['metrics_json'] != canonical_json(metrics)
                    or previous['metrics_hash'] != canonical_hash(metrics)):
                raise target_content_issue(
                    'target_formal_reused_evaluation_invalid',
                    f"Run {item['run_key']!r}, Evaluation {item['attempt_key']!r} reused evaluation_attempt_ref="
                    f"{attempt_ref!r} / metric_result_ref={item['metric_result_ref']!r} does not match accepted RG facts "
                    "(variant_run_ref, evaluation_ref, metrics, or measurement_accepted status).",
                    verify_only=verify_only,
                )
            from types import SimpleNamespace
            from meta_research.owners.research_graph import _accepted_experiment_input_binding
            old_binding = connection.execute(text('SELECT * FROM rg_experiment_input_bindings WHERE binding_ref=:ref'),
                                             {'ref': previous['input_binding_ref']}).mappings().first()
            if old_binding is None:
                raise OwnerConflict('target_formal_reused_evaluation_invalid')
            accepted_binding = _accepted_experiment_input_binding(SimpleNamespace(**old_binding))
            if accepted_binding.subject_kind != 'evaluation_attempt' or accepted_binding.subject_ref != attempt_ref:
                raise OwnerConflict('target_formal_reused_evaluation_invalid')
            _verify_reused_evaluation_source(connection, attempt_ref=attempt_ref,
                metric_ref=item['metric_result_ref'], binding=accepted_binding, source_owner=source_owner)
            _register_subject_artifacts(connection, ensure=ensure, subject_kind='evaluation_attempt',
                subject_ref=attempt_ref, paths=item.get('evaluation_artifact_paths'), entries=entries,
                accepted_at=at, reused=True, verify_only=verify_only)
            old_checkpoints = json.loads(previous['checkpoint_role_refs_json'])
            if canonical_hash(old_checkpoints) != previous['checkpoint_role_refs_hash']:
                raise OwnerConflict('target_formal_reused_evaluation_invalid')
            item['frozen_checkpoint_role_refs'] = accepted_binding.inputs['checkpoint_scope_role_refs']
            if any(item.get(key) is not None for key in ('checkpoint_paths', 'checkpoint_role_refs',
                    'checkpoint_version_refs', 'evaluation_checkpoint_paths',
                    'evaluation_checkpoint_role_refs', 'evaluation_checkpoint_version_refs')):
                available = _register_run_checkpoints(connection, ensure=ensure, item=item,
                    entries=checkpoint_entries, accepted_at=at, default_paths=checkpoint_defaults,
                    verify_only=verify_only)
                selected = _select_checkpoint_records(available, item.get('evaluation_checkpoint_paths'),
                    item.get('evaluation_checkpoint_role_refs'), item.get('evaluation_checkpoint_version_refs'),
                    verify_only=verify_only)
                if [record['role_ref'] for record in selected] != old_checkpoints:
                    raise target_content_issue(
                        'target_formal_reused_evaluation_checkpoint_mismatch',
                        f"Run {item['run_key']!r}, Evaluation {item['attempt_key']!r} checkpoint selection does not "
                        f"match reused EvaluationAttempt frozen checkpoint roles {old_checkpoints!r}. Omit checkpoint overrides or match them.",
                        verify_only=verify_only,
                    )
            _ensure_link(ensure, root, item, commit_ref)
            continue
        if verify_only and item['reuse_variant_run'] and attempt_ref is not None:
            prior = connection.execute(text('SELECT b.inputs_json FROM rg_evaluation_attempts a '
                'JOIN rg_experiment_input_bindings b ON b.binding_ref=a.input_binding_ref '
                'WHERE a.evaluation_attempt_ref=:ref'), {'ref': attempt_ref}).first()
            if prior is None:
                raise OwnerConflict('target_formal_entity_missing')
            # An assessment can consume a subset of the Run state it inspected.
            # Freeze both selections so later attribution corrections do not
            # change either historical Run scope or actual assessment inputs.
            item['frozen_checkpoint_role_refs'] = json.loads(prior.inputs_json)['checkpoint_scope_role_refs']
        checkpoint_cache_key = (run_ref, canonical_json({key: item.get(key) for key in
            ('checkpoint_paths', 'checkpoint_role_refs', 'checkpoint_version_refs', 'frozen_checkpoint_role_refs')}))
        if checkpoint_cache_key not in run_checkpoint_cache:
            run_checkpoint_cache[checkpoint_cache_key] = _register_run_checkpoints(
                connection, ensure=ensure, item=item, entries=checkpoint_entries, accepted_at=at,
                default_paths=checkpoint_defaults, verify_only=verify_only)
        run_checkpoints = run_checkpoint_cache[checkpoint_cache_key]
        if run_without_evaluation:
            _ensure_link(ensure, root, item, commit_ref)
            continue
        selected_checkpoints = _select_checkpoint_records(run_checkpoints, item.get('evaluation_checkpoint_paths'),
            item.get('evaluation_checkpoint_role_refs'), item.get('evaluation_checkpoint_version_refs'),
            verify_only=verify_only)
        checkpoint_roles = [entry['role_ref'] for entry in selected_checkpoints]
        evaluation_inputs = {'schema_ref': 'meta-research/formal-evaluation-inputs/v1',
                             'input_refs': [run_ref, definition['protocol_version_ref']],
                             'completion_ref': root['completion_ref'], 'manifest_ref': root['manifest_ref'],
                             'attempt_key': item['attempt_key'], 'result_asset': result_binding,
                             'checkpoint_refs': [entry['version_ref'] for entry in selected_checkpoints],
                             'checkpoint_scope_role_refs': [entry['role_ref'] for entry in run_checkpoints]}
        evaluation_inputs['produced_selections'] = produced_selections(entries, item.get('evaluation_artifact_paths'))
        _ensure_input(ensure, eb, 'evaluation_attempt', attempt_ref, evaluation_inputs, at)
        checkpoints = checkpoint_roles
        ensure('rg_evaluation_attempts', 'evaluation_attempt_ref', {
            'evaluation_attempt_ref': attempt_ref, 'evaluation_ref': item['evaluation_ref'],
            'variant_run_ref': run_ref, 'input_binding_ref': eb,
            'checkpoint_role_refs_json': canonical_json(checkpoints),
            'checkpoint_role_refs_hash': canonical_hash(checkpoints),
            'status': 'measurement_accepted' if metrics is not None else 'failed',
            'formal_rejection_code': None, 'created_at': at, 'updated_at': at}, insert_evaluation_attempt)
        _register_subject_artifacts(connection, ensure=ensure, subject_kind='evaluation_attempt',
            subject_ref=attempt_ref, paths=(item.get('evaluation_artifact_paths') if item.get('evaluation_artifact_paths') is not None
                else artifact_defaults.get(('evaluation_attempt', attempt_ref), [])), entries=entries,
            accepted_at=at, verify_only=verify_only)
        verified_checkpoints = None
        if verify_only:
            verified_checkpoints = dict(connection.execute(text(
                'SELECT ordinal,checkpoint_role_ref FROM rg_evaluation_attempt_checkpoints '
                'WHERE evaluation_attempt_ref=:ref'), {'ref': attempt_ref}).all())
        for checkpoint_index, checkpoint_role in enumerate(checkpoint_roles):
            if verified_checkpoints is not None:
                old_role_ref = verified_checkpoints.get(checkpoint_index)
            else:
                old_role_ref = connection.execute(text(
                    'SELECT checkpoint_role_ref FROM rg_evaluation_attempt_checkpoints '
                    'WHERE evaluation_attempt_ref=:ref AND ordinal=:ordinal'),
                    {'ref': attempt_ref, 'ordinal': checkpoint_index}).scalar_one_or_none()
            if old_role_ref is None:
                if verify_only:
                    raise OwnerConflict('target_formal_entity_missing')
                _insert(connection, 'rg_evaluation_attempt_checkpoints', {
                    'evaluation_attempt_ref': attempt_ref, 'ordinal': checkpoint_index,
                    'checkpoint_role_ref': checkpoint_role})
            elif old_role_ref != checkpoint_role:
                raise OwnerConflict('target_formal_entity_integrity_invalid')
        if metrics is None:
            _ensure_link(ensure, root, item, commit_ref)
            continue
        role_ref = _ref('formal_result_role', attempt_ref)
        role_receipt_ref = _ref('formal_result_role_receipt', attempt_ref)
        role_hash = _receipt('experiment_asset_role_acceptance', role_ref, {
            'subject_kind': 'evaluation_attempt', 'subject_ref': attempt_ref,
            'role': 'result_content', 'ordinal': 0, 'asset': result_binding})
        ensure('rg_experiment_asset_roles', 'role_ref', {
            'role_ref': role_ref, 'subject_kind': 'evaluation_attempt', 'subject_ref': attempt_ref,
            'role': 'result_content', 'ordinal': 0, 'asset_ref': result_binding['asset_ref'],
            'version_ref': result_binding['version_ref'], 'content_hash': result_binding['content_hash'],
            'manifest_hash': result_binding['manifest_hash'],
            'asset_receipt_ref': result_binding['receipt']['receipt_ref'],
            'asset_receipt_hash': result_binding['receipt']['payload_hash'],
            'receipt_ref': role_receipt_ref, 'receipt_hash': role_hash, 'accepted_at': at})
        metric_ref = item['metric_result_ref']
        receipt_ref = root['receipt_ref'] if item['ordinal'] == 0 else _ref('formal_metric_receipt', attempt_ref)
        receipt_hash = root['receipt_hash'] if item['ordinal'] == 0 else canonical_hash({
            'completion_ref': root['completion_ref'], 'evaluation_attempt_ref': attempt_ref,
            'metric_result_ref': metric_ref, 'metrics': metrics, 'result_asset': result_binding})
        ensure('rg_metric_results', 'metric_result_ref', {
            'metric_result_ref': metric_ref, 'evaluation_attempt_ref': attempt_ref, 'result_role_ref': role_ref,
            'metrics_json': canonical_json(metrics), 'metrics_hash': canonical_hash(metrics),
            'required_metrics_hash': definition['required_metrics_hash'], 'run_ref': root['target_run_ref'],
            'execution_attempt_ref': handle['execution_attempt_ref'], 'fence_ref': handle['execution_fence_ref'],
            'execution_result_hash': root['completion_payload_hash'],
            'execution_receipt_ref': root['completion_receipt_ref'],
            'execution_receipt_hash': root['completion_receipt_hash'],
            'receipt_ref': receipt_ref, 'receipt_hash': receipt_hash, 'accepted_at': at}, insert_metric_result)
        _ensure_link(ensure, root, item, commit_ref)
    links = connection.execute(text('SELECT count(*) FROM rg_target_root_formal_entities WHERE measurement_ref=:ref'),
                               {'ref': root['measurement_ref']}).scalar_one()
    if links != len(items):
        raise OwnerConflict('target_formal_entity_integrity_invalid')
    _ensure_execution_registration(connection, root=root, manifest=manifest, commit_ref=commit_ref,
                                   result_entry=result_entries[0], items=items,
                                   non_execution=None, verify_only=verify_only)
    for table, counter in (
        ('rg_variant_runs', 'variant_run_count'),
        ('rg_evaluation_attempts', 'evaluation_attempt_count'),
        ('rg_metric_results', 'formal_measurement_count'),
        ('rg_experiment_input_bindings', 'experiment_input_binding_count'),
        ('rg_experiment_asset_roles', 'experiment_asset_role_count'),
    ):
        if inserted.get(table):
            connection.execute(text(f"UPDATE research_graph_state SET {counter}={counter}+:count WHERE singleton='owner'"), {'count': inserted[table]})
    return items


def _verify_reused_evaluation_source(connection, *, attempt_ref, metric_ref, binding, source_owner):
    """Verify the original issuer, never a later completion that merely reuses it."""
    active = _REUSED_EVALUATION_SOURCES.get()
    if attempt_ref in active:
        raise OwnerConflict('target_formal_reused_evaluation_source_cycle')
    token = _REUSED_EVALUATION_SOURCES.set(active | {attempt_ref})
    try:
        if binding.inputs.get('schema_ref') != 'meta-research/formal-evaluation-inputs/v1':
            # Native execution-port results retain their established AR/RM/RG
            # verification semantics; root closure shapes do not apply to them.
            if source_owner is None:
                raise OwnerConflict('target_formal_reused_source_verifier_unavailable')
            accepted = source_owner.query_target_formal_metric_result(attempt_ref)
            if accepted is None or accepted.metric_result_ref != metric_ref:
                raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
            return
        source = connection.execute(text('SELECT * FROM rg_target_root_measurements WHERE '
            'completion_ref=:completion AND manifest_ref=:manifest'), {
            'completion': binding.inputs.get('completion_ref'), 'manifest': binding.inputs.get('manifest_ref')}).mappings().first()
        if source is None:
            raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
        def row(table, key, ref):
            value = connection.execute(text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': ref}).mappings().first()
            if value is None:
                raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
            return value
        manifest = row('rm_target_root_completion_manifests', 'manifest_ref', source['manifest_ref'])
        completion = row('ar_target_root_completions', 'completion_ref', source['completion_ref'])
        authority = row('rg_target_measurement_domain_authorities', 'authority_ref', source['authority_ref'])
        commit = row('rg_target_commits', 'target_ref', source['target_ref'])
        if source_owner is not None:
            transition = source_owner._query_target_root_commit_transition(source['target_ref'])
            if transition is None or transition.target_commit_ref != commit['commit_ref']:
                raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
        else:
            # Migration has no running Owner service. Rebuild exact immutable
            # AR/RM source receipts before the same native-entity verifier.
            _verify_reused_root_source_rows(source, authority, manifest, completion, commit)
        items = root_work_items(payload=json.loads(source['measurement_payload_json']),
            identities=authority, result_document=json.loads(manifest['result_document_json']))
        originals = [item for item in items if item['evaluation_attempt_ref'] == attempt_ref
                     and item['metric_result_ref'] == metric_ref and not item['reuse_evaluation_attempt']]
        if len(originals) != 1:
            raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
        if source_owner is None:
            register_root_entities(connection, root=source, authority=authority, manifest=manifest,
                completion=completion, commit_ref=commit['commit_ref'], work_items=items, verify_only=True)
    finally:
        _REUSED_EVALUATION_SOURCES.reset(token)


def _verify_reused_root_source_rows(root, authority, manifest, completion, commit):
    from types import SimpleNamespace
    from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
    from meta_research.target_run_finalizer import _receipt as manifest_receipt, RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND
    accepted = SQLiteTargetRootLifecycleAuthority._completion_from_row(SimpleNamespace(**completion))
    payload = {
        'completion_ref': accepted.completion_ref, 'target_ref': accepted.handle.target_ref,
        'target_run_ref': accepted.handle.target_run_ref, 'workspace_ref': accepted.workspace_ref,
        'implementation_revision_ref': accepted.implementation_revision_ref,
        'implementation_tree_hash': accepted.implementation_tree_hash,
        'result_document_path': accepted.handoff.result_document_path,
        'result_document': json.loads(manifest['result_document_json']),
        'result_document_hash': accepted.result_document_hash, 'artifact_snapshot_hash': accepted.artifact_snapshot_hash,
        'entries': json.loads(manifest['entries_json']), 'completion_receipt': accepted.receipt.as_public_dict(),
    }
    recorded_payload = json.loads(manifest['payload_json'])
    from meta_research.formal_run_bindings import SELECTION_MANIFEST_SCHEMA
    if recorded_payload.get('schema_ref') != SELECTION_MANIFEST_SCHEMA:
        raise OwnerConflict('target_root_manifest_protocol_unsupported')
    payload.update(schema_ref=SELECTION_MANIFEST_SCHEMA, selections=recorded_payload['selections'])
    digest = canonical_hash(payload)
    receipt = manifest_receipt('research_memory', RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND,
        manifest['receipt_ref'], manifest['manifest_ref'],
        {'manifest_ref': manifest['manifest_ref'], 'payload_hash': digest, **payload})
    for value, json_key, hash_key in (
        (root, 'measurement_payload_json', 'measurement_payload_hash'),
        (root, 'metrics_json', 'metrics_hash'), (commit, 'closure_json', 'closure_hash'),
        (manifest, 'entries_json', 'entries_hash'), (manifest, 'result_document_json', 'result_document_hash')):
        if canonical_hash(json.loads(value[json_key])) != value[hash_key]:
            raise OwnerConflict('target_formal_reused_evaluation_source_invalid')
    if (manifest['payload_json'] != canonical_json(payload) or manifest['payload_hash'] != digest
            or manifest['receipt_hash'] != receipt.payload_hash
            or manifest['completion_receipt_hash'] != accepted.receipt.payload_hash
            or root['authority_hash'] != authority['authority_hash']
            or root['completion_payload_hash'] != completion['payload_hash']
            or root['manifest_payload_hash'] != manifest['payload_hash']):
        raise OwnerConflict('target_formal_reused_evaluation_source_invalid')


def _select_checkpoint_records(records, paths, role_refs=None, version_refs=None, *, verify_only=False):
    selected = records
    for key, values in (('role_ref', role_refs), ('version_ref', version_refs)):
        if values is None:
            continue
        _checkpoint_paths(values, verify_only=verify_only)
        if not set(values) <= {record.get(key) for record in selected}:
            raise target_content_issue(
                'target_formal_checkpoint_reference_not_bound',
                f'Selected checkpoint {key}s {values!r} include references not bound to this Run.',
                verify_only=verify_only,
            )
        selected = [record for record in selected if record.get(key) in values]
    if paths is not None:
        available = {record['declared_relative_path'] for record in selected}
        if not set(paths) <= available:
            missing = [path for path in paths if path not in available]
            raise target_content_issue(
                'target_formal_checkpoint_path_not_bound',
                f'Declared checkpoint_paths {missing!r} are not retained with role checkpoint for this Run.',
                verify_only=verify_only,
            )
        if any(sum(record['declared_relative_path'] == path for record in selected) > 1 for path in paths):
            raise target_content_issue(
                'target_formal_checkpoint_path_ambiguous',
                f'Declared checkpoint_paths {paths!r} match multiple retained checkpoint records unambiguously; '
                'select by exact role_ref or version_ref instead.',
                verify_only=verify_only,
            )
        selected = [record for record in selected if record['declared_relative_path'] in paths]
    return selected


def _existing_checkpoint_records(connection, run_ref, frozen_refs=None):
    """Current attribution for new use; frozen stable roles for accepted history.

    A moved-in state's path belongs to its original producer manifest, never
    to the receiving Run's manifest. Re-authenticate every role receipt first.
    """
    from types import SimpleNamespace
    if frozen_refs is None:
        rows = connection.execute(text("SELECT * FROM rg_experiment_asset_roles WHERE "
            "subject_kind='variant_run' AND subject_ref=:ref AND role='checkpoint_artifact' "
            "ORDER BY role,ordinal,accepted_at,role_ref"), {'ref': run_ref}).mappings().all()
    else:
        rows = []
        for role_ref in frozen_refs:
            row = connection.execute(text('SELECT * FROM rg_experiment_asset_roles WHERE role_ref=:ref'),
                                     {'ref': role_ref}).mappings().first()
            if row is None or row['role'] != 'checkpoint_artifact':
                raise OwnerConflict('target_formal_checkpoint_reference_not_bound')
            rows.append(row)
    records = []
    for row in rows:
        _accepted_asset_role_for_replay(connection, SimpleNamespace(**row))
        first = connection.execute(text('SELECT from_subject_ref FROM rg_experiment_asset_role_adjustments '
            'WHERE role_ref=:ref ORDER BY accepted_at,rowid LIMIT 1'), {'ref': row['role_ref']}).first()
        producer_ref = first.from_subject_ref if first else row['subject_ref']
        source = connection.execute(text('SELECT b.inputs_json FROM rg_variant_runs r JOIN '
            'rg_experiment_input_bindings b ON b.binding_ref=r.input_binding_ref WHERE r.variant_run_ref=:ref'),
            {'ref': producer_ref}).first()
        manifest_ref = json.loads(source.inputs_json).get('manifest_ref') if source else None
        manifest = connection.execute(text('SELECT entries_json,payload_json FROM rm_target_root_completion_manifests '
            'WHERE manifest_ref=:ref'), {'ref': manifest_ref}).first() if manifest_ref else None
        from meta_research.formal_run_bindings import db_manifest_selection_entries
        source_entries = db_manifest_selection_entries(manifest._mapping) if manifest else []
        matches = [entry for entry in source_entries if entry['role'] == 'checkpoint'
                   and _ref('formal_checkpoint_selection_role', producer_ref,
                       entry['binding']['version_ref'], entry['declared_relative_path']) == row['role_ref']]
        if len(matches) != 1:
            raise OwnerConflict('target_formal_checkpoint_source_invalid')
        records.append({'role_ref': row['role_ref'], 'version_ref': row['version_ref'],
                        'declared_relative_path': matches[0]['declared_relative_path']})
    return records


def _default_checkpoint_paths(new_runs, entries, *, require_explicit=False, verify_only=False):
    """New work declares production; historical replay can verify old defaults.

    Multiple producers declare actual ownership; once every retained state is
    assigned, an omitted selection means no checkpoints for that Run.
    """
    if len(new_runs) <= 1 and not require_explicit:
        return None
    assigned = {path for paths in new_runs.values() for path in paths or []}
    if any(entry['declared_relative_path'] not in assigned for entry in entries):
        unassigned = [entry['declared_relative_path'] for entry in entries if entry['declared_relative_path'] not in assigned]
        raise target_content_issue(
            'target_formal_checkpoint_assignment_required',
            f'Retained checkpoint paths {unassigned!r} are not assigned in $.formal_runs[*].checkpoint_paths. '
            'Assign each retained checkpoint to the producing Run or declare checkpoint_paths: [] when none belong to a Run.',
            verify_only=verify_only,
        )
    return []


def _register_run_checkpoints(connection, *, ensure, item, entries, accepted_at, default_paths=None, verify_only=False):
    run_ref = item['variant_run_ref']
    if item['reuse_variant_run']:
        records = _existing_checkpoint_records(connection, run_ref, item.get('frozen_checkpoint_role_refs'))
        return _select_checkpoint_records(records, item.get('checkpoint_paths'),
            item.get('checkpoint_role_refs'), item.get('checkpoint_version_refs'),
            verify_only=verify_only)
    paths = item.get('checkpoint_paths')
    selected = _select_checkpoint_records(entries, default_paths if paths is None else paths, verify_only=verify_only)
    records = []
    for ordinal, entry in enumerate(selected):
        binding = entry['binding']
        role_ref = _ref('formal_checkpoint_selection_role', run_ref, binding['version_ref'],
                        entry['declared_relative_path'])
        ensure('rg_experiment_asset_roles', 'role_ref', {
            'role_ref': role_ref, 'subject_kind': 'variant_run', 'subject_ref': run_ref,
            'role': 'checkpoint_artifact', 'ordinal': ordinal,
            'asset_ref': binding['asset_ref'], 'version_ref': binding['version_ref'],
            'content_hash': binding['content_hash'], 'manifest_hash': binding['manifest_hash'],
            'asset_receipt_ref': binding['receipt']['receipt_ref'],
            'asset_receipt_hash': binding['receipt']['payload_hash'],
            'receipt_ref': _ref('formal_checkpoint_receipt', role_ref),
            'receipt_hash': _receipt('experiment_asset_role_acceptance', role_ref, {
                'subject_kind': 'variant_run', 'subject_ref': run_ref, 'role': 'checkpoint_artifact',
                'ordinal': ordinal, 'asset': binding}), 'accepted_at': accepted_at})
        records.append({'role_ref': role_ref, 'version_ref': binding['version_ref'],
                        'declared_relative_path': entry['declared_relative_path']})
    return records


def _recorded_subject_artifacts(connection, items, entries):
    """Read accepted production selections when replaying historical work."""
    by_version = {}
    for entry in entries:
        if entry['role'] in {'log', 'analysis', 'data'}:
            by_version.setdefault(entry['binding']['version_ref'], []).append(entry['declared_relative_path'])
    if not by_version:
        return {}
    subjects = {(kind, item[key]) for item in items
                for kind, key in (('variant_run', 'variant_run_ref'),
                                  ('evaluation_attempt', 'evaluation_attempt_ref'))
                if item[key] is not None}
    parameters = {f'version_{index}': version for index, version in enumerate(by_version)}
    placeholders = ','.join(':' + key for key in parameters)
    rows = connection.execute(text(
        'SELECT r.*, (SELECT a.from_subject_kind FROM rg_experiment_asset_role_adjustments a '
        'WHERE a.role_ref=r.role_ref ORDER BY a.accepted_at,a.rowid LIMIT 1) AS _adjusted_from_kind, '
        '(SELECT a.from_subject_ref FROM rg_experiment_asset_role_adjustments a '
        'WHERE a.role_ref=r.role_ref ORDER BY a.accepted_at,a.rowid LIMIT 1) AS _adjusted_from_ref '
        'FROM rg_experiment_asset_roles r WHERE r.version_ref IN (' + placeholders + ') '
        "AND r.role IN ('log_asset','analysis_asset','data_asset') "
        'ORDER BY r.role,r.ordinal,r.accepted_at,r.role_ref'), parameters).all()
    defaults = {}
    for row in rows:
        try:
            _accepted_asset_role_for_replay(connection, row)
        except OwnerConflict as error:
            raise OwnerConflict('target_formal_entity_integrity_invalid') from error
        subject = (row._adjusted_from_kind or row.subject_kind,
                   row._adjusted_from_ref or row.subject_ref)
        if subject in subjects:
            defaults.setdefault(subject, []).extend(by_version[row.version_ref])
    return defaults


def _accepted_asset_role_for_replay(connection, row):
    """Role verification that tolerates later Owner attribution corrections."""
    from meta_research.owners.research_graph import _accepted_experiment_asset_role_tolerant
    return _accepted_experiment_asset_role_tolerant(connection, row)



def produced_selections(entries, paths):
    selected = set(paths or [])
    return [{'artifact_path': entry['declared_relative_path'], 'role': entry['role'],
             'asset_binding': entry['binding'], 'path': entry.get('path')}
            for entry in entries if entry['declared_relative_path'] in selected]


def _artifact_role_ref(kind, subject, entry):
    return _ref('formal_artifact_parent_role', kind, subject, entry['role'], entry['binding']['version_ref'])

def _register_subject_artifacts(connection, *, ensure, subject_kind, subject_ref,
                                paths, entries, accepted_at, reused=False, verify_only=False):
    """Assign exact RM versions to the execution or assessment that produced them.

    Unassigned artifacts stay in the completion manifest. Reusing a subject
    never makes the current Target's files into that subject's old outputs.
    """
    from types import SimpleNamespace

    if reused:
        rows = connection.execute(text("SELECT r.*, (SELECT a.from_subject_kind FROM "
            "rg_experiment_asset_role_adjustments a WHERE a.role_ref=r.role_ref "
            "ORDER BY a.accepted_at, a.rowid LIMIT 1) AS _adjusted_from_kind, "
            "(SELECT a.from_subject_ref FROM rg_experiment_asset_role_adjustments a "
            "WHERE a.role_ref=r.role_ref ORDER BY a.accepted_at, a.rowid LIMIT 1) "
            "AS _adjusted_from_ref FROM rg_experiment_asset_roles r WHERE r.subject_kind=:kind "
            "AND r.subject_ref=:ref AND r.role IN ('log_asset','analysis_asset','data_asset') ORDER BY r.role,r.ordinal,r.accepted_at,r.role_ref"),
            {'kind': subject_kind, 'ref': subject_ref}).mappings().all()
        for row in rows:
            _accepted_asset_role_for_replay(connection, SimpleNamespace(**row))
        if paths is not None:
            table, key = ('rg_variant_runs', 'variant_run_ref') if subject_kind == 'variant_run' else (
                'rg_evaluation_attempts', 'evaluation_attempt_ref')
            source = connection.execute(text(f'SELECT b.inputs_json FROM {table} s JOIN rg_experiment_input_bindings b '
                f'ON b.binding_ref=s.input_binding_ref WHERE s.{key}=:ref'), {'ref': subject_ref}).first()
            manifest_ref = json.loads(source.inputs_json).get('manifest_ref') if source else None
            manifest = connection.execute(text('SELECT entries_json,payload_json FROM rm_target_root_completion_manifests '
                'WHERE manifest_ref=:ref'), {'ref': manifest_ref}).first()
            from meta_research.formal_run_bindings import db_manifest_selection_entries
            original_entries = db_manifest_selection_entries(manifest._mapping) if manifest else []
            selected = _selected_subject_artifacts(original_entries, paths, verify_only=verify_only)
            # Current roles under this subject minus later corrections in both
            # directions: roles moved away and roles moved in by an Owner
            # adjustment no longer witness this original manifest selection.
            moved_in = {row2['role_ref'] for row2 in connection.execute(text(
                'SELECT role_ref FROM rg_experiment_asset_role_adjustments '
                'WHERE to_subject_kind = :kind AND to_subject_ref = :ref'),
                {'kind': subject_kind, 'ref': subject_ref}).mappings()}
            moved_away = {row2['role_ref'] for row2 in connection.execute(text(
                'SELECT role_ref FROM rg_experiment_asset_role_adjustments '
                'WHERE from_subject_kind = :kind AND from_subject_ref = :ref'),
                {'kind': subject_kind, 'ref': subject_ref}).mappings()}
            excluded_refs = moved_in | moved_away
            remaining = {row['version_ref'] for row in rows
                         if row['role_ref'] not in moved_in}
            selected_versions = {
                entry['binding']['version_ref'] for entry in selected
                if _artifact_role_ref(subject_kind, subject_ref, entry) not in excluded_refs}
            if selected_versions != remaining:
                raise target_content_issue(
                    'target_formal_reused_artifact_mismatch',
                    f'Reused {subject_kind} {subject_ref!r} artifact_paths {paths!r} do not match its previously accepted '
                    'retained artifacts. Omit artifact_paths override or declare the exact previously retained paths.',
                    verify_only=verify_only,
                )
        return
    selected = _selected_subject_artifacts(entries, paths, verify_only=verify_only)
    ordinals, retained = {}, set()
    for entry in selected:
        binding = entry['binding']
        dependency = (entry['role'], binding['version_ref'])
        if dependency in retained:
            continue
        retained.add(dependency)
        role = {'log': 'log_asset', 'analysis': 'analysis_asset', 'data': 'data_asset'}[entry['role']]
        ordinal = ordinals.get(role, 0)
        ordinals[role] = ordinal + 1
        role_ref = _artifact_role_ref(subject_kind, subject_ref, entry)
        ensure('rg_experiment_asset_roles', 'role_ref', {
            'role_ref': role_ref, 'subject_kind': subject_kind, 'subject_ref': subject_ref,
            'role': role, 'ordinal': ordinal, 'asset_ref': binding['asset_ref'],
            'version_ref': binding['version_ref'], 'content_hash': binding['content_hash'],
            'manifest_hash': binding['manifest_hash'], 'asset_receipt_ref': binding['receipt']['receipt_ref'],
            'asset_receipt_hash': binding['receipt']['payload_hash'],
            'receipt_ref': _ref('formal_artifact_receipt', role_ref),
            'receipt_hash': _receipt('experiment_asset_role_acceptance', role_ref, {
                'subject_kind': subject_kind, 'subject_ref': subject_ref,
                'role': role, 'ordinal': ordinal, 'asset': binding}), 'accepted_at': accepted_at})
    adjusted = {row[0] for row in connection.execute(text(
        'SELECT role_ref FROM rg_experiment_asset_role_adjustments'))}
    actual = connection.execute(text("SELECT role_ref,version_ref FROM rg_experiment_asset_roles WHERE subject_kind=:kind "
        "AND subject_ref=:ref AND role IN ('log_asset','analysis_asset','data_asset')"),
        {'kind': subject_kind, 'ref': subject_ref}).all()
    # Roles whose current attribution was legitimately corrected by a later
    # Owner adjustment no longer participate in this subject's set comparison
    # on either side: the manifest selection keeps its deterministic role_ref.
    selected_versions = {
        entry['binding']['version_ref'] for entry in selected
        if _artifact_role_ref(subject_kind, subject_ref, entry) not in adjusted}
    if {row[1] for row in actual if row[0] not in adjusted} != selected_versions:
        raise OwnerConflict('target_formal_artifact_assignment_conflict')


def _selected_subject_artifacts(entries, paths, *, verify_only=False):
    eligible = [entry for entry in entries if entry['role'] in {'log', 'analysis', 'data'}
                and entry['declared_relative_path'] != 'handoff/final-message.md']
    if paths is None:
        return []
    available = {entry['declared_relative_path']: entry for entry in eligible}
    if any(path not in available for path in paths):
        missing = [path for path in paths if path not in available]
        raise target_content_issue(
            'target_formal_artifact_path_not_bound',
            f'Declared artifact_paths {missing!r} are not bound to retained log/analysis/data artifacts '
            'in the completion manifest. Ensure those paths are retained or fix artifact_paths.',
            verify_only=verify_only,
        )
    return [available[path] for path in paths]


def _ensure_execution_registration(connection, *, root, manifest, commit_ref, result_entry,
                                   items, non_execution, verify_only):
    primary = items[0] if items else {}
    formal = {key: primary.get(key) for key in ('variant_run_ref', 'evaluation_attempt_ref', 'metric_result_ref')}
    registration = {
        'schema_ref': 'meta-research/root-execution-registration/v1',
        'execution_status': 'not_executed' if non_execution is not None else primary.get('run_status', 'executed'),
        'evaluation_status': primary.get('evaluation_status', 'pending'),
        'target_ref': root['target_ref'], 'target_run_ref': root['target_run_ref'],
        'completion_ref': root['completion_ref'], 'completion_payload_hash': root['completion_payload_hash'],
        'manifest_ref': manifest['manifest_ref'], 'manifest_payload_hash': manifest['payload_hash'],
        'result_document_hash': manifest['result_document_hash'],
        'result_asset': {'asset_ref': result_entry['binding']['asset_ref'],
                         'version_ref': result_entry['binding']['version_ref'],
                         'content_hash': result_entry['binding']['content_hash'],
                         'manifest_hash': result_entry['binding']['manifest_hash'],
                         'source_bytes_sha256': result_entry['content_hash']},
        'legacy_anchor_refs': {key: root[key] for key in formal},
        'formal_primary_refs': formal, 'non_execution_evidence': non_execution,
    }
    values = {'formal_' + key: value for key, value in formal.items()}
    values.update(execution_registration_json=canonical_json(registration),
                  execution_registration_hash=canonical_hash(registration))
    current = connection.execute(text('SELECT formal_variant_run_ref,formal_evaluation_attempt_ref,'
        'formal_metric_result_ref,execution_registration_json,execution_registration_hash FROM '
        'rg_target_root_measurements WHERE measurement_ref=:ref'), {'ref': root['measurement_ref']}).mappings().one()
    if all(value is None for value in current.values()) and not verify_only:
        connection.execute(text('UPDATE rg_target_root_measurements SET ' +
            ','.join(key + '=:' + key for key in values) + ' WHERE measurement_ref=:ref'),
            {**values, 'ref': root['measurement_ref']})
        connection.execute(text('UPDATE rg_target_commits SET formal_evaluation_attempt_ref=:attempt WHERE commit_ref=:ref'),
                           {'attempt': formal['evaluation_attempt_ref'], 'ref': commit_ref})
    elif dict(current) != values:
        raise OwnerConflict('target_formal_execution_registration_invalid')
    commit = connection.execute(text('SELECT formal_evaluation_attempt_ref FROM rg_target_commits WHERE commit_ref=:ref'),
                                {'ref': commit_ref}).first()
    if commit is None or commit.formal_evaluation_attempt_ref != formal['evaluation_attempt_ref']:
        raise OwnerConflict('target_formal_execution_registration_invalid')


def _ensure_link(ensure, root, item, commit_ref):
    ensure('rg_target_root_formal_entities', 'link_ref', {
        'link_ref': _ref('formal_completion_link', root['measurement_ref'], item['ordinal']),
        'measurement_ref': root['measurement_ref'], 'ordinal': item['ordinal'],
        'run_key': item['run_key'], 'attempt_key': item['attempt_key'],
        'variant_run_ref': item['variant_run_ref'], 'evaluation_attempt_ref': item['evaluation_attempt_ref'],
        'metric_result_ref': item['metric_result_ref'], 'target_commit_ref': commit_ref})


def _ensure_input(ensure, ref, kind, subject, inputs, at):
    receipt_ref = _ref('formal_input_receipt', ref)
    inputs_hash = canonical_hash(inputs)
    ensure('rg_experiment_input_bindings', 'binding_ref', {
        'binding_ref': ref, 'subject_kind': kind, 'subject_ref': subject,
        'inputs_json': canonical_json(inputs), 'inputs_hash': inputs_hash,
        'receipt_ref': receipt_ref, 'receipt_hash': _receipt('experiment_input_binding_acceptance', ref, {
            'schema_ref': 'meta-research/experiment-input-binding/v1', 'subject_kind': kind,
            'subject_ref': subject, 'inputs_hash': inputs_hash}), 'accepted_at': at})


def query_target_formal_results(connection, target_ref):
    links = connection.execute(text(
        'SELECT l.*,m.manifest_ref,m.completion_ref FROM rg_target_root_formal_entities l JOIN rg_target_root_measurements m '
        'USING (measurement_ref) WHERE m.target_ref=:ref ORDER BY l.ordinal'), {'ref': target_ref}).mappings().all()
    results = []
    for link in links:
        item = dict(link)
        if ((link['evaluation_attempt_ref'] is None and link['metric_result_ref'] is not None)
                or (link['evaluation_attempt_ref'] is None) != (link['attempt_key'] is None)):
            raise OwnerConflict('target_formal_entity_integrity_invalid')
        for label, table, key in (
            ('variant_run', 'rg_variant_runs', 'variant_run_ref'),
            ('evaluation_attempt', 'rg_evaluation_attempts', 'evaluation_attempt_ref'),
            ('metric_result', 'rg_metric_results', 'metric_result_ref'),
        ):
            if link[key] is None:
                item[label] = None
                continue
            row = connection.execute(text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': link[key]}).mappings().first()
            if row is None:
                raise OwnerConflict('target_formal_entity_missing')
            value = dict(row)
            if label == 'metric_result':
                value['metrics'] = json.loads(value.pop('metrics_json'))
                if canonical_hash(value['metrics']) != value['metrics_hash']:
                    raise OwnerConflict('target_formal_entity_integrity_invalid')
            else:
                binding = connection.execute(text('SELECT inputs_json,inputs_hash FROM rg_experiment_input_bindings WHERE binding_ref=:ref'), {'ref': row['input_binding_ref']}).mappings().first()
                if binding is None or canonical_hash(json.loads(binding['inputs_json'])) != binding['inputs_hash']:
                    raise OwnerConflict('target_formal_entity_integrity_invalid')
                value['inputs'] = json.loads(binding['inputs_json'])
            if label == 'evaluation_attempt':
                value['checkpoint_role_refs'] = json.loads(value['checkpoint_role_refs_json'])
            item[label] = value
        if item['evaluation_attempt'] is not None and (
                (item['metric_result'] is None) != (item['evaluation_attempt']['status'] == 'failed')):
            raise OwnerConflict('target_formal_entity_integrity_invalid')
        for label, kind, key in (('run_artifacts', 'variant_run', 'variant_run_ref'),
                                 ('evaluation_artifacts', 'evaluation_attempt', 'evaluation_attempt_ref')):
            item[label] = [] if link[key] is None else [dict(row) for row in connection.execute(text(
                'SELECT role_ref,role,ordinal,accepted_at,asset_ref,version_ref,content_hash,manifest_hash FROM rg_experiment_asset_roles '
                'WHERE subject_kind=:kind AND subject_ref=:ref ORDER BY role,ordinal,accepted_at,role_ref'),
                {'kind': kind, 'ref': link[key]}).mappings()]
        results.append(item)
    return tuple(results)


def _verify_declared_subject_artifact_bindings(work, entries):
    """Reject revisable new-work selections before the formal write transaction."""
    available = {entry['declared_relative_path'] for entry in entries
                 if entry['role'] in {'log', 'analysis', 'data'}
                 and entry['declared_relative_path'] != 'handoff/final-message.md'}
    for run, attempt in work:
        for kind, producer, reuse_key in (
            ('Run', run, 'variant_run_ref'),
            ('Evaluation', attempt, 'evaluation_attempt_ref'),
        ):
            if producer is None or producer.get(reuse_key):
                continue
            missing = [path for path in producer.get('artifact_paths') or [] if path not in available]
            if not missing:
                continue
            related = sorted(path for path in available if any(
                path.startswith(selected + '/') or selected.startswith(path + '/')
                for selected in missing))
            error = TargetContentIssue('target_root_commit_domain_invalid')
            error.feedback = (
                kind + ' artifact_paths are not bound to exact retained log, analysis or data assets: '
                + canonical_json(missing[:8]) + '. '
                + ('The immutable completion selected a different directory boundary at '
                   + canonical_json(related[:8]) + '. First confirm that each declared workspace path '
                   'exists and identifies the intended contents. If correct, keep artifact_paths '
                   'unchanged and finish another normal root turn; the host will freeze the new '
                   'completion at those exact file or directory boundaries. '
                   if related else
                   'Check that each path exists as a retained log, analysis or data asset '
                   'and belongs to this actual producer. Correct missing, mistyped or wrong-role '
                   'declarations, then finish another normal root turn. ')
                + 'Preserve existing results; do not rerun the research or broaden a selected '
                  'artifact to unrelated contents. The previous manifest remains unchanged.'
            )
            raise error


def _verify_new_checkpoint_selections(items, entries, *, require_explicit=False):
    """Return new-work path mistakes through the existing revision boundary."""
    checkpoints = [entry for entry in entries if entry['role'] == 'checkpoint']
    new_runs = {item['variant_run_ref']: item.get('checkpoint_paths')
                for item in items if not item['reuse_variant_run']}
    defaults = _default_checkpoint_paths(new_runs, checkpoints, require_explicit=require_explicit)

    def select(records, paths, kind, run_key):
        try:
            return _select_checkpoint_records(records, paths)
        except OwnerConflict as cause:
            if cause.code != 'target_formal_checkpoint_path_not_bound':
                raise
            available = {record['declared_relative_path'] for record in records}
            missing = [path for path in paths if path not in available]
            roles = {entry['declared_relative_path']: entry['role'] for entry in entries}
            examples = [{'path': path, 'manifest_role': roles.get(path)} for path in missing[:8]]
            error = TargetContentIssue('target_root_commit_domain_invalid')
            error.feedback = (
                kind + ' checkpoint_paths are not bound to this Run\'s exact retained checkpoint assets '
                + '(run_key=' + repr(run_key) + '): ' + canonical_json(examples) + '. '
                'New checkpoint files must be retained with role checkpoint in the completion; '
                'a data role alone does not declare a checkpoint. Evaluation checkpoint_paths must select '
                'a subset of its own Run checkpoints. Correct the file organization and exact handoff '
                'paths while preserving the trained bytes, metrics and existing results; do not rerun '
                'training. Any retained data copy still needs its actual Run or Evaluation artifact_paths '
                'owner. Finish another normal root turn to freeze the corrected completion; the previous '
                'manifest remains unchanged.'
            )
            raise error from cause

    for item in items:
        # Reused state is authenticated against its historical role receipts by
        # registration/replay, not against this completion's new manifest.
        if item['reuse_variant_run']:
            continue
        paths = item.get('checkpoint_paths')
        selected = select(checkpoints, defaults if paths is None else paths, 'Run', item['run_key'])
        if item['evaluation_attempt_ref'] is not None and not item['reuse_evaluation_attempt']:
            select(selected, item.get('evaluation_checkpoint_paths'), 'Evaluation', item['run_key'])


def verify_retained_products(document, entries, *, work=None):
    """Every new retained scientific product has an explicitly declared producer."""
    if explicit_unexecuted_root_evidence(document) is not None:
        return
    work = _declared_work(document) if work is None else work
    primary = work[0][0]
    items = _root_work_items_from_declared(work=work,
        payload={'measurement_ref': 'inventory', 'variant_run_ref': primary.get('variant_run_ref', 'primary'),
        'evaluation_attempt_ref': 'assessment', 'metric_result_ref': 'result'},
        identities={'variant_ref': 'inventory', 'evaluation_ref': 'inventory'},
        declared_runs=document.get('formal_runs') if isinstance(document, dict) else None)
    _verify_declared_subject_artifact_bindings(work, entries)
    assigned = set()
    for item in items:
        if not item['reuse_variant_run']:
            assigned.update(item.get('artifact_paths') or [])
            assigned.update(item.get('checkpoint_paths') or [])
        if item['evaluation_attempt_ref'] is not None and not item['reuse_evaluation_attempt']:
            assigned.update(item.get('evaluation_artifact_paths') or [])
    missing = [entry['declared_relative_path'] for entry in entries
               if entry['role'] in {'analysis', 'data', 'checkpoint'} and not _is_target_note(entry)
               and entry['declared_relative_path'] not in assigned
               and not any(entry['declared_relative_path'].startswith(path + '/') for path in assigned)]
    if missing:
        error = TargetContentIssue('target_root_commit_domain_invalid')
        error.feedback = ('Retained research products need their actual Run or Evaluation owner: '
            + canonical_json(missing) + '. Assign these exact frozen paths with artifact_paths or '
            'checkpoint_paths in formal_runs. A selected directory containing a Target note still '
            'needs attribution for its other contents. A canonical note file or a verified '
            'note-only directory can remain Target content; do not invent an '
            'execution. Preserve the existing work; correct its handoff attribution.')
        raise error
    _verify_new_checkpoint_selections(items, entries, require_explicit=True)
    implementation = [entry['declared_relative_path'] for entry in entries
                      if entry['role'] == 'implementation']
    default_implementation = [entry['declared_relative_path'] for entry in entries
        if entry['role'] == 'implementation' and entry.get('path') is None]
    for item in items:
        if not item['reuse_variant_run']:
            paths = item.get('implementation_paths')
            assigned.update(path for path in implementation
                            if (paths is None and len(default_implementation) == 1 and path in default_implementation)
                            or (isinstance(paths, list) and path in paths))
    _verify_reusable_candidates(document, entries, assigned)


def _verify_reusable_candidates(document, entries, assigned):
    """Index candidates refer to the same exact, attributed completion assets."""
    from meta_research.target_implementation_bundle import (
        TargetImplementationBundleError, validate_bundle_relative_path,
    )

    declarations = []
    for field, label, roles in (
        ('dataset_candidates', 'Dataset', {'data', 'analysis'}),
        ('environment_candidates', 'Environment', {'data', 'analysis', 'implementation', 'checkpoint', 'log'}),
    ):
        candidates = document.get(field, [])
        if not isinstance(candidates, list):
            error = TargetContentIssue('target_root_commit_domain_invalid')
            error.feedback = ('$.' + field + ' received ' + _declaration_value_summary(candidates)
                + '; must be a list of declarations, each with '
                'nonempty artifact_path, name and purpose. Preserve the retained research products, '
                'correct this declaration in the result document and finish another normal root turn.')
            raise error
        for ordinal, candidate in enumerate(candidates):
            if (not isinstance(candidate, dict) or any(not isinstance(candidate.get(key), str)
                    or not candidate[key].strip() for key in ('artifact_path', 'name', 'purpose'))):
                error = TargetContentIssue('target_root_commit_domain_invalid')
                error.feedback = ('$.' + field + '[' + str(ordinal) + '] received '
                    + _declaration_value_summary(candidate) + '; each entry needs nonempty artifact_path, name and '
                    'purpose. Preserve the retained research products, correct the incomplete declaration '
                    'and finish another normal root turn.')
                raise error
            try:
                validate_bundle_relative_path(candidate['artifact_path'])
            except TargetImplementationBundleError as cause:
                error = TargetContentIssue('target_root_commit_domain_invalid')
                error.feedback = (label + ' candidate artifact_path must be a canonical relative workspace '
                    'path without traversal, absolute prefixes or unsupported characters: '
                    + repr(candidate['artifact_path']) + '. Correct the declaration to an existing exact '
                    'retained path, preserve all files and finish another normal root turn.')
                raise error from cause
            declarations.append((field, label, roles, candidate))
    for field, label, roles, candidate in declarations:
        path = candidate['artifact_path']
        resolved = resolve_artifact_selection(entries, path)
        owner_paths = assigned
        owned = path in owner_paths or (resolved is not None
            and any(path.startswith(parent + '/') for parent in owner_paths))
        if (not owned or resolved is None or resolved['role'] not in roles):
            parents = [entry['declared_relative_path'] for entry in entries
                       if entry['role'] in {'data', 'analysis'}
                       and path.startswith(entry['declared_relative_path'] + '/')]
            error = TargetContentIssue('target_root_commit_domain_invalid')
            error.feedback = (
                label + ' candidate ' + canonical_json(path) + ' must name its exact retained artifact '
                'and actual Run or Evaluation owner, with nonempty name and purpose. '
                + ('The existing immutable completion froze the containing collection at '
                   + canonical_json(parents) + '. First confirm that the exact candidate path exists '
                   'in the workspace and identifies the intended contents. If that subpath is correct, '
                   'keep ' + field + ' unchanged and finish another normal root '
                   'turn; the host will freeze the new completion at the declared candidate boundary. '
                   'Do not broaden this resource to its parent, download it again or rerun the research. '
                   if parents else
                   'Check the exact workspace path and its actual producer attribution. For implementation, '
                   'checkpoint and log candidates, select a verified logical content selection; implementation_paths '
                   'selects the actual adopted Run snapshot. Preserve existing results and correct '
                   'the path and producer declaration before finishing another normal root turn. ')
                + 'The previous manifest and accepted content remain unchanged.'
            )
            raise error


def verify_formal_variant_scope(connection, *, target_ref, variant_refs, verify_only=False):
    quest = connection.execute(text('SELECT g.quest_ref FROM rg_targets t JOIN rg_target_graphs g '
        'ON g.graph_ref=t.graph_ref WHERE t.target_ref=:ref'), {'ref': target_ref}).scalar_one_or_none()
    if quest is None:
        raise OwnerConflict('target_formal_variant_scope_invalid')
    for ref in set(variant_refs):
        row = connection.execute(text('SELECT b.quest_ref FROM rg_experiment_variants v JOIN '
            'rg_experiment_baselines b USING(baseline_ref) WHERE v.variant_ref=:ref'), {'ref': ref}).first()
        if row is None:
            raise target_content_issue(
                'target_formal_variant_definition_invalid',
                f'Declared variant_ref {ref!r} is not registered in Research Graph.',
                verify_only=verify_only,
            )
        if row.quest_ref != quest:
            raise OwnerConflict('target_formal_variant_scope_invalid')


def resolve_artifact_selection(entries, artifact_path):
    """Resolve an exact host-signed logical selection without guessing a parent."""
    matches = [entry for entry in entries if entry['declared_relative_path'] == artifact_path]
    if len(matches) > 1:
        raise OwnerConflict('target_formal_artifact_selection_ambiguous')
    return matches[0] if matches else None
