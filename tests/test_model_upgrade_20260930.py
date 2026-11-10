"""Conformance rejects unsupported model choices."""
import pytest
from pydantic import ValidationError
from meta_research.codex_runtime import CODEX_MODEL_REF
from meta_research.web import StartHarnessConformanceRequest


def test_web_conformance_accepts_only_the_current_model():
    assert StartHarnessConformanceRequest(codex_model_ref='gpt-6.1-sol', codex_auth_profile_ref='test').codex_model_ref == CODEX_MODEL_REF
    with pytest.raises(ValidationError):
        StartHarnessConformanceRequest(codex_model_ref='gpt-6-sol', codex_auth_profile_ref='test')
