"""Register accepted root work in native entities and enforce completion links."""
from __future__ import annotations
import json
from types import SimpleNamespace
import sqlalchemy as sa
from alembic import op

revision = '0048_root_formal_entities'
down_revision = '0047_baseline_method_identity'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    existing_foreign_key_violations = {
        tuple(row) for row in connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall()
    }
    op.create_table(
        'rg_target_root_unassessed_runs',
        sa.Column('association_ref', sa.String(96), primary_key=True),
        sa.Column('target_ref', sa.String(96), nullable=False),
        sa.Column('completion_ref', sa.String(96), nullable=False),
        sa.Column('manifest_ref', sa.String(96), nullable=False),
        sa.Column('authority_ref', sa.String(96), nullable=False),
        sa.Column('run_key', sa.String(128), nullable=False),
        sa.Column('variant_run_ref', sa.String(96), nullable=False),
        sa.Column('input_binding_ref', sa.String(96), nullable=False),
        sa.Column('input_binding_hash', sa.String(64), nullable=False),
        sa.Column('checkpoint_roles_json', sa.Text(), nullable=False),
        sa.Column('checkpoint_roles_hash', sa.String(64), nullable=False),
        sa.UniqueConstraint('completion_ref', 'run_key'),
        sa.CheckConstraint('length(input_binding_hash)=64 AND length(checkpoint_roles_hash)=64'),
        *[sa.ForeignKeyConstraint([column], [table + '.' + key])
          for column, table, key in (
            ('target_ref', 'rg_targets', 'target_ref'),
            ('completion_ref', 'ar_target_root_completions', 'completion_ref'),
            ('manifest_ref', 'rm_target_root_completion_manifests', 'manifest_ref'),
            ('authority_ref', 'rg_target_measurement_domain_authorities', 'authority_ref'),
            ('variant_run_ref', 'rg_variant_runs', 'variant_run_ref'),
            ('input_binding_ref', 'rg_experiment_input_bindings', 'binding_ref'))],
    )
    op.create_index('ix_root_unassessed_target', 'rg_target_root_unassessed_runs', ['target_ref'])
    # The original anchor columns were issued before execution registration and
    # remain immutable history. Only the new formal refs assert actual entities.
    for column in ('formal_variant_run_ref', 'formal_evaluation_attempt_ref', 'formal_metric_result_ref'):
        op.add_column('rg_target_root_measurements', sa.Column(column, sa.String(96), nullable=True))
    op.add_column('rg_target_root_measurements', sa.Column('execution_registration_json', sa.Text(), nullable=True))
    op.add_column('rg_target_root_measurements', sa.Column('execution_registration_hash', sa.String(64), nullable=True))
    op.add_column('rg_target_commits', sa.Column('formal_evaluation_attempt_ref', sa.String(96), nullable=True))
    op.create_table(
        'rg_target_root_formal_entities',
        sa.Column('link_ref', sa.String(96), primary_key=True),
        sa.Column('measurement_ref', sa.String(96), nullable=False),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.Column('run_key', sa.String(128), nullable=False),
        sa.Column('attempt_key', sa.String(128), nullable=True),
        sa.Column('variant_run_ref', sa.String(96), nullable=False),
        sa.Column('evaluation_attempt_ref', sa.String(96), nullable=True),
        sa.Column('metric_result_ref', sa.String(96), nullable=True),
        sa.Column('target_commit_ref', sa.String(96), nullable=False),
        sa.UniqueConstraint('measurement_ref', 'ordinal'),
        sa.UniqueConstraint('measurement_ref', 'run_key', 'attempt_key'),
        sa.UniqueConstraint('measurement_ref', 'evaluation_attempt_ref'),
        sa.CheckConstraint('ordinal >= 0'),
        sa.CheckConstraint(
            '(evaluation_attempt_ref IS NULL AND metric_result_ref IS NULL AND attempt_key IS NULL) OR '
            '(evaluation_attempt_ref IS NOT NULL AND attempt_key IS NOT NULL)',
            name='ck_root_formal_evaluation_metric_pair'),
        *[sa.ForeignKeyConstraint([column], [table + '.' + key], deferrable=True, initially='DEFERRED')
          for column, table, key in (
            ('measurement_ref', 'rg_target_root_measurements', 'measurement_ref'),
            ('variant_run_ref', 'rg_variant_runs', 'variant_run_ref'),
            ('evaluation_attempt_ref', 'rg_evaluation_attempts', 'evaluation_attempt_ref'),
            ('metric_result_ref', 'rg_metric_results', 'metric_result_ref'),
            ('target_commit_ref', 'rg_target_commits', 'commit_ref'))],
    )
    op.create_index('uq_root_formal_run_without_evaluation', 'rg_target_root_formal_entities',
                    ['measurement_ref', 'run_key'], unique=True,
                    sqlite_where=sa.text('evaluation_attempt_ref IS NULL'))
    from meta_research.formal_entities import register_root_entities
    from meta_research.owners.common import canonical_hash, canonical_json
    from meta_research.owners.target_root_lifecycle import SQLiteTargetRootLifecycleAuthority
    from meta_research.target_run_finalizer import _receipt, RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND
    # Backfill only accepted completions. No lifecycle/status is interpreted as a result.
    for root in connection.execute(sa.text('SELECT * FROM rg_target_root_measurements')).mappings().all():
        def row(table, key, ref):
            value = connection.execute(sa.text(f'SELECT * FROM {table} WHERE {key}=:ref'), {'ref': ref}).mappings().first()
            if value is None:
                raise RuntimeError('root_formal_migration_missing_' + table)
            return value
        authority = row('rg_target_measurement_domain_authorities', 'authority_ref', root['authority_ref'])
        manifest = row('rm_target_root_completion_manifests', 'manifest_ref', root['manifest_ref'])
        completion = row('ar_target_root_completions', 'completion_ref', root['completion_ref'])
        commit = row('rg_target_commits', 'target_ref', root['target_ref'])
        accepted_completion = SQLiteTargetRootLifecycleAuthority._completion_from_row(SimpleNamespace(**completion))
        manifest_payload = {
            'completion_ref': accepted_completion.completion_ref,
            'target_ref': accepted_completion.handle.target_ref,
            'target_run_ref': accepted_completion.handle.target_run_ref,
            'workspace_ref': accepted_completion.workspace_ref,
            'implementation_revision_ref': accepted_completion.implementation_revision_ref,
            'implementation_tree_hash': accepted_completion.implementation_tree_hash,
            'result_document_path': accepted_completion.handoff.result_document_path,
            'result_document': json.loads(manifest['result_document_json']),
            'result_document_hash': accepted_completion.result_document_hash,
            'artifact_snapshot_hash': accepted_completion.artifact_snapshot_hash,
            'entries': json.loads(manifest['entries_json']),
            'completion_receipt': accepted_completion.receipt.as_public_dict(),
        }
        manifest_hash = canonical_hash(manifest_payload)
        expected_manifest_receipt = _receipt('research_memory', RM_TARGET_ROOT_COMPLETION_MANIFEST_RECEIPT_KIND,
            manifest['receipt_ref'], manifest['manifest_ref'],
            {'manifest_ref': manifest['manifest_ref'], 'payload_hash': manifest_hash, **manifest_payload})
        if (manifest['payload_json'] != canonical_json(manifest_payload) or manifest['payload_hash'] != manifest_hash
                or manifest['receipt_hash'] != expected_manifest_receipt.payload_hash
                or manifest['completion_receipt_hash'] != accepted_completion.receipt.payload_hash
                or manifest['request_hash'] != canonical_hash({'command': 'accept_target_root_completion_manifest', **manifest_payload})
                or any(manifest[key] != manifest_payload[key] for key in (
                    'completion_ref', 'target_ref', 'target_run_ref', 'workspace_ref', 'implementation_revision_ref',
                    'implementation_tree_hash', 'result_document_path', 'result_document_hash', 'artifact_snapshot_hash'))):
            raise RuntimeError('root_formal_migration_manifest_integrity_invalid')
        for value, json_key, hash_key in (
            (root, 'measurement_payload_json', 'measurement_payload_hash'),
            (root, 'metrics_json', 'metrics_hash'), (root, 'variant_input_binding_json', 'variant_input_binding_hash'),
            (root, 'evaluation_input_binding_json', 'evaluation_input_binding_hash'),
            (commit, 'closure_json', 'closure_hash'), (manifest, 'entries_json', 'entries_hash'),
            (manifest, 'result_document_json', 'result_document_hash')):
            if canonical_hash(json.loads(value[json_key])) != value[hash_key]:
                raise RuntimeError('root_formal_migration_hash_invalid_' + hash_key)
        if (authority['authority_hash'] != root['authority_hash'] or
                completion['payload_hash'] != root['completion_payload_hash'] or
                manifest['payload_hash'] != root['manifest_payload_hash']):
            raise RuntimeError('root_formal_migration_source_mismatch')
        register_root_entities(connection, root=root, authority=authority, manifest=manifest,
                               completion=completion, commit_ref=commit['commit_ref'])
    # Existing root references are preserved; constraints are added after the backfill.
    with op.batch_alter_table('rg_target_root_measurements', recreate='always',
                             naming_convention={'uq': 'uq_%(table_name)s_%(column_0_name)s'}) as batch:
        batch.drop_constraint('uq_rg_target_root_measurements_variant_run_ref', type_='unique')
        batch.drop_constraint('uq_rg_target_root_measurements_evaluation_attempt_ref', type_='unique')
        batch.drop_constraint('uq_rg_target_root_measurements_metric_result_ref', type_='unique')
        batch.create_check_constraint('ck_root_measurement_formal_primary_refs',
            '(formal_variant_run_ref IS NULL AND formal_evaluation_attempt_ref IS NULL AND formal_metric_result_ref IS NULL) OR '
            '(formal_variant_run_ref IS NOT NULL AND formal_evaluation_attempt_ref IS NULL AND formal_metric_result_ref IS NULL) OR '
            '(formal_variant_run_ref IS NOT NULL AND formal_evaluation_attempt_ref IS NOT NULL)')
        batch.create_check_constraint('ck_root_measurement_execution_registration',
            '(execution_registration_json IS NULL AND execution_registration_hash IS NULL) OR '
            '(execution_registration_json IS NOT NULL AND execution_registration_hash IS NOT NULL AND length(execution_registration_hash) = 64)')
        for column, table, key in (
            ('authority_ref', 'rg_target_measurement_domain_authorities', 'authority_ref'),
            ('formal_variant_run_ref', 'rg_variant_runs', 'variant_run_ref'),
            ('formal_evaluation_attempt_ref', 'rg_evaluation_attempts', 'evaluation_attempt_ref'),
            ('formal_metric_result_ref', 'rg_metric_results', 'metric_result_ref')):
            batch.create_foreign_key('fk_root_measurement_' + column, table, [column], [key],
                                     deferrable=True, initially='DEFERRED')
    with op.batch_alter_table('rg_target_commits', recreate='always') as batch:
        batch.create_foreign_key('fk_target_commit_formal_evaluation_attempt', 'rg_evaluation_attempts',
                                 ['formal_evaluation_attempt_ref'], ['evaluation_attempt_ref'],
                                 deferrable=True, initially='DEFERRED')
    # Earlier migrations intentionally retain some unbound legacy artifacts.
    # This migration must introduce no violations; it does not reclassify those
    # unrelated historical rows or silently repair their source receipts.
    violations = {tuple(row) for row in connection.exec_driver_sql('PRAGMA foreign_key_check').fetchall()}
    if violations - existing_foreign_key_violations:
        raise RuntimeError('root_formal_migration_foreign_key_invalid')


def downgrade():
    raise RuntimeError('production migrations are forward-only')
