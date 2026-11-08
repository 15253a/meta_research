"""Isolated #263 actual Codex consumer probe; no production service mutation.

Fixtures only create the accepted research state and platform lease. The real
installed CLI/model receives a real Idea-scoped MCP bearer, calls the production
gateway/Owners over loopback, and decides its own corrective calls. No scripted
tool retries or fabricated model transcript. Output directories must be new.
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
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', '--root', type=Path, required=True)
    parser.add_argument('--source-head', help='Externally verified revision for an exported source archive')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--codex-executable', required=True)
    parser.add_argument('--codex-home', type=Path, default=os.environ.get('CODEX_HOME'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--scenario', choices=('reader', 'effect', 'both'), default='both')
    args = parser.parse_args()
    if args.codex_home is None:
        parser.error('--codex-home or CODEX_HOME is required')
    if args.source_head is not None and not re.fullmatch(r'[0-9a-f]{40}', args.source_head):
        parser.error('--source-head must be the verified full 40-character revision')
    source, output = args.source.resolve(), args.output.resolve()
    codex_home = args.codex_home.resolve()
    output.mkdir(parents=True, exist_ok=False)
    sys.path[:0] = [str(source / 'src'), str(source / 'tests')]
    from conftest import _DeterministicPowerInhibitor
    from test_root_workspace import _make, _channel, _call
    from test_public_plan_stage import _confirm_direct_quest
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation
    from meta_research.semantic_owner_gateway import ROOT_AGENT_SEMANTIC_OPERATION_IDS

    private_values = [value for key, value in os.environ.items()
                      if re.search(r'KEY|TOKEN|SECRET|PASSWORD', key, re.I) and len(value) >= 8]
    def safe_text(text):
        for secret in private_values:
            text = text.replace(secret, '<redacted-secret>')
        return text
    def write_json(path, value):
        path.write_text(safe_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)) + '\n', encoding='utf-8')

    source_head = args.source_head or subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip()
    manifest = {str(path.relative_to(source)).replace('\\', '/'):
                hashlib.sha256(path.read_bytes()).hexdigest()
                for folder in ('src', 'tests') for path in sorted((source / folder).rglob('*'))
                if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'}
    write_json(output / 'source-file-sha256.json', manifest)
    with patch('meta_research.composition.ProductionPowerInhibitor', lambda *_: _DeterministicPowerInhibitor()):
        runtime = _make(output / 'data')
    server = None
    server_thread = None
    audit = []
    audit_lock = threading.Lock()
    originals = {}
    summary = {'status': 'running', 'scenario': args.scenario,
               'source_head': source_head,
               'source_head_basis': 'caller-verified archive' if args.source_head else 'local git HEAD',
               'source_manifest_files': len(manifest),
               'source': str(source), 'model': args.model,
               'boundary': 'Fixture-created accepted initial state and deterministic platform lease; real model/CLI, live role grant, real gateway and Owners. Not deployed-service or full-stage acceptance.'}
    write_json(output / 'summary.json', summary)
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = runtime.idea_stage.start('issue263-real-model')
        run = runtime.owners.agent_runtime.query_idea_stage_run(admitted['stage_run_request']['request_ref'])
        assert run is not None
        scope = SimpleNamespace(run_ref=run.run_ref, attempt_ref=run.attempt_ref,
            root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
            capability_binding_hash=run.runtime_binding_hash, root_kind='idea', phase='primary')
        channel = _channel(runtime, scope)
        private_values.append(channel.connection.token)
        question = runtime.owners.research_graph.query_question(quest['initialization_id'])
        assert question is not None

        class McpHandler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                if self.path != '/mcp':
                    self.send_error(404)
                    return
                try:
                    raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    request = json.loads(raw)
                    bearer = self.headers.get('Authorization', '').removeprefix('Bearer ')
                    status, response, session = runtime.harnesses.dispatch_mcp_http(
                        bearer, request, mcp_session_id=self.headers.get('Mcp-Session-Id'))
                    event = {'at_monotonic': time.monotonic(), 'request': request,
                             'http_status': status, 'response': response}
                    with audit_lock:
                        event['sequence'] = len(audit)
                        audit.append(event)
                        with (output / 'mcp-audit.jsonl').open('a', encoding='utf-8') as stream:
                            stream.write(safe_text(json.dumps(event, ensure_ascii=False)) + '\n')
                    body = b'' if response is None else json.dumps(response, ensure_ascii=False).encode()
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    if session:
                        self.send_header('Mcp-Session-Id', session)
                    self.end_headers()
                    self.wfile.write(body)
                except Exception as error:
                    # Preserve the failure; a transport error must never become success.
                    write_json(output / 'http-error.json', {'type': type(error).__name__, 'message': str(error)})
                    self.send_error(500)

            def do_GET(self):
                # Standalone server-to-client SSE is optional; JSON POST is used.
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
        url = f'http://127.0.0.1:{server.server_port}/mcp'
        cli_runs = []

        def actual_runner(argv, input_text, timeout_seconds, environment):
            index = len(cli_runs)
            metadata = {'index': index, 'argv': argv, 'timeout': timeout_seconds}
            cli_runs.append(metadata)
            write_json(output / f'cli-{index:02d}-command.json', metadata)
            env = {**os.environ, **environment, 'CODEX_HOME': str(codex_home)}
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
                (output / f'cli-{index:02d}.jsonl').write_text(safe_text(stdout), encoding='utf-8')
                (output / f'cli-{index:02d}.stderr').write_text(safe_text(stderr), encoding='utf-8')
                raise
            (output / f'cli-{index:02d}.jsonl').write_text(safe_text(stdout), encoding='utf-8')
            (output / f'cli-{index:02d}.stderr').write_text(safe_text(stderr), encoding='utf-8')
            metadata.update(returncode=process.returncode, stdout_sha256=hashlib.sha256(stdout.encode()).hexdigest())
            write_json(output / f'cli-{index:02d}-command.json', metadata)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

        adapter = CodexHarnessAdapter(output / 'model-workspace', executable=args.codex_executable,
            runner=actual_runner, codex_home=codex_home, timeout_seconds=args.timeout)
        native_session = None
        summary['scope'] = vars(scope)
        summary['granted_operation_ids'] = list(ROOT_AGENT_SEMANTIC_OPERATION_IDS['idea'])

        def turn(name, prompt):
            nonlocal native_session
            write_json(output / f'{name}-prompt.json', {'prompt': prompt})
            invocation = HarnessInvocation(harness_family='codex',
                provider_operation_ref='issue263-probe-' + name,
                run_ref=run.run_ref, attempt_ref=run.attempt_ref, attempt_generation=1,
                root_session_ref=run.root_session_ref, fence_ref=run.fence_ref,
                model_ref=args.model, prompt=prompt, mcp_url=url, mcp_token=channel.connection.token,
                native_session_ref=native_session, root_kind='idea',
                entry_path='initial' if native_session is None else 'resume',
                authorized_operation_ids=ROOT_AGENT_SEMANTIC_OPERATION_IDS['idea'],
                provider_operation_timeout_seconds=args.timeout)
            evidence = adapter.invoke(invocation)
            if native_session is not None:
                assert evidence.native_session_ref == native_session, 'Model resumed a different native session'
            native_session = evidence.native_session_ref
            write_json(output / f'{name}-adapter-evidence.json', asdict(evidence))
            summary[name + '_native_session_ref'] = native_session
            return evidence

        def calls_since(start):
            return [entry for entry in audit[start:] if entry['request'].get('method') == 'tools/call']

        if args.scenario in {'reader', 'both'}:
            start = len(audit)
            stale = {'source_ref': question.question_ref, 'version_ref': 'inherited-stale-version'}
            turn('reader', 'You are the admitted Idea researcher in an isolated acceptance task. '
                'Use only the granted meta_research MCP tools for this task. Do not use shell/files/network/subagents or inspect service internals. '
                'First try the inherited research_memory.content.read pointer exactly as supplied: ' + json.dumps(stale) + '. '
                'Obtain the complete current accepted question and briefly summarize its research goal. '
                'If that pointer is rejected, use the returned explanation and legitimate current tools to correct your own reader submission, '
                'then continue in this session. Do not change research facts, authorization, source code or validation rules. '
                'The successful follow-up must read actual content, not merely describe what you would do.')
            calls = calls_since(start)
            assert calls, 'No real model tool calls observed'
            first = calls[0]
            assert first['request']['params']['name'] == 'research_memory.content.read', 'Inherited pointer was not attempted first'
            assert first['request']['params']['arguments'] == stale, 'Inherited pointer was silently changed before rejection'
            rejected = first['response']['result']
            assert rejected['isError'] and rejected['structuredContent']['code'] == 'content_version_unbound'
            assert json.loads(rejected['content'][0]['text']) == rejected['structuredContent']
            corrected = [entry for entry in calls[1:] if entry['request']['params']['name'] == 'research_memory.content.read'
                and entry['request']['params']['arguments'].get('version_ref') == question.content_ref
                and entry['response']['result'].get('isError') is False]
            assert corrected, 'The model did not correct the reference and read the actual accepted content'
            assert any(entry['request']['params']['name'] == 'research_graph.questions.page' for entry in calls[1:]), 'No actual discovery of the current reader'
            summary['reader'] = {'status': 'passed', 'rejection_sequence': first['sequence'],
                'corrected_sequences': [entry['sequence'] for entry in corrected],
                'exact_version_ref': question.content_ref, 'same_scope': True}

        if args.scenario in {'effect', 'both'}:
            gateway = runtime.harnesses._gateway
            name = 'research_memory.literature.register'
            original_operation = gateway._operations[name]
            originals[name] = original_operation
            handler_results = []
            def lose_receipt(context, arguments):
                value = original_operation.handler(context, arguments)
                handler_results.append({'arguments': dict(arguments), 'result': value})
                # Fault after real Owner commit, before gateway output validation.
                return 'controlled-unusable-receipt' if len(handler_results) == 1 else value
            gateway._operations[name] = replace(original_operation, handler=lose_receipt)
            submission = {'effect_id': 'issue263-original-effect',
                'record_ref': 'doi:10.1000/issue263-acceptance', 'title': 'Isolated diagnostic evidence',
                'source': 'Controlled acceptance fixture; metadata only', 'fulltext_status': 'missing'}
            start = len(audit)
            turn('effect', 'Continue this same Idea session. Use only granted meta_research MCP tools, no shell/files/network/subagents. '
                'Register this metadata once using research_memory.literature.register: ' + json.dumps(submission) + '. '
                'Report the actual registered literature ref and retained fulltext status. '
                'Use tool feedback to establish the actual result; preserve the original identity and valid existing work. '
                'Do not invent full text, change the scientific metadata or bypass validation. After the result is established, read the accepted record to confirm it remains usable.')
            calls = calls_since(start)
            writes = [entry for entry in calls if entry['request']['params']['name'] == name]
            assert len(writes) == 1 and len(handler_results) == 1, 'Model repeated an effect instead of reconciling'
            assert writes[0]['request']['params']['arguments'] == submission
            failed = writes[0]['response']['result']
            assert failed['isError'] and failed['structuredContent']['code'] == 'semantic_output_schema_mismatch'
            assert failed['structuredContent']['details']['effect_status'] == 'unknown'
            reconciling = [entry for entry in calls if entry['request']['params']['name'] == name + '.reconcile']
            assert reconciling and all(entry['request']['params']['arguments'].get('effect_id') == submission['effect_id'] for entry in reconciling)
            accepted = handler_results[0]['result']['result']
            assert any(entry['response']['result'].get('isError') is False
                and entry['response']['result']['structuredContent']['result'] == accepted for entry in reconciling)
            reads = [entry for entry in calls if entry['request']['params']['name'] == 'research_memory.literature.read'
                and entry['request']['params']['arguments'].get('literature_ref') == accepted['ref']
                and entry['response']['result'].get('isError') is False]
            assert reads, 'Model did not continue with the retained accepted record'
            # Independently query the real original effect after the model finishes;
            # this check is not added to the model's MCP audit or transcript.
            check = _call(runtime, channel, name + '.reconcile', effect_id=submission['effect_id'])
            assert not check['isError'] and check['structuredContent']['result'] == accepted
            summary['effect'] = {'status': 'passed', 'write_calls': len(writes),
                'owner_handler_calls': len(handler_results), 'effect_id': submission['effect_id'],
                'accepted_result': accepted, 'reconcile_sequences': [entry['sequence'] for entry in reconciling],
                'continued_read_sequences': [entry['sequence'] for entry in reads]}
            write_json(output / 'controlled-fault.json', {'boundary': 'Real Owner succeeded; wrapper corrupted one output only.',
                'owner_results': handler_results})
        summary['status'] = 'passed'
        summary['native_session_ref'] = native_session
    except BaseException as error:
        summary.update(status='failed', failure_type=type(error).__name__, failure=str(error))
        raise
    finally:
        summary['http_calls'] = len(audit)
        write_json(output / 'summary.json', summary)
        if server is not None:
            server.shutdown()
            server.server_close()
        if server_thread is not None:
            server_thread.join(timeout=5)
        for name, operation in originals.items():
            runtime.harnesses._gateway._operations[name] = operation
        runtime.close()
    print(json.dumps({'status': summary['status'], 'output': str(output),
                      'native_session_ref': summary.get('native_session_ref')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
