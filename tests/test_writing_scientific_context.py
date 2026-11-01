"""Writing carries scientific fields and exact readers without whole stage bodies."""
from copy import deepcopy
import json
from types import SimpleNamespace

from meta_research.bundle_protocol import BundleReport, projection_plain_value, validate_bundle_report
from meta_research.companion_writing import WritingRequest, writing_message
from meta_research.composition import build_production_runtime
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.paths import prepare_data_root
from meta_research.research_content import read_content
from meta_research.research_overview import ResearchOverviewReader
from test_public_plan_stage import (
    _DeterministicDraftingAdapter, _DeterministicIdeaSkill, _DeterministicPlanSkill,
    _confirm_direct_quest, _finish_idea_stage, _runtime,
)
from test_public_first_question_deepfetch import (
    DeterministicDeepFetchProvider, DeterministicProbe, SnapshotAwareProposalDrafter,
    RecordingAcquisitionProvider,
)
from test_public_reasoning_stage import _confirm_deepfetch_quest, _tick_reasoning
from test_research_overview import CurrentReasoningSkill, reader


def read_exact(runtime, quest_ref, reference):
    assert reference["operation"] == "research_memory.content.read"
    parts, offset = [], 0
    with runtime._database.read_snapshot():
        while True:
            page = read_content(runtime.owners.research_graph, runtime.owners.research_memory,
                quest_ref=quest_ref, **reference["args"], offset=offset, limit=16384)
            parts.append(page["text"])
            offset = page["next_offset"]
            if offset is None:
                return json.loads("".join(parts))


class LargePlan(_DeterministicPlanSkill):
    def _document(self, request):
        document = super()._document(request)
        original = document["answer_contract"]["obligations"][0]
        keys = [f"comparison-{index}" for index in range(40)]
        document["answer_contract"]["obligations"] = [{**deepcopy(original),
            "obligation_key": key, "statement": "Exact research commitment. " * 300,
            "minimum_support": "Exact support condition. " * 300} for key in keys]
        contract = document["answer_contract"]
        contract["answer_contract_hash"] = canonical_hash({
            key: value for key, value in contract.items() if key != "answer_contract_hash"})
        document["coverage"] = [{**deepcopy(document["coverage"][0]),
            "obligation_key": key} for key in keys]
        document["gap_set"] = keys
        document["experiment_briefs"][0]["gap_obligation_keys"] = keys
        document["idea_trace"][0]["obligation_roles"] = [
            {"obligation_key": key, "role": "experiment_lens"} for key in keys]
        return document


def test_large_accepted_plan_queues_writing_and_exact_reader_keeps_full_document(tmp_path):
    runtime = _runtime(tmp_path / "large-plan", idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=LargePlan(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(16):
            if runtime.plan_stage.query_current()["stage_commit"] is not None:
                break
            assert runtime.plan_stage.process_once()
        overview = reader(runtime).query(quest["quest_ref"])
        accepted = overview["cycles"][0]["stages"]["plan"][0]
        assert accepted["status"] == "accepted"
        assert len(canonical_json(accepted["content"])) > 256 * 1024
        human = runtime.owners.human_collaboration
        turn = human.send_companion_message("quest:" + quest["quest_ref"], "Write an outline.",
            "large-plan-writing", writing_request={"requirements": "Write an outline.", "task_scope": "outline"})
        assert turn["assistant_status"] == "queued"
        task, _ = json.JSONDecoder().raw_decode(turn["message"].split("写作任务（普通助手会话）：\n", 1)[1])
        artifact = task["scientific_context"]["scientific_artifacts"][0]
        assert len(turn["message"]) < 256 * 1024
        assert "content" not in artifact
        assert artifact["scientific_fields"]["coverage"] == accepted["content"]["coverage"]
        assert read_exact(runtime, quest["quest_ref"], artifact["reader"]) == accepted["content"]
    finally:
        runtime.close()


class NegativeReasoning(CurrentReasoningSkill):
    def _result_parts(self, request):
        outcome, successor = super()._result_parts(request)
        outcome["disposition"] = "denied"
        outcome["claim"] = "The selected result contradicts the proposed effect."
        outcome["evidence"][0]["finding"] = "negative"
        outcome["limitations"] = ["Only the selected experimental conditions are supported."]
        outcome["uncertainty_basis"] = ["No independent replication is available."]
        # Accepted scientific text has no 256 KiB rejection threshold. Keep a
        # large semantic field whole behind its reader, without slicing prose.
        outcome["causal_interpretation"]["confounders"] = [
            f"Condition {index}: " + "Detailed scientific condition. " * 300 for index in range(40)]
        return outcome, successor


def test_scientific_claim_negative_evidence_and_limits_stay_exact_with_large_deferred_field(tmp_path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "science"),
        proposal_drafter=SnapshotAwareProposalDrafter(), intent_drafting_provider=_DeterministicDraftingAdapter(),
        host_compute_probe=DeterministicProbe(), deepfetch_provider=DeterministicDeepFetchProvider(),
        acquisition_provider=RecordingAcquisitionProvider(), idea_skill_provider=_DeterministicIdeaSkill(no_viable=True),
        plan_skill_provider=_DeterministicPlanSkill(no_gap=False), reasoning_skill_provider=NegativeReasoning())
    try:
        quest = _confirm_deepfetch_quest(runtime)
        _finish_idea_stage(runtime)
        for _ in range(14):
            current = _tick_reasoning(runtime)
            if current["stage_commit"] is not None:
                break
        overview = reader(runtime).query(quest["quest_ref"])
        original = overview["cycles"][0]["stages"]["reasoning"][0]["content"]
        assert len(canonical_json(original)) > 256 * 1024
        context = reader(runtime).writing_context(quest["quest_ref"])
        selected = next(item for item in context["scientific_artifacts"] if item["stage"] == "reasoning")
        for field in ("claim", "disposition", "evidence", "limitations", "uncertainty_basis", "research_synthesis"):
            assert selected["scientific_fields"][field] == original[field]
        assert selected["deferred_fields"] == ["causal_interpretation"]
        assert "成稿前" in context["instruction"]
        assert read_exact(runtime, quest["quest_ref"], selected["reader"]) == original
        turn = runtime.owners.human_collaboration.send_companion_message("quest:" + quest["quest_ref"],
            "Write the supported version.", "large-science-writing", writing_request={"requirements": "Retain the negative result."})
        assert turn["assistant_status"] == "queued"
    finally:
        runtime.close()


def test_large_protocol_valid_bundle_does_not_inline_execution_provenance():
    report = BundleReport(disposition="realized", stage_request_ref="bundle-request", formal_plan_ref="plan",
        accepted_target_commit_refs=("target-commit",), accepted_evaluation_attempt_refs=("evaluation",),
        metric_result_refs=("metric",), execution_attempt_refs=("execution",), execution_fence_refs=("fence",),
        checkpoint_artifact_refs=(), realized_experiment_keys=("experiment",), remaining_experiment_keys=(),
        provenance=tuple((f"target-{index}", tuple(f"source-{index}-{item}" for item in range(400))) for index in range(64)))
    assert len(validate_bundle_report(report)) == 64
    content = projection_plain_value(report)
    assert len(canonical_json(content)) > 256 * 1024
    commit = SimpleNamespace(commit_ref="bundle-commit", receipt=SimpleNamespace(payload_hash="a" * 64))
    artifact = ResearchOverviewReader._writing_artifact({"stage": "bundle", "status": "accepted",
        "source": {"content_ref": "report", "content_hash": canonical_hash(content)}, "content": content}, commit)
    message = writing_message(WritingRequest(requirements="Write the report."), "Write the report.", None,
        science={"scientific_artifacts": [artifact]})
    assert len(message) < 256 * 1024
    assert "provenance" not in message
    assert artifact["scientific_fields"]["metric_result_refs"] == ["metric"]
    assert artifact["reader"]["args"] == {"source_ref": "bundle-commit", "version_ref": "a" * 64}
