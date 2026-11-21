"""Deployment selection applies to every production root and its admission paths."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SOURCE = Path(__file__).parents[1] / "src"


def _isolated_model_check(model: str | None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(SOURCE)
    environment.pop("META_RESEARCH_CODEX_MODEL", None)
    if model is not None:
        environment["META_RESEARCH_CODEX_MODEL"] = model
    script = '''
import inspect
import hashlib
import json
from pathlib import Path
import tempfile
from meta_research.codex_runtime import CODEX_MODEL_REF, CODEX_LOCKED_VERSION
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.companion import CodexCompanionAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.quest_drafting import (
    CodexDraftingAdapter, _DRAFTING_MODEL_CATALOG_PATH,
    _DRAFTING_MODEL_CATALOG_SHA256, _verified_drafting_model_catalog,
)
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile
from meta_research.recorder_sources import RecorderChannels
from meta_research.timeline_summary_provider import CodexTimelineSummaryAdapter
from meta_research.web import StartHarnessConformanceRequest
from pydantic import ValidationError
adapters = [CodexAcquisitionRootAdapter, CodexBundleSkillAdapter,
            CodexCompanionAdapter, CodexDeepFetchAdapter, CodexIdeaSkillAdapter,
            CodexPlanSkillAdapter, CodexReasoningSkillAdapter,
            CodexDraftingAdapter]
defaults = [inspect.signature(adapter).parameters['model_ref'].default for adapter in adapters]
assert defaults == [CODEX_MODEL_REF] * len(adapters)
assert all(root_capability_profile(kind).as_dict()['model_ref'] == CODEX_MODEL_REF
           for kind in ROOT_AGENT_KINDS)
assert _verified_drafting_model_catalog(_DRAFTING_MODEL_CATALOG_PATH,
    model_ref=CODEX_MODEL_REF) == _DRAFTING_MODEL_CATALOG_SHA256
with tempfile.TemporaryDirectory() as temporary:
    workspace = Path(temporary)
    channels = RecorderChannels()
    channels.url = 'http://127.0.0.1:8771/mcp'
    adapter = CodexTimelineSummaryAdapter(workspace, channels=channels)
    for native in (None, 'existing-session'):
        argv = adapter._provider_argv(workspace / 'schema.json', workspace / 'result.json',
                                      native_session_ref=native, ephemeral=False)
        assert argv.count('--model') == 1
        assert argv[argv.index('--model') + 1] == CODEX_MODEL_REF
        if native is not None:
            assert argv[argv.index('resume') + 1] == native
        configs = [argv[i + 1] for i, flag in enumerate(argv) if flag == '--config']
        catalog_setting, = [value for value in configs if value.startswith('model_catalog_json=')]
        catalog_path = Path(json.loads(catalog_setting.split('=', 1)[1]))
        assert catalog_path.is_absolute()
        raw_catalog = catalog_path.read_bytes()
        entry, = json.loads(raw_catalog)['models']
        assert entry['slug'] == CODEX_MODEL_REF
        assert entry['shell_type'] == 'disabled'
        assert entry['apply_patch_tool_type'] is None and not entry['supports_search_tool']
        assert entry['experimental_supported_tools'] == []
        if CODEX_MODEL_REF == 'gpt-6.2-sol':
            assert entry['context_window'] == 500000
            assert entry['max_context_window'] == 872000
            assert entry['truncation_policy'] == {'mode': 'tokens', 'limit': 10000}
            assert entry['default_reasoning_level'] == 'max'
assert StartHarnessConformanceRequest(codex_auth_profile_ref='test').codex_model_ref == CODEX_MODEL_REF
assert StartHarnessConformanceRequest(codex_auth_profile_ref='test', codex_model_ref=CODEX_MODEL_REF).codex_model_ref == CODEX_MODEL_REF
other = 'gpt-unmanaged'
try:
    StartHarnessConformanceRequest(codex_auth_profile_ref='test', codex_model_ref=other)
except ValidationError:
    pass
else:
    raise AssertionError('diagnostic must use the configured deployment model')
print(json.dumps({'model':CODEX_MODEL_REF, 'cli':CODEX_LOCKED_VERSION,
                  'catalog_file':catalog_path.name, 'catalog_sha256':hashlib.sha256(raw_catalog).hexdigest(),
                  'root_count':len(ROOT_AGENT_KINDS), 'adapter_count':len(adapters)}))
'''
    return subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(("model", "catalog_file", "legacy_hash"), [
    (None, "codex_drafting_model_catalog.json", "591c4d314dec8c8f2ff0102ba193ae42478f7bf7f0e6e07f4e5180f49ba6af77"),
    ("gpt-6.1-sol", "codex_drafting_model_catalog.json", "591c4d314dec8c8f2ff0102ba193ae42478f7bf7f0e6e07f4e5180f49ba6af77"),
    ("gpt-5.6-sol", "codex_drafting_model_catalog_gpt_5_6_sol.json", "9d9b4936f927a6a855625bbbe0f52241d044d0d271c8fe2def611a7188cd5c9b"),
    ("gpt-6.2-sol", "codex_drafting_model_catalog_gpt_6_2_sol.json", None),
    ("gpt-6.2-astra", "generated", None),
], ids=["default", "6.1", "5.6", "6.2", "6.2-astra"])
def test_selected_model_reaches_all_roots_catalog_and_diagnostic(model, catalog_file, legacy_hash):
    result = _isolated_model_check(model)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["model"] == (model or "gpt-6.1-sol")
    assert observed["cli"] == "0.159.0"
    if catalog_file == "generated":
        assert observed["catalog_file"].startswith("gpt-6.2-astra-")
    else:
        assert observed["catalog_file"] == catalog_file
    if legacy_hash is not None:
        assert observed["catalog_sha256"] == legacy_hash


def test_unknown_deployment_model_fails_before_provider_execution():
    result = _isolated_model_check("unknown-model")
    assert result.returncode != 0
    assert "META_RESEARCH_CODEX_MODEL must be" in result.stderr
