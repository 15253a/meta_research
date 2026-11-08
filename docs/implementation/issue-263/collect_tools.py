"""Inventory the actual production registry without invoking a business operation.

Run from the repository with its test dependencies installed:
    python docs/implementation/issue-263/collect_tools.py

Run on Linux, the production runtime's supported platform. This script observes
registration, not provider/model conformance or recovery.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import inspect
import json
from pathlib import Path
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    options = parser.parse_args()
    source = options.source.resolve()
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(source / 'src'), str(source / 'tests')]

    def prohibit_external_effects(event, args):
        if event in {'socket.connect', 'subprocess.Popen', 'os.system'}:
            raise RuntimeError('Registry inventory forbids external effects: ' + event)
    sys.addaudithook(prohibit_external_effects)

    from conftest import _DeterministicPowerInhibitor
    from test_public_bundle_stage import _bundle_runtime
    from meta_research.harness import FULL_CONFORMANCE_OPERATION_IDS
    from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
    from meta_research.work_material_contract import CREATION_MATERIAL_OPERATION_IDS

    def location(handler):
        return {'file': Path(inspect.getsourcefile(handler)).resolve().relative_to(source).as_posix(),
                'line': inspect.getsourcelines(handler)[1], 'function': handler.__qualname__}

    with tempfile.TemporaryDirectory(prefix='issue-263-registry-') as folder:
        runtime = _bundle_runtime(Path(folder), harness_ready=False,
                                  power_inhibitor=_DeterministicPowerInhibitor())
        try:
            gateway = runtime.harnesses._gateway
            rows = []
            for name in gateway.operation_ids:
                operation = gateway._operations[name]
                roles = [role for role, names in ROOT_AGENT_SEMANTIC_OPERATION_IDS.items() if name in names]
                if name == 'timeline.sources.read':
                    roles.append('recorder (job-scoped)')
                if name in CREATION_MATERIAL_OPERATION_IDS:
                    roles.append('companion (creation-materials scoped)')
                if name in FULL_CONFORMANCE_OPERATION_IDS and not roles:
                    roles.append('harness conformance only; no default research-root grant')
                required = operation.input_schema.get('required', [])
                related_tests = []
                for test in sorted((source / 'tests').glob('test_*.py')):
                    if name in test.read_text(encoding='utf-8'):
                        related_tests.append(test.relative_to(source).as_posix())
                contract = {'input': operation.input_schema, 'output': operation.output_schema}
                rows.append({
                    'operation': name, 'owner': operation.owning_module, 'mode': operation.access_mode,
                    'roles_catalog_membership_not_grant': roles,
                    'handler': location(operation.handler),
                    'availability': location(operation.unavailable_reason) if operation.unavailable_reason else None,
                    'required_input_fields': required,
                    'reconciliation_operation': operation.reconciliation_operation_id,
                    'contract_sha256': hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest(),
                    'input_schema': operation.input_schema, 'output_schema': operation.output_schema,
                    'description': operation.description, 'return_description': operation.return_description,
                    'declared_error_descriptions': operation.error_descriptions,
                    'related_test_files_not_execution_evidence': related_tests,
                    'evidence_status': 'production registration observed; business recovery not exercised by inventory',
                })
            assert len(rows) == len({row['operation'] for row in rows})
            assert not gateway._grants
            external_config = runtime.external_mcp.read_config()
            metadata = {
                'assembly': 'test_public_bundle_stage._bundle_runtime -> build_production_runtime -> create_semantic_owner_gateway',
                'count': len(rows), 'mode_counts': dict(sorted(Counter(row['mode'] for row in rows).items())),
                'business_calls': 0, 'model_calls': 0, 'grants_issued': 0,
                'isolated_external_service_count': len(external_config.services),
                'platform': sys.platform,
                'boundary': 'New isolated data root; deterministic repository provider/platform fixtures; no live provider inventory or active deployment configuration implied.',
            }
        finally:
            runtime.close()
    options.output.mkdir(parents=True, exist_ok=True)
    (options.output / 'registered-tools.json').write_text(
        json.dumps({'metadata': metadata, 'operations': rows}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(metadata, ensure_ascii=False))


if __name__ == '__main__':
    main()
