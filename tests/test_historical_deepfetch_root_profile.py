"""Historical DeepFetch receipts retain their original instruction identity."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from meta_research import root_capabilities
from meta_research.deepfetch import DeepFetchUnavailable, validate_runtime_binding
from meta_research.owners.agent_runtime import (
    DEEPFETCH_EXECUTION_RECEIPT_KIND,
    SQLiteAgentRuntimeReceiptVerifier,
    _deepfetch_runtime_binding,
    _deepfetch_run_from_row,
)
from meta_research.owners.common import (
    AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json,
)


FIXTURE = Path(__file__).parent / "fixtures/root_prompt_20260929/historical-deepfetch.json"


@pytest.fixture
def stored():
    # Actual executed 8768 initialization DeepFetch; result body is intentionally
    # absent because the receipt query binds its hash without loading that body.
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def receipt_arguments(stored):
    return {
        "request_ref": stored["request_ref"],
        "run_ref": stored["run_ref"],
        "attempt_ref": stored["joined_attempt_ref"],
        "fence_ref": stored["fence_ref"],
        "result_hash": stored["result_hash"],
        "receipt": AcceptanceReceipt(
            issuer="agent_runtime", kind=DEEPFETCH_EXECUTION_RECEIPT_KIND,
            receipt_ref=stored["execution_receipt_ref"],
            subject_ref=stored["run_ref"], payload_hash=stored["execution_receipt_hash"],
        ),
    }


def verifier_for(stored):
    connection = SimpleNamespace(execute=lambda *args, **kwargs: SimpleNamespace(
        first=lambda: SimpleNamespace(**stored),
    ))

    @contextmanager
    def read():
        yield connection

    verifier = object.__new__(SQLiteAgentRuntimeReceiptVerifier)
    verifier._database = SimpleNamespace(read=read)
    return verifier


def test_actual_receipt_reads_with_original_binding_and_hash(stored):
    original = deepcopy(stored)
    binding = _deepfetch_runtime_binding(
        stored["runtime_binding_json"], allow_historical_root_profile=True,
    )
    assert canonical_json(binding.as_dict()) == stored["runtime_binding_json"]
    assert canonical_hash(binding.as_dict()) == stored["runtime_binding_hash"]
    verifier_for(stored).verify_deepfetch_execution_receipt(**receipt_arguments(stored))
    assert stored == original


def test_historical_read_never_authorizes_fresh_admission_execution_or_recovery(stored):
    binding = _deepfetch_runtime_binding(
        stored["runtime_binding_json"], allow_historical_root_profile=True,
    )
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_runtime_capability_unavailable"):
        validate_runtime_binding(binding)
    with pytest.raises(OwnerConflict, match="deepfetch_runtime_binding_invalid"):
        _deepfetch_runtime_binding(stored["runtime_binding_json"])


@pytest.mark.parametrize("profile_fixture", ("before.json", "after.json"))
def test_both_reviewed_profiles_are_readable_but_never_executable(stored, profile_fixture):
    # Construct a parser input for each reviewed profile without altering the
    # real stored receipt fixture or claiming a new signed historical record.
    profile = json.loads((FIXTURE.parent / profile_fixture).read_text(encoding="utf-8"))
    value = json.loads(stored["runtime_binding_json"])
    value["capability_bindings"] = [
        "root-capability-profile:sha256:" + profile["root_profile_hash"]
        if item.startswith("root-capability-profile:sha256:") else item
        for item in value["capability_bindings"]
    ]
    serialized = canonical_json(value)
    binding = _deepfetch_runtime_binding(serialized, allow_historical_root_profile=True)
    assert canonical_json(binding.as_dict()) == serialized
    with pytest.raises(DeepFetchUnavailable, match="deepfetch_runtime_capability_unavailable"):
        validate_runtime_binding(binding)
    with pytest.raises(OwnerConflict, match="deepfetch_runtime_binding_invalid"):
        _deepfetch_runtime_binding(serialized)


@pytest.mark.parametrize("status", ("admitted", "running", "failed"))
def test_unfinished_history_does_not_opt_into_completed_receipt_compatibility(stored, status):
    stored["status"] = status
    with pytest.raises(OwnerConflict, match="deepfetch_runtime_binding_invalid"):
        _deepfetch_run_from_row(SimpleNamespace(**stored))


@pytest.mark.parametrize("field", ("CODEX_MODEL_REF", "CODEX_REASONING_EFFORT", "RESEARCH_SYSTEM_PROMPT"))
def test_future_profile_cannot_inherit_the_reviewed_history_exception(stored, monkeypatch, field):
    monkeypatch.setattr(root_capabilities, field, getattr(root_capabilities, field) + "-unreviewed")
    with pytest.raises(OwnerConflict, match="deepfetch_runtime_binding_invalid"):
        verifier_for(stored).verify_deepfetch_execution_receipt(**receipt_arguments(stored))


@pytest.mark.parametrize("change", ("unknown_profile", "missing_shell", "missing_web", "duplicate_profile"))
def test_history_keeps_profile_and_capability_constraints(stored, change):
    value = json.loads(stored["runtime_binding_json"])
    capabilities = value["capability_bindings"]
    profile = next(item for item in capabilities if item.startswith("root-capability-profile:"))
    if change == "unknown_profile":
        capabilities[capabilities.index(profile)] = "root-capability-profile:sha256:" + "f" * 64
    elif change == "duplicate_profile":
        capabilities.append(profile)
    else:
        capabilities.remove("shell-tool-enabled" if change == "missing_shell" else "web-search-live")
    with pytest.raises(OwnerConflict, match="deepfetch_runtime_binding_invalid"):
        _deepfetch_runtime_binding(canonical_json(value), allow_historical_root_profile=True)


@pytest.mark.parametrize("field", (
    "runtime_binding_hash", "attempt_runtime_binding_hash", "attempt_runtime_binding_json",
    "attempt_native_session_ref", "result_hash", "attempt_result_hash",
    "execution_receipt_hash", "provider_operation_ref", "attempt_status",
))
def test_historical_receipt_still_rejects_tampering(stored, field):
    arguments = receipt_arguments(stored)
    stored[field] = "unreviewed"
    with pytest.raises(OwnerConflict, match="deepfetch_execution_receipt_invalid"):
        verifier_for(stored).verify_deepfetch_execution_receipt(**arguments)
