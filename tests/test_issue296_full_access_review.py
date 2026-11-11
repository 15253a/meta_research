"""Exercise Owner review consumption after the real Target Harness boundary.

Native transport/child ledgers are controlled fixtures; admission, reservation,
adapter verification, durable profile/events and Owner child binding are real.
This is evidence-policy coverage, not a native model or semantic review proof.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from meta_research.harness_adapters import CodexHarnessAdapter
from meta_research.owners.common import OwnerConflict
from test_harness_adapters import (
    _RecordedChildLedger,
    _RecordedRunner,
    _child_ledger,
    _codex_result_review_events,
    _codex_review_events,
    _codex_root_preflight_events,
    _result_child_ledger,
    _result_review_prompt,
    _result_review_request,
    _result_review_terminal,
)
from test_harness_full_conformance import _FullConformanceAdapter
from test_issue296_stable_workspace import _activate
from test_target_root_finalizer import (
    _admit_independent_target_root,
    _current_bundle_runtime,
)


class _ReviewAdapter(_FullConformanceAdapter):
    def __init__(self, path: Path, review_kind: str, sandbox_mode: str, child_mode: str):
        super().__init__("codex")
        self.path = path
        self.review_kind = review_kind
        self.sandbox_mode = sandbox_mode
        self.child_mode = child_mode
        self.target_ref = None

    def invoke(self, invocation):
        if invocation.root_kind != "target":
            return super().invoke(invocation)
        root_ref, child_ref = "native-review-root", "native-review-child"
        if self.review_kind == "result":
            request = _result_review_request(
                target_ref=self.target_ref,
                target_run_ref=invocation.run_ref,
            )
            prompt = _result_review_prompt(request)
            payload = json.loads(_result_review_terminal(request))
            payload["review"]["unresolved_findings"] = 0
            terminal = json.dumps(payload)
            events = _codex_result_review_events(
                root_ref=root_ref, child_ref=child_ref,
                prompt=prompt, terminal_message=terminal,
            )
            records = _result_child_ledger(
                root_ref=root_ref, child_ref=child_ref,
                cwd=str(invocation.working_directory), prompt=prompt,
                terminal_message=terminal, sandbox_mode=self.child_mode,
            )
        else:
            binding = {"subject_ref": "subject:review", "content_hash_ref": "a" * 64}
            receipt = {
                "receipt_ref": "receipt:review", "subject_ref": "subject:review",
                "verified": True, "currentness_known": True, "current": True,
            }
            terminal = json.dumps({
                "schema_ref": "meta-research/target-review-evidence/v1",
                "review_kind": "code",
                "review": {
                    "code_changed": True, "disposition": "approved",
                    "candidate_revision_ref": "revision:review",
                    "reviewed_revision_ref": "revision:review",
                    "fixed_base_ref": "base:review", "diff_ref": "diff:review",
                    "review_ref": "review:code",
                    "unresolved_standards_findings": 0,
                    "unresolved_spec_findings": 0,
                },
                "scope": {
                    "candidate_revision_binding": binding,
                    "target_spec_binding": binding,
                    "target_spec_acceptance_receipt": receipt,
                    "formal_plan_binding": binding,
                    "formal_plan_acceptance_receipt": receipt,
                    "experiment_keys": [], "semantic_deltas": [],
                    "held_fixed_bindings": [], "accepted_input_refs": [],
                    "reuse_provenance_refs": [], "repository_standards_refs": [],
                },
            })
            events = _codex_review_events(
                root_ref=root_ref, child_ref=child_ref, terminal_message=terminal,
            )
            events = (events[0], *_codex_root_preflight_events(
                root_ref=root_ref, target_ref=self.target_ref,
                target_run_ref=invocation.run_ref,
                implementation_revision_ref="revision:review",
            ), *events[1:])
            records = _child_ledger(
                root_ref=root_ref, child_ref=child_ref,
                cwd=str(invocation.working_directory), terminal_message=terminal,
                sandbox_mode=self.child_mode,
            )
        # The root lifecycle requires actual workspace shell/file event evidence.
        events = (events[0],
            {"type": "item.completed", "item": {"type": "command_execution"}},
            {"type": "item.completed", "item": {"type": "file_change"}},
            *events[1:])
        runner = _RecordedRunner("codex", events)
        result = CodexHarnessAdapter(
            self.path, runner=runner,
            codex_child_ledger_reader=_RecordedChildLedger(child_ref, records),
        ).invoke(invocation)
        argv = next(argv for argv, _, _ in runner.calls if "--sandbox" in argv)
        assert argv[argv.index("--sandbox") + 1] == self.sandbox_mode
        assert result.profile["subagent_evidence"][0]["child_ledger_lineage"][
            "sandbox_mode"
        ] == self.child_mode
        return result


def _completed_review(tmp_path, monkeypatch, review_kind, sandbox_mode, child_mode=None):
    if sandbox_mode == "workspace-write":
        monkeypatch.delenv("META_RESEARCH_TARGET_SANDBOX_MODE", raising=False)
    else:
        monkeypatch.setenv("META_RESEARCH_TARGET_SANDBOX_MODE", sandbox_mode)
    adapter = _ReviewAdapter(
        tmp_path / "native", review_kind, sandbox_mode, child_mode or sandbox_mode,
    )
    runtime = _current_bundle_runtime(
        tmp_path / "runtime",
        harness_adapters=(adapter,),
    )
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    try:
        admitted = _admit_independent_target_root(runtime)
        _activate(runtime, admitted, "activate-review-policy")
        handle = admitted[-1]
        adapter.target_ref = handle.target_ref
        harness = runtime.owners.agent_runtime.harness_runs
        reservation = harness.reserve_target_child_session(
            target_run_ref=handle.target_run_ref, review_kind=review_kind,
        )
        runtime.harnesses.run_or_resume_target_root(
            admitted[-2].run.request_ref,
            prompt="Complete this reserved independent review.",
            mcp_base_url="http://127.0.0.1:8999",
        )
        profile = harness.query_profile(handle.target_run_ref)
        assert profile["sandbox_mode"] == sandbox_mode
        evidence = profile["subagent_evidence"][0]
        assert evidence["provider_operation_ref"] == reservation.harness_operation_ref
        return runtime, handle, reservation, evidence
    except BaseException:
        runtime.close()
        raise


@pytest.mark.parametrize("review_kind,sandbox_mode,child_mode", [
    ("code", "workspace-write", "workspace-write"),
    ("result", "workspace-write", "workspace-write"),
    ("code", "danger-full-access", "danger-full-access"),
    ("result", "danger-full-access", "danger-full-access"),
    ("code", "danger-full-access", "workspace-write"),
    ("result", "workspace-write", "read-only"),
])
def test_owner_consumes_review_under_configured_target_policy(
    tmp_path, monkeypatch, review_kind, sandbox_mode, child_mode,
):
    runtime, handle, reservation, evidence = _completed_review(
        tmp_path, monkeypatch, review_kind, sandbox_mode, child_mode,
    )
    try:
        owner = runtime.target_run_authorities.agent_runtime
        turn = owner.query_target_review_turn_evidence(
            handle=handle, harness_operation_ref=reservation.harness_operation_ref,
            review_kind=review_kind,
        )
        assert turn is not None
        # Discovery is read-only; acceptance's common verifier owns child binding.
        assert runtime.owners.agent_runtime.harness_runs.query_target_child_session(
            reservation.harness_operation_ref
        ).status == "reserved"
        profile = runtime.owners.agent_runtime.harness_runs.query_profile(handle.target_run_ref)
        # Rechecking the trusted projection still rejects a missing policy or
        # corrupted exact native lineage. No mutated projection is persisted.
        mutations = [
            {"sandbox_mode": None}, {"sandbox_mode": "unknown-policy"},
            {"cwd": "/wrong-workspace"}, {"parent_session_id": "wrong-parent"},
        ]
        if sandbox_mode == "workspace-write":
            mutations.append({"sandbox_mode": "danger-full-access"})
        if review_kind == "code":
            mutations.append({"sandbox_mode": "read-only"})
        invalid_projections = []
        for mutation in mutations:
            invalid = deepcopy(evidence)
            invalid["child_ledger_lineage"].update(mutation)
            invalid_projections.append(invalid)
        invalid_actor = deepcopy(evidence)
        invalid_actor[
            "skill_actor_session_ref" if review_kind == "code" else "review_actor_session_ref"
        ] = "wrong-native-reviewer"
        invalid_projections.append(invalid_actor)
        for invalid in invalid_projections:
            with pytest.raises(OwnerConflict, match=(
                "target_code_review_skill_evidence_invalid" if review_kind == "code"
                else "target_result_review_native_evidence_invalid"
            )):
                if review_kind == "code":
                    owner._verify_code_review_skill_invocation(
                        handle=handle, operation_ref=reservation.harness_operation_ref,
                        profile=profile, evidence=invalid,
                    )
                else:
                    owner._verify_result_review_native_child(
                        handle=handle, operation_ref=reservation.harness_operation_ref,
                        profile=profile, evidence=invalid,
                        expected_review=evidence["payload"]["review"],
                    )
        accepted = owner._review_evidence(
            handle=handle, operation_ref=reservation.harness_operation_ref,
            review_kind=review_kind, expected_review=evidence["payload"]["review"],
            expected_scope=evidence["payload"].get("scope"),
        )
        assert accepted["domain_child_session_ref"] == reservation.child_session_ref
        bound = runtime.owners.agent_runtime.harness_runs.query_target_child_session(
            reservation.harness_operation_ref
        )
        assert bound.status == "bound"
        assert bound.native_child_session_ref == "native-review-child"
        assert asdict(turn.review)["reviewer_session_ref"] == bound.child_session_ref
    finally:
        runtime.close()


@pytest.mark.parametrize("review_kind", ["code", "result"])
def test_unconfigured_owner_rejects_full_access_review_without_binding(
    tmp_path, monkeypatch, review_kind,
):
    runtime, handle, reservation, evidence = _completed_review(
        tmp_path, monkeypatch, review_kind, "danger-full-access",
    )
    try:
        monkeypatch.delenv("META_RESEARCH_TARGET_SANDBOX_MODE")
        owner = runtime.target_run_authorities.agent_runtime
        with pytest.raises(OwnerConflict, match="target_review_harness_invalid"):
            owner.query_target_review_turn_evidence(
                handle=handle, harness_operation_ref=reservation.harness_operation_ref,
                review_kind=review_kind,
            )
        with pytest.raises(OwnerConflict, match="target_review_harness_invalid"):
            owner._review_evidence(
                handle=handle, operation_ref=reservation.harness_operation_ref,
                review_kind=review_kind,
                expected_review=evidence["payload"]["review"],
                expected_scope=evidence["payload"].get("scope"),
            )
        assert runtime.owners.agent_runtime.harness_runs.query_target_child_session(
            reservation.harness_operation_ref
        ).status == "reserved"
    finally:
        runtime.close()
