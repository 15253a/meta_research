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
import json
from meta_research.codex_runtime import CODEX_MODEL_REF, CODEX_LOCKED_VERSION
from meta_research.acquisition_root import CodexAcquisitionRootAdapter
from meta_research.bundle_skill import CodexBundleSkillAdapter
from meta_research.companion import CodexCompanionAdapter
from meta_research.deepfetch import CodexDeepFetchAdapter
from meta_research.idea_skill import CodexIdeaSkillAdapter
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.reasoning_skill import CodexReasoningSkillAdapter
from meta_research.writing_skill import CodexWritingSkillAdapter
from meta_research.quest_drafting import (
    CodexDraftingAdapter, _DRAFTING_MODEL_CATALOG_PATH,
    _DRAFTING_MODEL_CATALOG_SHA256, _verified_drafting_model_catalog,
)
from meta_research.root_capabilities import ROOT_AGENT_KINDS, root_capability_profile
from meta_research.web import StartHarnessConformanceRequest
from pydantic import ValidationError
adapters = [CodexAcquisitionRootAdapter, CodexBundleSkillAdapter,
            CodexCompanionAdapter, CodexDeepFetchAdapter, CodexIdeaSkillAdapter,
            CodexPlanSkillAdapter, CodexReasoningSkillAdapter,
            CodexWritingSkillAdapter, CodexDraftingAdapter]
defaults = [inspect.signature(adapter).parameters['model_ref'].default for adapter in adapters]
assert defaults == [CODEX_MODEL_REF] * len(adapters)
assert all(root_capability_profile(kind).as_dict()['model_ref'] == CODEX_MODEL_REF
           for kind in ROOT_AGENT_KINDS)
assert _verified_drafting_model_catalog(_DRAFTING_MODEL_CATALOG_PATH,
    model_ref=CODEX_MODEL_REF) == _DRAFTING_MODEL_CATALOG_SHA256
assert StartHarnessConformanceRequest(codex_auth_profile_ref='test').codex_model_ref == CODEX_MODEL_REF
assert StartHarnessConformanceRequest(codex_auth_profile_ref='test', codex_model_ref=CODEX_MODEL_REF).codex_model_ref == CODEX_MODEL_REF
other = 'gpt-5.6-sol' if CODEX_MODEL_REF == 'gpt-6.1-sol' else 'gpt-6.1-sol'
try:
    StartHarnessConformanceRequest(codex_auth_profile_ref='test', codex_model_ref=other)
except ValidationError:
    pass
else:
    raise AssertionError('diagnostic must use the configured deployment model')
print(json.dumps({'model':CODEX_MODEL_REF, 'cli':CODEX_LOCKED_VERSION,
                  'root_count':len(ROOT_AGENT_KINDS), 'adapter_count':len(adapters)}))
'''
    return subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize("model", [None, "gpt-6.1-sol", "gpt-5.6-sol"])
def test_selected_model_reaches_all_roots_catalog_and_diagnostic(model):
    result = _isolated_model_check(model)
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert observed["model"] == (model or "gpt-6.1-sol")
    assert observed["cli"] == "0.159.0"


def test_unknown_deployment_model_fails_before_provider_execution():
    result = _isolated_model_check("unknown-model")
    assert result.returncode != 0
    assert "META_RESEARCH_CODEX_MODEL must be" in result.stderr
