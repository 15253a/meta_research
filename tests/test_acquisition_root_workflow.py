from pathlib import Path
from dataclasses import replace
import pytest
from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper, AcquisitionPreflightRequest, AcquisitionUnavailable
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from test_external_root_resident_mcp import _AcquisitionDelegate, _SequenceRunner, _fake_codex


def batch(tmp_path, **changes):
    target = (tmp_path / "downloads").resolve()
    target.mkdir(exist_ok=True)
    request = AcquisitionBatchRequest("batch1", "oa_first_then_institution",
        (AcquisitionPaper("p1", "Exact title", None, None, ()),),
        session_ref="session1", session_mode="oa_only", target_dir=str(target),
        provider_state_dir=str(tmp_path / "private"))
    return replace(request, **changes)


def missing():
    return {"paper_id": "p1", "status": "missing", "path": None, "format": None,
            "failure": {"code": "oa_not_found", "detail": "OA routes exhausted."}}


def test_root_executes_download_instead_of_accepting_delegate_output(tmp_path):
    runner = _SequenceRunner([{"results": [missing()]}])
    delegate = _AcquisitionDelegate()
    adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", delegate,
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    result = adapter.acquire(batch(tmp_path))
    assert result[0].paper_id == "p1" and result[0].status == "missing"
    assert delegate.batches == []
    assert "Exact title" in runner.prompts[0]
    assert "run_batch_download.py" in runner.prompts[0]
    assert "typed observation" not in runner.prompts[0]


def test_missing_mode_fails_before_any_provider_work(tmp_path):
    runner = _SequenceRunner([])
    adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    with pytest.raises(AcquisitionUnavailable, match="acquisition_provider_binding_invalid"):
        adapter.acquire(batch(tmp_path, session_mode=""))
    assert not runner.prompts


def test_initial_connection_failure_has_formal_receipt_and_original_effect_reconcile(tmp_path):
    from meta_research.composition import build_production_runtime
    from meta_research.paths import prepare_data_root
    from meta_research.power_inhibitors import OperatorAttestedPowerInhibitor
    from test_external_root_resident_mcp import _QuestDrafter, _HostProbe

    waiting = {"status": "waiting_user", "browser_context_ref": None,
        "reason_code": "institutional_login_required", "evidence": {
            "configuration_health": "ready", "browser_control": "functional", "authorized_resource": "login_required"}}
    ready = {"status": "ready", "browser_context_ref": "page-verified", "reason_code": None,
        "evidence": {"configuration_health": "ready", "browser_control": "functional", "authorized_resource": "verified"}}

    class Runner(_SequenceRunner):
        runtime = None
        opened = None
        def __call__(self, argv, prompt, timeout, environment=None):
            token = environment["META_RESEARCH_MCP_TOKEN"]
            if self.opened is None:
                def call(operation, arguments):
                    status, response = self.runtime.harnesses.dispatch_mcp(token,
                        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": operation, "arguments": arguments}})
                    assert status == 200, response
                    result = response["result"]
                    assert not result.get("isError"), result
                    return result["structuredContent"]
                self.opened = call("human_request.open", {"effect_id": "first-login",
                    "request_kind": "library_reconnect", "obligation": "Reconnect library access",
                    "business_purpose": "The configured library requires login to fetch the requested file.",
                    "condition": {"impact": "Download cannot continue.", "safe_response": "Sign in in the controlled browser."},
                    "acceptance_conditions": ["Institution access verified."]})
                self.reconciled = call("human_request.open.reconcile", {"effect_id": "first-login"})
            return super().__call__(argv, prompt, timeout, environment)

    runner = Runner([waiting, ready])
    adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    drafter = _QuestDrafter()
    runtime = build_production_runtime(prepare_data_root(tmp_path / "data"), acquisition_provider=adapter,
        proposal_drafter=drafter, intent_drafting_provider=drafter, host_compute_probe=_HostProbe(),
        power_inhibitor=OperatorAttestedPowerInhibitor(), startup_power_probe=False, startup_harness_diagnostics=False)
    runner.runtime = runtime
    try:
        runtime.configure_resident_mcp_endpoint("http://127.0.0.1:18768")
        human = runtime.owners.human_collaboration
        creation = human.create_quest({}, "first-login-create")
        initialization = creation["initialization_id"]
        draft = dict(creation["quest_draft"]["value"])
        draft["literature"] = {"mode": "oa_then_institution", "library_entry_url": "https://library.example/login",
            "scope_exclusions": "", "accepted_material_bindings": []}
        revised = human.revise_quest_draft(initialization, draft, creation["quest_draft"]["hash"],
            "first-login-draft", creation["quest_draft"]["revision"])
        session = runtime.owners.agent_runtime.prepare_acquisition_session(initialization_id=initialization,
            draft_revision=revised["quest_draft"]["revision"], config={"mode": "oa_then_institution",
            "library_entry_url": "https://library.example/login"}, provider=adapter)
        assert session.status == "waiting_user", session
        assert runner.opened["receipt"]["kind"] == "human_request_open"
        assert runner.opened["operation_binding"]["quest_ref"] is None
        assert runner.reconciled["request_ref"] == runner.opened["request_ref"]
        assert len(runtime.owners.agent_runtime.query_human_requests()) == 1
        request_ref = runner.opened["request_ref"]
        human.respond_to_human_request(request_ref, decision="provided",
            facts={"route": "institutional_browser_reconnected"}, note="Signed in in original browser.",
            idempotency_key="first-login-response")
        current = runtime.owners.agent_runtime.query_acquisition_session(session_ref=session.session_ref)
        assert current.status == "ready"
        assert current.preflight_generation == 2
        resolved = runtime.owners.agent_runtime.query_human_request(request_ref)
        assert resolved["status"] == "satisfied", resolved
        assert resolved["direct_waiters"][0]["status"] == "consumed"
        assert "resume" in runner.argvs[1]
        assert "Nature Downloader：DeepFetch" not in runner.prompts[1]
    finally:
        runtime.close()


def test_packaged_launcher_downloads_real_http_body_and_reuses_completed_spool(tmp_path):
    import functools, hashlib, http.server, json, os, subprocess, sys, threading
    from meta_research.acquisition import freeze_acquisition_item_artifacts
    source = tmp_path / "http-source"
    source.mkdir()
    body = b"<!doctype html><html><article><h1>Exact title</h1><p>Full article body for controlled acquisition verification.</p></article></html>"
    (source / "article.html").write_bytes(body)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(source))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request = batch(tmp_path, papers=(AcquisitionPaper("p1", "Exact title", None, None,
        (f"http://127.0.0.1:{server.server_port}/article.html",)),))

    class DownloadRunner(_SequenceRunner):
        def __call__(self, argv, prompt, timeout, environment=None):
            import meta_research.acquisition_root as module
            launcher = Path(module.__file__).parent / "skills/deepfetch_v4/providers/nature-downloader/scripts/run_batch_download.py"
            result = subprocess.run([sys.executable, str(launcher), "--pdf-url", request.papers[0].source_urls[0],
                "--title", "Exact title", "--no-institutional-access", "--no-si", "--cnki-format", "pdf",
                "--out", request.target_dir], capture_output=True, text=True, timeout=45,
                env=dict(os.environ, LIT_DL_CONFIG_DIR=str(tmp_path / "private-config")))
            assert result.returncode == 0, result.stderr
            observed = json.loads(result.stdout)
            self.observation = observed
            files = list(Path(request.target_dir).rglob("*.html"))
            assert len(files) == 1, observed
            self._outputs = iter([{"results": [{"paper_id": "p1", "status": "obtained",
                "path": str(files[0].resolve()), "format": "html", "failure": None}]}])
            return super().__call__(argv, prompt, timeout, environment)
    runner = DownloadRunner([])
    adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    try:
        first = adapter.acquire(request)
        verified = freeze_acquisition_item_artifacts(request, first)
        assert verified[0].content_sha256 == hashlib.sha256(body).hexdigest()
        assert Path(verified[0].path).read_bytes() == body
        assert adapter.reconcile(request) == first
        assert len(runner.prompts) == 1
        (tmp_path / "download-observation.json").write_text(json.dumps(runner.observation), encoding="utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_provided_only_native_policy_does_not_leak_to_next_request(tmp_path):
    from unittest.mock import Mock
    runner = _SequenceRunner([{"results": [missing()]}, {"results": [missing()]}])
    adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(_fake_codex(tmp_path / "codex")), process_runner=runner)
    external = Mock()
    external.operation_snapshot.side_effect = AssertionError("provided_only opened an external MCP channel")
    adapter.bind_external_mcp(external)
    adapter.acquire(batch(tmp_path, session_mode="provided_only"))
    args = runner.argvs[0]
    assert 'web_search="disabled"' in args
    assert 'sandbox_workspace_write.network_access=false' in args
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    assert str((tmp_path / "downloads").resolve()) in args
    external.operation_snapshot.assert_not_called()
    # A second adapter uses the same durable root without carrying policy state.
    next_adapter = CodexAcquisitionRootAdapter(tmp_path / "provider", _AcquisitionDelegate(),
        executable=str(tmp_path / "codex"), process_runner=runner)
    next_adapter.acquire(batch(tmp_path, request_id="batch2", session_mode="oa_only"))
    assert 'web_search="disabled"' not in runner.argvs[1]
    assert "resume" in runner.argvs[1]


def test_unknown_native_effect_is_reconciled_without_repeating_download(tmp_path):
    import subprocess
    class InterruptedRunner(_SequenceRunner):
        attempts = 0
        def __call__(self, argv, prompt, timeout, environment=None):
            self.attempts += 1
            raise subprocess.TimeoutExpired(argv, 1)
    runner = InterruptedRunner([])
    workspace = tmp_path / "provider"
    executable = str(_fake_codex(tmp_path / "codex"))
    adapter = CodexAcquisitionRootAdapter(workspace, _AcquisitionDelegate(), executable=executable, process_runner=runner)
    request = batch(tmp_path)
    with pytest.raises(AcquisitionUnavailable):
        adapter.acquire(request)
    resumed = CodexAcquisitionRootAdapter(workspace, _AcquisitionDelegate(), executable=executable, process_runner=runner)
    with pytest.raises(AcquisitionUnavailable, match="reconciliation_pending"):
        resumed.reconcile(request)
    assert runner.attempts == 1
