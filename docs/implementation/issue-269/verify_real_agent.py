"""Actual Codex consumer acceptance for the purpose catalog, in new isolated data.

The initial Quest, guidance, materials and human reply are controlled fixtures.
The admitted Idea provider unit remains live while the installed CLI/model chooses
all discovery, exact-contract and business calls through production gateways.
One receipt is deliberately lost AFTER the real literature Owner commits.
The external MCP is a real subprocess fixture, not a production supplier.
No service deployment or existing research directory is modified.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--source-head', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codex-executable', required=True)
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--timeout', type=float, default=420)
    parser.add_argument('--prepare-only', action='store_true', help='Validate real fixture/grant/server setup without starting a model')
    parser.add_argument('--scenario', choices=('all', 'discovery'), default='all')
    parser.add_argument('--require-exact-asset-reader', action='store_true',
                        help='Assert after the turn that the model did not guess a logical asset_ref as the exact reader source')
    args = parser.parse_args()
    if not re.fullmatch(r'[0-9a-f]{40}', args.source_head):
        parser.error('--source-head requires the verified full revision')
    source, output = args.source.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path[:0] = [str(source / 'src'), str(source / 'tests')]
    from conftest import _DeterministicPowerInhibitor
    from test_root_workspace import _make, _context, _directories
    from test_public_plan_stage import _confirm_direct_quest, _DeterministicIdeaSkill
    from test_research_datasets import _asset
    from test_human_history_discovery import _request
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation
    from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS
    from meta_research.human_guidance import GuidanceRuntimeScope, StageGuidanceOperation
    from meta_research.owners.common import canonical_hash
    from meta_research.external_mcp import parse_services, compose_external_mcp_prompt

    secrets = [value for key, value in os.environ.items()
               if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I) and len(value) >= 8]

    def safe(text):
        for secret in secrets:
            text = text.replace(secret, '<redacted-secret>')
        return text

    def write(path, value):
        path.write_text(safe(json.dumps(value, ensure_ascii=False, indent=2, default=str)) + '\n', encoding='utf-8')

    manifest = {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in ('src', 'tests') for path in sorted((source / folder).rglob('*'))
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    write(output / 'source-file-sha256.json', manifest)
    summary = {'status': 'running', 'source_head': args.source_head, 'model': args.model, 'scenario': args.scenario,
               'boundary': 'Fixture initial state and deterministic platform lease; real admitted Idea unit, real model/CLI and production Owners. Real controlled external MCP subprocess. Not full Cycle or production deployment acceptance.'}
    write(output / 'summary.json', summary)
    audit, cli_runs = [], []
    audit_lock = threading.Lock()
    runtime, server, server_thread, external_access = None, None, None, None
    originals = {}
    probe_error = []

    def actual_runner(argv, input_text, timeout_seconds, environment):
        index = len(cli_runs)
        metadata = {'index': index, 'argv': argv, 'timeout': timeout_seconds}
        cli_runs.append(metadata)
        write(output / f'cli-{index:02d}-command.json', metadata)
        env = {**os.environ, **environment, 'CODEX_HOME': str(args.codex_home.resolve())}
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', env=env, start_new_session=True)
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout_seconds or args.timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            (output / f'cli-{index:02d}.jsonl').write_text(safe(stdout), encoding='utf-8')
            (output / f'cli-{index:02d}.stderr').write_text(safe(stderr), encoding='utf-8')
            raise
        (output / f'cli-{index:02d}.jsonl').write_text(safe(stdout), encoding='utf-8')
        (output / f'cli-{index:02d}.stderr').write_text(safe(stderr), encoding='utf-8')
        metadata.update(returncode=process.returncode, stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest())
        write(output / f'cli-{index:02d}-command.json', metadata)
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

    def semantic_calls(start=0):
        """Keep original wire records; normalize only for independent assertions."""
        calls = []
        for entry in audit[start:]:
            request = entry['request']
            if request.get('method') != 'tools/call' or entry['path'] != '/mcp':
                continue
            params = request['params']
            if params['name'] == 'tools.call':
                arguments = params['arguments']
                name, arguments = arguments['operation_id'], arguments.get('arguments', {})
            else:
                name, arguments = params['name'], params.get('arguments', {})
            calls.append({'name': name, 'arguments': arguments, 'sequence': entry['sequence'],
                          'result': entry['response'].get('result', entry['response'])})
        return calls

    def successful(calls, name):
        return [item for item in calls if item['name'] == name and item['result'].get('isError') is False]

    def probe(request):
        nonlocal server, server_thread, external_access
        context = _context(request, 'idea')
        scope = GuidanceRuntimeScope('idea', request.run_ref, request.attempt_ref,
            request.root_session_ref, request.fence_ref, context.capability_binding_hash)
        cut = runtime.owners.human_collaboration.freeze_operation_guidance(
            StageGuidanceOperation(scope, request.job_ref, 'primary'))
        channel = runtime.harnesses.issue_resident_mcp_channel(
            run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            capability_binding_hash=context.capability_binding_hash,
            root_kind='idea', phase='primary', subject_policy='operation_tree',
            operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS['idea'], guidance_binding=cut.binding)
        secrets.append(channel.connection.token)
        summary['scope'] = asdict(scope)
        summary['guidance_binding'] = cut.binding.as_dict()

        class McpHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                if self.path not in {'/mcp', '/mcp/external'}:
                    self.send_error(404)
                    return
                try:
                    wire = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                    bearer = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    if self.path == '/mcp/external':
                        status, response, session = runtime.external_mcp.dispatch_http(bearer, wire)
                    else:
                        status, response, session = runtime.harnesses.dispatch_mcp_http(bearer, wire,
                            mcp_session_id=self.headers.get('Mcp-Session-Id'))
                    entry = {'at_monotonic': time.monotonic(), 'path': self.path,
                             'request': wire, 'http_status': status, 'response': response}
                    with audit_lock:
                        entry['sequence'] = len(audit)
                        audit.append(entry)
                        with (output / 'mcp-audit.jsonl').open('a', encoding='utf-8') as stream:
                            stream.write(safe(json.dumps(entry, ensure_ascii=False)) + '\n')
                    body = b'' if response is None else json.dumps(response, ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    if session:
                        self.send_header('Mcp-Session-Id', session)
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    write(output / 'http-error.json', {'type': type(error).__name__, 'message': str(error)})
                    self.send_error(500)

            def do_GET(self):
                self.send_error(405)

            def do_DELETE(self):
                self.send_response(200)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), McpHandler)
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        base_url = f'http://127.0.0.1:{server.server_port}'
        runtime.external_mcp.configure_endpoint(base_url)
        runtime.external_mcp.save_config(services=parse_services([{
            'service_id': 'lab', 'name': 'Acceptance lab thermometer', 'allowed_root_kinds': ['idea'],
            'research_instructions': 'Read a specified sensor. Observations do not become accepted research facts.',
            'connection': {'transport': 'stdio', 'command': sys.executable,
                'arguments': [str(source / 'tests/fixtures/external_mcp_server.py')],
                'environment': {'EXTERNAL_MCP_LEDGER': str(output / 'external-calls.jsonl')}}}]),
            expected_revision=runtime.external_mcp.read_config().revision)
        snapshot = runtime.external_mcp.operation_snapshot(operation_identity='issue269-actual-consumer',
            root_kind='idea', task_prompt='Compare original retained observations and current workspace notes.')
        external_access = runtime.external_mcp.acquire(snapshot, resident_token=channel.connection.token)
        secrets.append(external_access.token)
        directories = _directories(runtime, context)
        workspace = runtime.root_workspaces.bind_runtime(context).location.directory
        draft = workspace / 'working-copy.txt'
        draft.write_text('UNACCEPTED_WORKING_COPY_269: tentative 91 percent; not the retained observation.\n')
        draft_hash = hashlib.sha256(draft.read_bytes()).hexdigest()
        write(output / 'native-directories.json', directories)
        if args.prepare_only:
            summary.update(status='prepared', current_workspace_count=len(directories),
                           guidance_deliveries=len(cut.deliveries), external_services=len(snapshot.services))
            return
        adapter = CodexHarnessAdapter(workspace, executable=args.codex_executable,
            runner=actual_runner, codex_home=args.codex_home.resolve(), timeout_seconds=args.timeout)
        native_session = None
        turn_outputs = {}

        def turn(name, prompt, *, external=False):
            nonlocal native_session
            write(output / f'{name}-prompt.json', {'prompt': prompt})
            cli_start = len(cli_runs)
            invocation = HarnessInvocation(harness_family='codex',
                provider_operation_ref='issue269-' + name,
                run_ref=request.run_ref, attempt_ref=request.attempt_ref, attempt_generation=1,
                root_session_ref=request.root_session_ref, fence_ref=request.fence_ref,
                model_ref=args.model, prompt=prompt, mcp_url=base_url + '/mcp',
                mcp_token=channel.connection.token, native_session_ref=native_session,
                root_kind='idea', entry_path='initial' if native_session is None else 'resume',
                authorized_operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS['idea'],
                provider_operation_timeout_seconds=args.timeout,
                external_mcp_access=external_access if external else None)
            evidence = adapter.invoke(invocation)
            model_indexes = [item['index'] for item in cli_runs[cli_start:] if 'exec' in item['argv']]
            assert model_indexes, 'No actual model command was executed'
            turn_outputs[name] = '\n'.join((output / f'cli-{index:02d}.jsonl').read_text() for index in model_indexes)
            if native_session:
                assert evidence.native_session_ref == native_session, 'Original consumer session was replaced'
            native_session = evidence.native_session_ref
            write(output / f'{name}-adapter-evidence.json', asdict(evidence))
            summary[name + '_session'] = native_session
            write(output / 'summary.json', summary)

        def finish():
            lists = [item for item in audit if item['path'] == '/mcp' and item['request'].get('method') == 'tools/list']
            assert lists, 'Provider did not request its actual MCP tool inventory'
            for item in lists:
                names = {tool['name'] for tool in item['response']['result']['tools']}
                assert names == {'tools.catalog.page', 'tools.contracts.read', 'tools.call'}, names
            write(output / 'normalized-semantic-calls.json', semantic_calls())
            summary.update(status='passed', native_session_ref=native_session,
                initial_semantic_tool_names=sorted(names), current_workspace_count=len(directories))

        # No operation or argument recipes are supplied for unknown-purpose work.
        turn('discovery',
            'You are the admitted Idea researcher in an isolated acceptance task. Use the granted semantic tools '
            'to find what the researcher previously said about retaining a negative observation. Discover by purpose '
            'and inspect only the relevant exact contracts. Read the accurate historical guidance, then find the '
            'original attached observation and read its retained RM version. Distinguish the human history from a '
            'current request and distinguish an accepted original from a working copy. Read the current request '
            'and its actual human reply. The guidance also applies to this active work: record your understanding, '
            'the change to your work and what continues using the legitimate current feedback capability, '
            'then read back its actual treatment/impact. Do not change the Quest goal or create duplicate requests. '
            'Use native file tools only inside these host-issued workspace directories: ' + json.dumps(directories) + '. '
            'Read working-copy.txt and compare it to the retained material; do not alter either. '
            'Do not inspect service files, database, credentials or network. Finish with the observed exact '
            'marker/text and a short explanation of which version is accepted and which guidance applies.')
        calls = semantic_calls()
        catalogs = successful(calls, 'tools.catalog.page')
        contracts = successful(calls, 'tools.contracts.read')
        assert catalogs and contracts, 'No model-driven progressive discovery / contract use'
        assert any(item['arguments'].get('purpose') == 'RG' for item in catalogs), 'No RG purpose selection'
        history = successful(calls, 'human_request.read')
        assert any(item['arguments'].get('constraint_ref') == guidance['constraint_ref'] for item in history), 'No exact historical guidance read'
        assert any(item['arguments'].get('response_ref') == reply['response_ref'] for item in history), 'No exact human reply read'
        material_reads = [item for item in successful(calls, 'research_memory.content.read')
                          if item['arguments'].get('version_ref') == asset.version_ref]
        assert material_reads and material_reads[-1]['result']['structuredContent']['text'] == material_text
        asset_attempts = [item for item in calls if item['name'] == 'research_memory.content.read'
                          and item['arguments'].get('version_ref') == asset.version_ref]
        logical_asset_guesses = [item for item in asset_attempts if item['arguments'].get('source_ref') == asset.asset_ref]
        if args.require_exact_asset_reader:
            assert not logical_asset_guesses, 'The model still guessed the logical asset_ref after reading the corrected exact contract'
        feedback = successful(calls, 'human_guidance.feedback')
        assert feedback, 'No formal current guidance treatment'
        treatment = feedback[-1]['result']['structuredContent']
        impact_reads = [item for item in successful(calls, 'human_guidance.read')
                        if item['sequence'] > feedback[-1]['sequence']
                        and any(assessment.get('receipt_ref') == treatment['receipt']['receipt_ref']
                                for assessment in item['result']['structuredContent'].get('goal_assessments', []))]
        assert impact_reads, 'No current feedback receipt / goal-impact readback'
        assert hashlib.sha256(draft.read_bytes()).hexdigest() == draft_hash, 'Working copy altered'
        cli_text = turn_outputs['discovery']
        assert 'UNACCEPTED_WORKING_COPY_269' in cli_text, 'No observed native workspace file consumption'
        summary['discovery'] = {'status': 'passed', 'catalog_calls': len(catalogs),
            'contract_calls': len(contracts), 'material_read_sequences': [item['sequence'] for item in material_reads],
            'feedback_sequences': [item['sequence'] for item in feedback],
            'impact_read_sequences': [item['sequence'] for item in impact_reads], 'workspace_unchanged': True,
            'asset_reader_attempts': len(asset_attempts), 'logical_asset_ref_guesses': len(logical_asset_guesses)}
        if args.scenario == 'discovery':
            finish()
            return

        start = len(audit)
        pointer = {'source_ref': asset.version_ref, 'version_ref': asset.version_ref}
        turn('direct-reader', 'Continue the same task. This exact retained reader is already known: '
            + json.dumps({'operation': 'research_memory.content.read', 'arguments': pointer})
            + '. Invoke it immediately and report its actual content. Do not perform directory or contract calls '
            'for this already-known reader. Use only the granted semantic channel.')
        direct = semantic_calls(start)
        assert direct and direct[0]['name'] == 'research_memory.content.read' and direct[0]['arguments'] == pointer
        assert direct[0]['result']['structuredContent']['text'] == material_text
        assert not any(item['name'] in {'tools.catalog.page', 'tools.contracts.read'} for item in direct)
        summary['direct_reader'] = {'status': 'passed', 'sequence': direct[0]['sequence']}

        start = len(audit)
        stale = {'source_ref': question.question_ref, 'version_ref': 'inherited-stale-version'}
        turn('repair', 'Continue the same work. First attempt this inherited exact reader unchanged: '
            + json.dumps({'operation': 'research_memory.content.read', 'arguments': stale})
            + '. Then obtain the actual current accepted question, correcting your own submission if needed '
            'using the real error and current tools. Read its body and summarize the goal. Do not change facts, '
            'permissions, tools or validation, and do not use shell or network for semantic work.')
        repaired = semantic_calls(start)
        assert repaired[0]['name'] == 'research_memory.content.read' and repaired[0]['arguments'] == stale
        assert repaired[0]['result']['structuredContent']['code'] == 'content_version_unbound'
        assert any(item['arguments'].get('version_ref') == question.content_ref
                   for item in successful(repaired, 'research_memory.content.read'))
        summary['repair'] = {'status': 'passed', 'failure_sequence': repaired[0]['sequence']}

        name = 'research_memory.literature.register'
        gateway = runtime.harnesses._gateway
        original = gateway._operations[name]
        originals[name] = original
        committed = []

        def lose_receipt(context, arguments):
            result = original.handler(context, arguments)
            committed.append({'arguments': arguments, 'result': result})
            return 'controlled-lost-receipt' if len(committed) == 1 else result

        gateway._operations[name] = replace(original, handler=lose_receipt)
        submission = {'effect_id': 'issue269-original-effect', 'record_ref': 'doi:10.1000/issue269-acceptance',
            'title': 'Isolated catalog acceptance metadata', 'source': 'Controlled acceptance; metadata only',
            'fulltext_status': 'missing'}
        start = len(audit)
        turn('workflow', 'Continue the same work. First discover the workflow purpose for establishing the result '
            'of an original operation; inspect the appropriate status/reconciliation contract. '
            'Then register this literature metadata once: ' + json.dumps(submission)
            + '. Discover the appropriate purpose and exact contract if needed. Establish what actually happened '
            'using the real tool feedback and workflow status/reconciliation capabilities, then read the accepted '
            'record. If the result is uncertain, discover its original reconciliation operation through the workflow '
            'purpose and follow that actual returned entry before continuing. Preserve original operation identity. Do not invent full text or '
            'repeat an effect because its response is unusable. No shell or service internals.')
        effects = semantic_calls(start)
        writes = [item for item in effects if item['name'] == name]
        assert len(writes) == len(committed) == 1 and writes[0]['arguments'] == submission
        assert writes[0]['result']['structuredContent']['details']['effect_status'] == 'unknown'
        reconciled = successful(effects, name + '.reconcile')
        assert reconciled and all(item['arguments']['effect_id'] == submission['effect_id'] for item in reconciled)
        accepted = committed[0]['result']['result']
        assert any(item['result']['structuredContent']['result'] == accepted for item in reconciled)
        assert any(item['arguments'].get('literature_ref') == accepted['ref']
                   for item in successful(effects, 'research_memory.literature.read'))
        workflow_entries = [item for item in successful(effects, 'tools.catalog.page')
                            if item['arguments'].get('purpose') == 'WF'
                            and writes[0]['sequence'] < item['sequence'] < reconciled[0]['sequence']
                            and any(entry['operation_id'] == name + '.reconcile'
                                    for entry in item['result']['structuredContent'].get('items', []))]
        assert workflow_entries, 'The executed original reconciliation was not discovered through WF after the unknown outcome'
        write(output / 'controlled-fault.json', {'boundary': 'Real Owner commit; one corrupted receipt', 'committed': committed})
        summary['workflow'] = {'status': 'passed', 'owner_writes': len(committed),
                               'catalog_sequences': [item['sequence'] for item in workflow_entries],
                               'reconcile_sequences': [item['sequence'] for item in reconciled]}

        start = len(audit)
        turn('external', compose_external_mcp_prompt(
            'Continue the same research session. Use the configured lab service to read sensor acceptance-269 '
            'and report the actual Celsius result. Discover the configured service, relevant capability and '
            'accurate contract before calling it. Do not invoke a connectivity business action, change Owner '
            'facts or use shell/network to bypass the provided tools. Explain that it is an observation.', snapshot), external=True)
        ledger = [json.loads(line) for line in (output / 'external-calls.jsonl').read_text().splitlines()]
        assert ledger == [{'name': 'read_temperature', 'sensor': 'acceptance-269'}], ledger
        external_calls = [item for item in audit[start:] if item['path'] == '/mcp/external'
                          and item['request'].get('method') == 'tools/call']
        assert len(external_calls) >= 3, 'External discovery, contract and actual call not observed'
        assert '23.75' in turn_outputs['external'], 'No actual external result consumed'
        summary['external'] = {'status': 'passed', 'service': 'controlled real stdio MCP subprocess',
                               'business_calls': ledger, 'call_sequences': [item['sequence'] for item in external_calls]}
        finish()

    class ConsumerIdea(_DeterministicIdeaSkill):
        def generate_draft(self, request):
            try:
                probe(request)
            except BaseException as error:
                probe_error.append(error)
                raise
            return super().generate_draft(request)

    try:
        with patch('meta_research.composition.ProductionPowerInhibitor', lambda *_: _DeterministicPowerInhibitor()):
            runtime = _make(output / 'data', idea=ConsumerIdea())
        quest = _confirm_direct_quest(runtime)
        question = runtime.owners.research_graph.query_question(quest['initialization_id'])
        hc = runtime.owners.human_collaboration
        guidance = hc.submit_human_guidance(quest_ref=quest['quest_ref'],
            original_text='Retain the negative observation and distinguish accepted evidence from working guesses.',
            strength=3, idempotency_key='issue269-guidance')
        material_text = 'ACCEPTED_ORIGINAL_269: the negative control did not reproduce the effect.\n'
        asset = _asset(runtime, content=material_text.encode(), key='issue269-observation')
        runtime.owners.research_graph.accept_asset_role(binding=asset, role='evidence',
            quest_ref=quest['quest_ref'], idempotency_key='issue269-evidence')
        hc.submit_research_input(quest_ref=quest['quest_ref'],
            text_content='Original negative observation; compare this retained material with provisional working notes.',
            asset_bindings=[asset.as_dict()], idempotency_key='issue269-input')
        request = _request(runtime, quest['quest_ref'], 'issue269-current-request')
        reply = hc.respond_to_human_request(request['request_ref'], decision='provided',
            facts={'observation': 'CURRENT_REPLY_269: instrument recalibration is complete.'},
            note='This current reply does not replace the retained negative observation.', idempotency_key='issue269-reply')
        write(output / 'fixture-facts.json', {'quest_ref': quest['quest_ref'], 'guidance_ref': guidance['constraint_ref'],
            'asset': asset.as_dict(), 'request_ref': request['request_ref'], 'response_ref': reply['response_ref']})
        runtime.idea_stage.start('issue269-model-consumer')
        for _ in range(4):
            runtime.idea_stage.process_once()
            if probe_error:
                raise probe_error[0]
            if summary['status'] in {'passed', 'prepared'}:
                break
        assert summary['status'] == ('prepared' if args.prepare_only else 'passed'), 'The real Idea provider unit did not execute the acceptance consumer'
    except BaseException as error:
        summary.update(status='failed', failure_type=type(error).__name__, failure=str(error))
        raise
    finally:
        summary['http_calls'] = len(audit)
        write(output / 'summary.json', summary)
        # Child consumers are legitimate native callers. Export only their
        # completed MCP events, never reasoning, instructions or authentication.
        child_ids = {entry['request'].get('params', {}).get('_meta', {}).get('threadId')
                     for entry in audit if entry['request'].get('method') == 'tools/call'}
        child_ids.discard(None)
        child_ids.discard(summary.get('native_session_ref', summary.get('discovery_session')))
        child_calls = []
        for child_id in sorted(child_ids):
            if not re.fullmatch(r'[0-9a-f-]{36}', child_id):
                continue
            for path in (args.codex_home / 'sessions').rglob('*' + child_id + '*.jsonl'):
                for line in path.read_text().splitlines():
                    event = json.loads(line)
                    value = event.get('payload', {})
                    if event.get('type') == 'event_msg' and value.get('type') == 'item_completed' and value.get('item', {}).get('type') == 'McpToolCall':
                        child_calls.append({'thread_id': child_id, 'item': value['item']})
        write(output / 'child-mcp-events.json', child_calls)
        if external_access is not None and runtime is not None:
            runtime.external_mcp.release(external_access)
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        if runtime is not None:
            for name, operation in originals.items():
                runtime.harnesses._gateway._operations[name] = operation
            runtime.close()
    print(json.dumps({'status': summary['status'], 'output': str(output), 'session': summary.get('native_session_ref')}))


if __name__ == '__main__':
    main()
