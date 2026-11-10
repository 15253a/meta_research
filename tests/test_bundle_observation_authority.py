from meta_research.bundle_skill import CodexBundleSkillAdapter
from test_bundle_skill_adapter import _SequenceRunner, _fake_codex
from test_public_bundle_stage import _bundle_runtime, _prepare_bundle_request


def test_changed_real_provider_artifact_is_a_failure_before_provider_entry(tmp_path):
    executable = _fake_codex(tmp_path / "codex")
    runner = _SequenceRunner([])
    adapter = CodexBundleSkillAdapter(tmp_path / "provider", executable=str(executable), process_runner=runner)
    runtime = _bundle_runtime(tmp_path / "data", bundle_skill_provider=adapter, harness_ready=False)
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        original = executable.read_bytes()
        executable.write_bytes(original + b"\n# changed provider artifact\n")
        assert not runtime.bundle_stage.process_once()
        assert runtime.bundle_stage.observation.kind == "failure"
        assert runtime.bundle_stage.observation.code == "bundle_runtime_binding_drift"
        assert runner.calls == []
        executable.write_bytes(original)
        assert runtime.bundle_stage.query_current()["run"]["status"] == "admitted"
    finally:
        runtime.close()
