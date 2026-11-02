"""Cross-Quest navigation descriptors execute without reconstructing their scope."""
import json

from test_baseline_historical_discovery import completed_work
from test_dataset_effect_scope_recovery import _scope
from test_public_manual_question_lifecycle import _build_runtime, _accept_root_question
from test_root_workspace import _channel, _call, _accepted, _external_context


def follow(runtime, channel, descriptor):
    operation = descriptor.get("operation", "research_memory.content.read")
    arguments = descriptor.get("args", {key: value for key, value in descriptor.items() if key != "operation"})
    return _accepted(_call(runtime, channel, operation, **arguments))


def test_baseline_and_stage_history_return_directly_callable_cross_quest_readers(completed_work):
    runtime, graph, quest, baseline, variant = completed_work
    channel = _channel(runtime, _external_context(_scope(runtime, quest_ref="other-selected-quest")))
    page = _accepted(_call(runtime, channel, "research_graph.baselines.read", baseline_ref=baseline,
        relation="variants", source_quest_ref=quest))
    selected = follow(runtime, channel, page["result"]["reusable_entities"]["items"][0]["reader"])
    run_reader = selected["result"]["reusable_entities"]["items"][0]["runs"][0]["reader"]
    assert follow(runtime, channel, run_reader)["status"] == "accepted"
    artifacts = _accepted(_call(runtime, channel, "research_graph.baselines.read", baseline_ref=baseline,
        relation="artifacts", source_quest_ref=quest))
    follow(runtime, channel, artifacts["result"]["reusable_entities"]["items"][0]["reader"])
    question = runtime.owners.advancement_engine.query_foreground(quest)["question_ref"]
    history = _accepted(_call(runtime, channel, "research_graph.question_history.read",
        question_ref=question, source_quest_ref=quest))
    observed = 0
    for cycle in history["items"]:
        for stage in cycle["stages"]:
            for key in ("reader", "basis_reader"):
                if key in stage:
                    follow(runtime, channel, stage[key])
                    observed += 1
    assert observed >= 2
    cycle = runtime.owners.advancement_engine.query_quest_stage_history_display(quest)[0]
    commit = next(item for item in cycle.commits if item.stage == "idea")
    envelope = _accepted(_call(runtime, channel, "research_memory.content.read", source_quest_ref=quest,
        source_ref=commit.commit_ref, version_ref=commit.receipt.payload_hash, limit=16384))
    follow(runtime, channel, json.loads(envelope["text"])["content_reader"])


def test_human_input_and_guidance_readers_preserve_source_quest(tmp_path):
    runtime = _build_runtime(tmp_path / "runtime")
    try:
        current, _ = _accept_root_question(runtime, "current")
        other, _ = _accept_root_question(runtime, "other")
        human = runtime.owners.human_collaboration
        human.submit_research_input(quest_ref=other, text_content="A specific negative observation.", idempotency_key="input")
        human.submit_human_guidance(quest_ref=other, original_text="Retain the negative result.", idempotency_key="guide")
        channel = _channel(runtime, _external_context(_scope(runtime, quest_ref=current)))
        directory = _accepted(_call(runtime, channel, "human_request.read", source_quest_ref=other))
        summary = json.loads(directory["text"])
        original = follow(runtime, channel, summary["research_inputs"]["items"][0]["reader"])
        assert json.loads(original["text"])["text"] == "A specific negative observation."
        guidance = follow(runtime, channel, summary["formal_guidance_reader"])
        exact = follow(runtime, channel, json.loads(guidance["text"])["items"][0]["reader"])
        assert "Retain the negative result." in exact["text"]
    finally:
        runtime.close()
