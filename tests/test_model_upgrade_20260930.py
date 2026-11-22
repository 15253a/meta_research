"""Conformance accepts the current Codex model choices."""
import pytest
from pydantic import ValidationError
from meta_research.codex_runtime import CODEX_MODEL_REF, SUPPORTED_CODEX_MODEL_REFS
from meta_research.web import StartHarnessConformanceRequest


def test_web_conformance_accepts_all_visible_models():
    assert StartHarnessConformanceRequest(codex_model_ref='gpt-6.1-sol', codex_auth_profile_ref='test').codex_model_ref == CODEX_MODEL_REF
    for model in SUPPORTED_CODEX_MODEL_REFS:
        assert StartHarnessConformanceRequest(codex_model_ref=model, codex_auth_profile_ref='test').codex_model_ref == model
    with pytest.raises(ValidationError):
        StartHarnessConformanceRequest(codex_model_ref='gpt-unmanaged', codex_auth_profile_ref='test')
