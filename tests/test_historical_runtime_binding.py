"""Accepted Stage history remains readable after relocating the installation."""

from dataclasses import replace
import json
import shutil
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research import research_guidance
from meta_research.owners.agent_runtime import _runtime_binding_from_row
from meta_research.owners.common import (
    AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json,
)
from meta_research.root_capabilities import CODEX_MODEL_REF, root_capability_profile
from test_public_bundle_stage import (
    _DeterministicBundleSkill, _bundle_runtime, _prepare_bundle_request,
)
from test_public_plan_stage import _DeterministicIdeaSkill, _DeterministicPlanSkill


@pytest.fixture
def relocated_history(tmp_path, monkeypatch):
    for stage, provider_type in (
        ("idea", _DeterministicIdeaSkill),
        ("plan", _DeterministicPlanSkill),
        ("bundle", _DeterministicBundleSkill),
    ):
        original = provider_type.runtime_binding

        def binding(self, original=original, stage=stage):
            value = original(self)
            return replace(value, capability_bindings=(
                *value.capability_bindings,
                *root_capability_profile(stage, CODEX_MODEL_REF).runtime_bindings(),
            ))

        monkeypatch.setattr(provider_type, "runtime_binding", binding)
    runtime = _bundle_runtime(tmp_path / "data")
    try:
        _prepare_bundle_request(runtime)
        assert runtime.bundle_stage.process_once()
        with runtime._database.read() as connection:
            rows = [dict(row) for row in connection.execute(text(
                "SELECT * FROM ar_stage_runs ORDER BY stage"
            )).mappings()]
        assert {row["stage"]: row["status"] for row in rows} == {
            "idea": "completed", "plan": "completed", "bundle": "running",
        }
        old_profiles = {stage: root_capability_profile(stage, CODEX_MODEL_REF).digest
                        for stage in ("idea", "plan", "bundle")}
        relocated = tmp_path / "another-installation" / "meta_research" / "skills"
        shutil.copytree(research_guidance.shared_reference_root(), relocated)
        monkeypatch.setattr(research_guidance, "shared_reference_root", lambda: relocated)
        assert all(root_capability_profile(stage, CODEX_MODEL_REF).digest != digest
                   for stage, digest in old_profiles.items())
        yield runtime, rows
    finally:
        runtime.close()


def test_completed_stages_and_running_bundle_remain_readable_after_relocation(
    relocated_history,
):
    runtime, rows = relocated_history
    for row in rows:
        query = getattr(runtime.owners.agent_runtime, f"query_{row['stage']}_stage_run")
        run = query(row["request_ref"])
        assert run.status == row["status"]
        assert canonical_json(run.runtime_binding.as_dict()) == row["runtime_binding_json"]
        assert run.runtime_binding_hash == row["runtime_binding_hash"]
    # The public detailed snapshot and deferred panels both read accepted
    # ancestors and the active Bundle; hiding history must not be the workaround.
    for include_assets, include_history in ((True, True), (False, False)):
        snapshot = runtime.projection.query_snapshot(
            include_assets=include_assets, include_history=include_history,
        )
        assert not any(item.get("code") == "idea_runtime_binding_unauthorized"
                       for item in snapshot.get("query_warnings", []))
        for stage in ("idea", "plan", "bundle"):
            assert snapshot[f"{stage}_stage"] is not None
    current = runtime.bundle_stage.query_current()
    assert current["stage_run_request"] is not None
    bundle_row = next(row for row in rows if row["stage"] == "bundle")
    run = runtime.owners.agent_runtime.query_bundle_stage_run(bundle_row["request_ref"])
    # Readback is not authorization to execute using an unreviewed new profile.
    assert not runtime.bundle_stage._runtime_binding_is_current(run)
    assert runtime.bundle_stage.observation.code == "bundle_runtime_binding_drift"
    with runtime._database.read() as connection:
        assert [dict(row) for row in connection.execute(text(
            "SELECT * FROM ar_stage_runs ORDER BY stage"
        )).mappings()] == rows


def test_historical_profile_does_not_authorize_new_admission_or_write(relocated_history):
    runtime, rows = relocated_history
    for row in rows:
        binding = _runtime_binding_from_row(SimpleNamespace(**row))
        request = getattr(runtime.owners.advancement_engine,
                          f"query_{row['stage']}_stage_request")(row["cycle_ref"])
        with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
            getattr(runtime.owners.agent_runtime, f"admit_{row['stage']}_stage")(
                request, f"new-admit-{row['stage']}", runtime_binding=binding,
            )
    bundle_row = next(row for row in rows if row["stage"] == "bundle")
    run = runtime.owners.agent_runtime.query_bundle_stage_run(bundle_row["request_ref"])
    with pytest.raises(OwnerConflict, match="idea_runtime_binding_unauthorized"):
        runtime.owners.agent_runtime.record_bundle_primary_draft(
            run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
            native_session_ref="new-native", runtime_binding=run.runtime_binding,
            draft={}, adapter_kind="test", idempotency_key="old-profile-new-write",
        )
    assert runtime.owners.agent_runtime.query_bundle_stage_run(run.request_ref).primary_draft is None


def _with_rehashed_binding(row, value):
    result = dict(row)
    result["runtime_binding_json"] = canonical_json(value)
    result["runtime_binding_hash"] = canonical_hash(value)
    result["admission_hash"] = canonical_hash({
        "command": f"admit_{row['stage']}_stage", "request_ref": row["request_ref"],
        "cycle_ref": row["cycle_ref"], "stage": row["stage"], "epoch": int(row["epoch"]),
        "context_pack_ref": row["context_pack_ref"], "context_pack_hash": row["context_pack_hash"],
        "request_receipt": AcceptanceReceipt(
            issuer="advancement_engine", kind="stage_run_request",
            receipt_ref=row["request_receipt_ref"], subject_ref=row["request_ref"],
            payload_hash=row["request_receipt_hash"],
        ).as_public_dict(),
        "runtime_binding": value, "runtime_binding_hash": result["runtime_binding_hash"],
    })
    return result


def test_historical_readback_preserves_integrity_and_permission_ceiling(relocated_history):
    _runtime, rows = relocated_history
    for row in rows:
        for field in ("runtime_binding_hash", "admission_hash", "context_pack_hash"):
            altered = {**row, field: "f" * 64}
            with pytest.raises(OwnerConflict, match="idea_runtime_binding_invalid"):
                _runtime_binding_from_row(SimpleNamespace(**altered))
        value = json.loads(row["runtime_binding_json"])
        value["model_ref"] = "tampered-model"
        with pytest.raises(OwnerConflict, match="idea_runtime_binding_invalid"):
            _runtime_binding_from_row(SimpleNamespace(
                **{**row, "runtime_binding_json": canonical_json(value)}
            ))
        # Even a self-consistent row must not extend the historic permission floor.
        for field, extra in (
            ("capability_bindings", "owner-admin-unrestricted"),
            ("resource_bindings", "filesystem:/"),
            ("mcp_bindings", "unapproved-owner-admin"),
            ("capability_bindings", "root-capability-profile:sha256:not-a-digest"),
            ("capability_bindings", "root-capability-profile:sha256:" + "f" * 64),
        ):
            value = json.loads(row["runtime_binding_json"])
            value[field].append(extra)
            with pytest.raises(OwnerConflict):
                _runtime_binding_from_row(SimpleNamespace(**_with_rehashed_binding(row, value)))
        for marker in ("root-capability-profile:sha256:" + "g" * 64,
                       "root-capability-profile:sha256:" + "a" * 63):
            value = json.loads(row["runtime_binding_json"])
            value["capability_bindings"] = [
                marker if item.startswith("root-capability-profile:") else item
                for item in value["capability_bindings"]
            ]
            with pytest.raises(OwnerConflict, match="idea_runtime_binding_invalid"):
                _runtime_binding_from_row(SimpleNamespace(**_with_rehashed_binding(row, value)))
        value = json.loads(row["runtime_binding_json"])
        value["capability_bindings"].append(next(
            item for item in value["capability_bindings"]
            if item.startswith("root-capability-profile:")
        ))
        with pytest.raises(OwnerConflict, match="idea_runtime_binding_invalid"):
            _runtime_binding_from_row(SimpleNamespace(**_with_rehashed_binding(row, value)))
