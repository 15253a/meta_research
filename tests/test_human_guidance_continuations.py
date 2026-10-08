import json

import pytest
from sqlalchemy import text

from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.harness import HarnessAdmissionError
from meta_research.human_guidance import GUIDANCE_OPERATION_IDS
from test_human_guidance_providers import (
    _adapter, _BundleTransport, _ReasoningTransport, _reasoning_runtime,
    _bundle_runtime, _submit, _seals, _tool,
)
from test_human_guidance_inboxes import _InboxIdea, _run_idea, _finish
from test_public_bundle_stage import _accept_real_target_root_commit, _ParallelTwoTargetBundleSkill
from test_target_root_finalizer import (
    _CurrentBindingBundleSkill, _current_bundle_runtime, _admit_independent_target_root,
)
from test_public_autonomous_creation import (
    _AutonomousReasoningSkill, _reach_autonomous_checkpoint,
    _drive_autonomous_creation_ready, _finish_reasoning_after_creation,
)


def _configure(runtime):
    runtime.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.bundle_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")
    runtime.reasoning_stage.configure_resident_mcp_endpoint("http://127.0.0.1:8999")


def test_actual_bundle_target_batch_has_its_own_cut_on_the_same_rolling_job(tmp_path):
    provider, runner = _adapter(_BundleTransport, tmp_path)
    runtime = _bundle_runtime(tmp_path / "runtime", bundle_skill_provider=provider)
    runner.runtime = runtime
    _configure(runtime)
    try:
        target, _ = _accept_real_target_root_commit(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(target.target_ref)
        _submit(runtime, launch.quest_ref)
        for _ in range(12):
            if any(item["operation_name"].startswith("target-batch-") for item in _seals(provider)):
                break
            assert runtime.bundle_stage.process_once(), runtime.bundle_stage.transient_error
        seals = _seals(provider)
        batch = next(item for item in seals if item["operation_name"] == "target-batch-1")
        dispatch = next(item for item in seals if item["operation_name"] == "dispatch-1")
        assert batch["job_ref"] == dispatch["job_ref"]
        assert batch["guidance_binding"]["snapshot_ref"] != dispatch["guidance_binding"]["snapshot_ref"]
        assert runner.inboxes[-1]["deliveries"][0]["needs_treatment"] is True
        assert len(runner.receipts) == 1
    finally:
        runtime.close()


class _AutonomousTransport(_ReasoningTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.delegate = _AutonomousReasoningSkill()

    def generate_draft(self, request):
        self.call(request, "primary")
        return self.delegate.generate_draft(request)

    def review_draft(self, request, draft):
        self.call(request, "review")
        return self.delegate.review_draft(request, draft)

    def decide_after_deepfetch(self, request, checkpoint, facts, summary):
        self.call(request, "autonomous-resume")
        return self.delegate.decide_after_deepfetch(request, checkpoint, facts, summary)

    def resume_after_autonomous_creation(self, request, checkpoint, creation_result):
        self.call(request, "autonomous-resume")
        return self.delegate.resume_after_autonomous_creation(request, checkpoint, creation_result)


def test_actual_reasoning_summary_and_creation_resumes_keep_distinct_exact_jobs(tmp_path):
    provider, runner = _adapter(_AutonomousTransport, tmp_path)
    runtime = _reasoning_runtime(tmp_path / "runtime", reasoning_skill=provider)
    runner.runtime = runtime
    _configure(runtime)
    try:
        quest, _, checkpoint = _reach_autonomous_checkpoint(runtime)
        _submit(runtime, quest["quest_ref"])
        _drive_autonomous_creation_ready(runtime, checkpoint, key="guidance-autonomous")
        _finish_reasoning_after_creation(runtime)
        resumed = [item for item in _seals(provider) if item["operation_name"] == "autonomous-resume"]
        assert len(resumed) == 2
        assert len({item["job_ref"] for item in resumed}) == 2
        assert len({item["guidance_binding"]["snapshot_ref"] for item in resumed}) == 2
        assert len({item["guidance_binding"]["identity"]["run_ref"] for item in resumed}) == 1
        assert len(runner.receipts) == 1
        assert runner.inboxes[-2]["deliveries"][0]["needs_treatment"] is True
        assert runner.inboxes[-1]["deliveries"][0]["needs_treatment"] is False
    finally:
        runtime.close()


def test_two_active_target_roots_treat_independently_and_cannot_read_each_others_delivery(tmp_path):
    class ParallelBundle(_CurrentBindingBundleSkill, _ParallelTwoTargetBundleSkill):
        pass

    runtime = _current_bundle_runtime(tmp_path / "runtime", bundle_skill=ParallelBundle())
    try:
        first_target, _, _, first, _ = _admit_independent_target_root(runtime)
        launch = runtime.owners.agent_runtime.query_admitted_target_launch(first_target.target_ref)
        guide = _submit(runtime, launch.quest_ref)
        for _ in range(12):
            runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            run = runtime.owners.agent_runtime.query_bundle_stage_run(current["stage_run_request"]["request_ref"])
            decisions = runtime.owners.agent_runtime.query_bundle_dispatch_decisions(run.run_ref)
            second_decision = next((item for item in decisions if item.selected_target_ref != first_target.target_ref), None)
            if second_decision is not None:
                graph = runtime.owners.research_graph.query_target_graph(run.request_ref)
                second_target = next(item for item in graph.targets if item.target_ref == second_decision.selected_target_ref)
                if runtime.owners.research_graph.query_target_candidate_projection(target_ref=second_target.target_ref) is not None:
                    break
        else:
            raise AssertionError("Second independent Target did not become dispatchable")
        _, _, _, second, _ = _admit_independent_target_root(runtime,
            ready=(graph, second_target, run, second_decision, None), key_prefix="second-")
        adapter = runtime.harnesses._adapters["codex"]
        original_invoke = adapter.invoke
        observed = []
        first_delivery = None

        def invoke(invocation):
            nonlocal first_delivery
            inbox = _tool(runtime, invocation.mcp_token, "human_guidance.read")
            delivery, = inbox["deliveries"]
            assert delivery["needs_treatment"] is True
            observed.append(inbox)
            if invocation.run_ref == first.run.run_ref:
                first_delivery = delivery["delivery_ref"]
                runtime.harnesses.run_or_resume_target_root(second.run.request_ref,
                    prompt="Audit the second independent Target.", mcp_base_url="http://127.0.0.1:8999")
            else:
                assert runtime.owners.agent_runtime.harness_runs.query_run(first.run.request_ref).status == "running"
                assert runtime.owners.agent_runtime.harness_runs.query_run(second.run.request_ref).status == "running"
                status, denied = runtime.harnesses.dispatch_mcp(invocation.mcp_token, {
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                        "name": "human_guidance.read", "arguments": {
                            "delivery_ref": first_delivery, "effect_id": "foreign-read"}}})
                assert status == 200
                assert denied["result"]["structuredContent"]["code"] == "guidance_delivery_unbound"
            _tool(runtime, invocation.mcp_token, "human_guidance.read",
                delivery_ref=delivery["delivery_ref"], effect_id="read")
            _tool(runtime, invocation.mcp_token, "human_guidance.feedback",
                delivery_ref=delivery["delivery_ref"], effect_id="feedback", understanding="Audit devices independently.",
                changes="Separate device evidence.", continuing_work="Continue this Target.", reasons="Device drift matters.", disposition="applied")
            return original_invoke(invocation)

        adapter.invoke = invoke
        runtime.harnesses.run_or_resume_target_root(first.run.request_ref,
            prompt="Audit the first independent Target.", mcp_base_url="http://127.0.0.1:8999")
        assert len(observed) == 2
        assert len({item["binding"]["identity"]["run_ref"] for item in observed}) == 2
        rows = runtime.owners.human_collaboration.query_guidance_deliveries(guide["constraint_ref"])
        assert len(rows) == 2
        assert all(row["treatment"]["declared_by_root"] for row in rows)
    finally:
        runtime.close()


def test_large_legacy_guidance_keeps_original_binding_and_requires_full_utf8_coverage(tmp_path):
    original = "  " + "\u7a00\u6709\u5f62\u6001" * 8000 + "\nKeep the exact legacy text.  "
    document = {"text": original}
    observed = {}

    def primary(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        delivery, = cut.deliveries
        assert hc.read_operation_guidance(scope=operation.scope, binding=cut.binding)["deliveries"][0]["strength"] == 3
        offset = 0
        pages = []
        while True:
            page = hc.read_operation_guidance(scope=operation.scope, binding=cut.binding,
                delivery_ref=delivery.delivery_ref, effect_id="page-" + str(offset), offset=offset, limit=4096)
            assert len(page["text"].encode("utf-8")) <= 4096
            pages.append(page["text"])
            if page["next_offset"] is None:
                assert page["full_read"] is True
                break
            assert page["full_read"] is False
            offset = page["next_offset"]
        assert json.loads("".join(pages)) == {"text": original}
        receipt = hc.feedback_operation_guidance(scope=operation.scope, binding=cut.binding,
            delivery_ref=delivery.delivery_ref, effect_id="feedback", understanding="Keep exact legacy guidance.",
            changes="Read all pages.", continuing_work="Continue the audit.", reasons="Partial text is insufficient.", disposition="considered")
        assert receipt["goal_update_pending"] is False
        observed["read"] = True

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, quest = _run_idea(tmp_path, provider)
    try:
        hc = runtime.owners.human_collaboration
        guide = hc.submit_human_guidance(quest_ref=quest, original_text="Legacy seed.", idempotency_key="legacy-seed")
        guidance_hash = canonical_hash(document)
        receipt_hash = canonical_hash({"schema_ref": "meta-research/soft-constraint-receipt/v1",
            "issuer": "human_collaboration", "constraint_ref": guide["constraint_ref"],
            "scope_ref": guide["scope_ref"], "revision": 1, "guidance_hash": guidance_hash})
        with runtime._database.fenced_write() as connection:
            connection.execute(text("UPDATE hc_soft_constraints SET guidance_json=:document, guidance_hash=:hash,receipt_hash=:receipt WHERE constraint_ref=:ref"),
                {"document": canonical_json(document), "hash": guidance_hash, "receipt": receipt_hash, "ref": guide["constraint_ref"]})
        before, = hc.query_active_guidance_bindings(guide["scope_ref"])
        _finish(runtime)
        assert observed == {"read": True}
        after, = hc.query_active_guidance_bindings(guide["scope_ref"])
        assert after == before
        assert after["guidance"] == {"text": original}
    finally:
        runtime.close()


def test_review_child_cannot_receive_guidance_read_or_feedback_authority(tmp_path):
    observed = []

    def primary(hc, operation):
        cut = hc.freeze_operation_guidance(operation)
        scope = operation.scope
        for operation_id in GUIDANCE_OPERATION_IDS:
            with pytest.raises(HarnessAdmissionError, match="mcp_channel_scope_invalid"):
                provider.runtime.harnesses.issue_resident_mcp_channel(
                    run_ref=scope.run_ref, attempt_ref=scope.attempt_ref,
                    root_session_ref=scope.root_session_ref, fence_ref=scope.fence_ref,
                    capability_binding_hash=scope.runtime_binding_hash,
                    operation_ids=(operation_id,), root_kind="idea",
                    phase="primary", subject_policy="review_tree",
                    guidance_binding=cut.binding,
                )
            observed.append(operation_id)

    provider = _InboxIdea(primary, lambda *_: None)
    runtime, _ = _run_idea(tmp_path, provider)
    try:
        _finish(runtime)
        assert tuple(observed) == GUIDANCE_OPERATION_IDS
    finally:
        runtime.close()
