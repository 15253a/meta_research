"""The reviewed model/CLI upgrade preserves historical identity, not execution grants."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from meta_research import root_capabilities
from meta_research.codex_runtime import CODEX_LOCKED_VERSION, CODEX_MODEL_REF
from meta_research.owners.agent_runtime import _runtime_binding_from_row, _validated_runtime_binding
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.runtime_binding_compatibility import (
    bundle_bindings_compatible, reasoning_bindings_compatible,
    reviewed_historical_root_profile_hashes,
)
from meta_research.web import StartHarnessConformanceRequest


FIXTURES = Path(__file__).parent / 'fixtures'
UPGRADE = json.loads((FIXTURES / 'model_upgrade_20260930/profiles.json').read_text())


@pytest.mark.parametrize('kind', root_capabilities.ROOT_AGENT_KINDS)
def test_exact_new_profile_changes_only_model_and_preserves_root_floor(kind):
    current = root_capabilities.root_capability_profile(kind)
    assert CODEX_MODEL_REF == 'gpt-6.1-sol' and CODEX_LOCKED_VERSION == '0.159.0'
    assert current.as_dict() == UPGRADE['after_profile']
    assert current.digest == canonical_hash(UPGRADE['after_profile']) == UPGRADE['after_profile_hash']
    before = UPGRADE['before_profile']
    assert canonical_hash(before) == UPGRADE['before_profile_hash']
    assert [key for key in before if before[key] != current.as_dict()[key]] == ['model_ref']
    expected = {UPGRADE['before_profile_hash']} | {p['root_profile_hash'] for p in UPGRADE['historical_profiles']}
    assert reviewed_historical_root_profile_hashes(current.digest) == expected


def test_prior_current_profile_reads_without_admitting_or_executing_old_bindings():
    rows = json.loads((FIXTURES / 'root_prompt_20260929/historical-stage-rows.json').read_text())
    for stage, row in rows.items():
        # Original signed fixture is first read and authenticated unchanged.
        original_row = deepcopy(row)
        original = _runtime_binding_from_row(SimpleNamespace(**row))
        assert row == original_row and canonical_hash(original.as_dict()) == row['runtime_binding_hash']
        # Synthetic parser input covers the immediately preceding profile;
        # it is not represented as a new signed historical admission.
        previous = replace(original, capability_bindings=tuple(
            'root-capability-profile:sha256:' + UPGRADE['before_profile_hash']
            if value.startswith('root-capability-profile:sha256:') else value
            for value in original.capability_bindings))
        accepted, serialized, digest = _validated_runtime_binding(previous, stage=stage, allow_historical_root_profile=True)
        assert accepted == previous and json.loads(serialized) == previous.as_dict()
        assert digest == canonical_hash(previous.as_dict())
        with pytest.raises(OwnerConflict, match='idea_runtime_binding_unauthorized'):
            _validated_runtime_binding(previous, stage=stage)
        current = replace(previous, model_ref=CODEX_MODEL_REF, capability_bindings=tuple(
            'root-capability-profile:sha256:' + UPGRADE['after_profile_hash']
            if value.startswith('root-capability-profile:sha256:') else value
            for value in previous.capability_bindings))
        if stage == 'bundle':
            assert not bundle_bindings_compatible(previous, current)
        elif stage == 'reasoning':
            assert not reasoning_bindings_compatible(previous, current)
        else:
            assert previous != current


def test_prior_current_diagnostic_preserves_observed_identity():
    diagnostic = root_capabilities.root_capability_profile('target').public_diagnostics()
    diagnostic['capability_profile_hash'] = UPGRADE['before_profile_hash']
    original = deepcopy(diagnostic)
    assert root_capabilities.validate_root_capability_diagnostics(diagnostic) == original
    assert diagnostic == original


def test_web_conformance_accepts_only_the_current_model():
    assert StartHarnessConformanceRequest(codex_model_ref='gpt-6.1-sol', codex_auth_profile_ref='test').codex_model_ref == CODEX_MODEL_REF
    with pytest.raises(ValidationError):
        StartHarnessConformanceRequest(codex_model_ref='gpt-6-sol', codex_auth_profile_ref='test')
