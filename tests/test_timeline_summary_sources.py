import json
from contextlib import contextmanager
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import create_engine, text

from meta_research.owners.common import OwnerConflict
from meta_research.timeline_summary_sources import TimelineSummarySourceReader, _public_messages


class Database:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        self.cuts = 0
        with self.engine.begin() as connection:
            connection.execute(text("CREATE TABLE rg_quests (quest_ref TEXT, accepted_at REAL)"))
            connection.execute(text("INSERT INTO rg_quests VALUES ('quest', 1), ('other', 2)"))

    @contextmanager
    def read_snapshot(self):
        self.cuts += 1
        with self.engine.connect() as connection:
            yield connection


def request(cycle="cycle", question="question", stage="bundle", epoch=1):
    return NS(request_ref=f"request-{cycle}-{stage}-{epoch}", cycle_ref=cycle,
              accepted_question=NS(quest_ref="quest", question_ref=question), stage=stage, epoch=epoch)


class Fixture:
    def __init__(self, *, target_count=1):
        self.request = request()
        self.history = [NS(cycle_ref="cycle", question_ref="question", requests=(self.request,), commits=())]
        self.foreground = dict(quest_ref="quest", question_ref="question", cycle_ref="cycle",
                               stage="bundle", epoch=1, status="active", grant_status="active")
        self.graph = NS(quest_ref="quest", cycle_ref="cycle", request_ref=self.request.request_ref,
            graph_ref="graph", formal_plan_ref="plan", targets=tuple(NS(target_ref=f"target-{i}",
                graph_ref="graph", target_key=f"experiment-{i}", ordinal=i, spec_hash=f"spec-{i}") for i in range(target_count)))
        self.context_failures = set()
        self.result_failures = set()
        self.results = {}
        self.context_calls = []
        self.index = {"quest_ref": "quest", "sessions": [], "observed_at": 1, "limited": False}
        self.outputs = {}
        self.output_calls = []
        self.artifact_content = {"finding": "accepted finding"}
        self.artifact_status = "accepted"
        self.question_quest = "quest"
        owners = NS(
            research_graph=NS(query_quest_by_ref=lambda ref: NS(quest_ref=ref) if ref == "quest" else None,
                query_question_history_by_ref=lambda ref: NS(question_ref=ref, quest_ref=self.question_quest,
                    content_ref=f"content-{ref}", content_hash=f"hash-{ref}"),
                query_target_graph=lambda ref: self.graph if ref == self.request.request_ref else None,
                query_target_root_commit_transition=self.transition),
            research_memory=NS(read_question_content=lambda ref, digest: {"title": "Does the intervention help?", "unknown_statement": "The effect is unknown."}),
            advancement_engine=NS(query_quest_stage_history=lambda ref: tuple(self.history),
                query_foreground=lambda ref: self.foreground,
                query_bundle_stage_request=lambda ref: self.request),
            agent_runtime=NS(query_bundle_stage_run=lambda ref: NS(run_ref="bundle-run", request_ref=self.request.request_ref,
                cycle_ref=self.request.cycle_ref, stage=self.request.stage, epoch=self.request.epoch)))
        self.runtime = NS(_database=Database(), owners=owners, data_root=NS(root="unused"),
            target_run_authorities=NS(research_graph=NS(query_target_research_context=self.context)),
            target_run_finalizer=NS(_memory=NS(query=self.manifest)))
        self.reader = TimelineSummarySourceReader(self.runtime)
        self.reader.observations = NS(query=lambda ref: self.index, query_output=self.output)
        self.reader.overview = NS(_artifact=self.artifact)

    def context(self, *, target_ref):
        self.context_calls.append(target_ref)
        if target_ref in self.context_failures:
            raise OwnerConflict("target_research_context_invalid")
        return {"target_ref": target_ref, "question_ref": "question", "formal_plan_ref": "plan",
                "question_content_ref": "question-content", "question_content_hash": "question-hash",
                "plan_document_hash": "plan-hash", "obligations": [{"description": "Estimate effect"}],
                "experiment_briefs": [{"objective": "Measure the intervention against control"}],
                "bundle_notes": "Use a held-out split", "bundle_notes_source_ref": "proposal",
                "bundle_notes_source_hash": "proposal-hash", "selection_boundary": "Only selected experiments"}

    def transition(self, ref):
        if ref in self.result_failures:
            raise OwnerConflict("target_root_commit_issuer_invalid")
        if ref not in self.results:
            return None
        return NS(target_ref=ref, target_run_ref=f"run-{ref}", target_commit_ref=f"commit-{ref}",
                  target_execution_closure_ref=f"completion-{ref}", canonical_terminal=NS(asset_manifest_ref=ref))

    def manifest(self, ref):
        return NS(target_ref=ref, target_run_ref=f"run-{ref}", completion_ref=f"completion-{ref}",
                  manifest_ref=f"manifest-{ref}", result_document_hash=f"result-hash-{ref}",
                  result_document=NS(as_dict=lambda: self.results[ref]), entries=())

    def artifact(self, quest, cycle, req, commit):
        return {"stage": "bundle", "epoch": 1, "status": self.artifact_status,
                "source": {"request_ref": req.request_ref, "outcome_ref": "report"},
                "content": self.artifact_content if self.artifact_status == "accepted" else None,
                "reason": None if self.artifact_status == "accepted" else {"code": "writing_bundle_result_invalid"}}

    def output(self, quest, session, *, operation_ref, after=0, limit=65536):
        self.output_calls.append((operation_ref, after, limit))
        value = self.outputs[operation_ref]
        return dict(quest_ref=quest, session_ref=session, operation_ref=operation_ref,
            text=value, offset=after, source_bytes=len(value.encode()), root_native_session_ref="native-root", exact=True)

    def nodes(self):
        return {node["node_key"]: node for node in self.reader.read("quest")}


def message(body, **fields):
    return {"type": "item.completed", "thread_id": "native-root",
            "item": {"type": "agent_message", "text": body}, **fields}


def session(*, kind="target", target="target-0"):
    return {"session_ref": "root-session", "kind": kind, "target_ref": target,
            "question_ref": "question", "cycle_ref": "cycle", "stage": "bundle", "run_ref": "bundle-run",
            "created_at": 1, "updated_at": 1, "status": "executing", "is_current": True,
            "operations": [{"operation_ref": "operation", "created_at": 1}]}


def test_full_owner_history_not_bounded_root_index_and_failed_bundle_keeps_target_science():
    fixture = Fixture(target_count=513)
    fixture.index.update(limited=True, sessions=[])
    fixture.artifact_status = "unavailable"
    fixture.results["target-0"] = {"result_disposition": "negative", "metrics": {"effect": -0.2}, "finding": "No benefit was observed"}
    fixture.context_failures.add("target-1")
    fixture.result_failures.add("target-1")
    nodes = fixture.nodes()
    assert len([n for n in nodes.values() if n["kind"] == "target"]) == 513
    assert len(fixture.context_calls) == 513
    bundle = nodes["stage:cycle:bundle"]["content"]
    assert bundle["artifacts"][0]["status"] == "unavailable"
    assert bundle["targets"][0]["research_context"]["experiment_briefs"][0]["objective"] == "Measure the intervention against control"
    assert bundle["targets"][0]["accepted_result"]["result_document"]["result_disposition"] == "negative"
    assert bundle["targets"][1]["gaps"] == [{"code": "target_research_context_invalid"}, {"code": "target_root_commit_issuer_invalid"}]
    assert bundle["targets"][2]["research_context"] is not None
    refs = {s["ref"] for s in nodes["cycle:cycle"]["sources"]}
    assert {"plan", "proposal", "commit-target-0", "manifest-target-0"} <= refs


def test_question_is_immutable_meaning_and_revisited_cycles_keep_owner_order():
    fixture = Fixture()
    fixture.history.extend([NS(cycle_ref="cycle-second", question_ref="other-question", requests=(), commits=()),
                            NS(cycle_ref="cycle-third", question_ref="question", requests=(), commits=())])
    before = fixture.reader.read("quest")
    fixture.artifact_content = {"finding": "Later the result changed"}
    after = fixture.reader.read("quest")
    assert [n["cycle_ref"] for n in before if n["kind"] == "cycle"] == ["cycle", "cycle-second", "cycle-third"]
    questions = [n for n in before if n["kind"] == "question"]
    assert len(questions) == 2
    assert questions == [n for n in after if n["kind"] == "question"]
    assert "Later" not in json.dumps(questions)
    assert next(n for n in before if n["kind"] == "cycle")["source_hash"] != next(n for n in after if n["kind"] == "cycle")["source_hash"]


def test_hash_ignores_poll_clock_and_executing_waiting_but_changes_for_public_content_and_pause():
    fixture = Fixture()
    fixture.index["sessions"] = [session()]
    fixture.outputs["operation"] = json.dumps(message("Checking the selected dataset")) + "\n"
    before = fixture.nodes()
    fixture.index["observed_at"] = 999
    fixture.index["sessions"][0].update(updated_at=1000, status="waiting", is_executing=False)
    fixture.index["sessions"][0]["operations"][0].update(updated_at=1000, elapsed_seconds=55)
    assert fixture.nodes() == before
    fixture.outputs["operation"] = json.dumps(message("The dataset passed the schema check")) + "\n"
    after = fixture.nodes()
    for key in ("target:cycle:target-0", "stage:cycle:bundle", "cycle:cycle"):
        assert before[key]["source_hash"] != after[key]["source_hash"]
    assert before["question:question"] == after["question:question"]
    fixture.foreground.update(status="paused", grant_status="suspended")
    paused = fixture.nodes()
    assert paused["target:cycle:target-0"]["content"]["progress"]["control"]["status"] == "paused"
    assert paused["stage:cycle:bundle"]["source_hash"] != after["stage:cycle:bundle"]["source_hash"]


def test_public_text_excludes_other_actors_children_analysis_tools_and_incomplete_records():
    events = [message("old public"), message("child", parent_thread_id="native-root"),
              message("foreign", thread_id="other"), message("hidden", channel="analysis"),
              message("hidden reason", channel="reasoning"),
              {"type": "item.completed", "item": {"type": "command_execution", "output": "tool secret"}},
              message("middle public"), message("new public" + "x" * 2500),
              message("sender conflict", sender_thread_id="other")]
    raw = "\n".join(json.dumps(e) for e in events) + "\n" + json.dumps(message("unfinished"))
    values = _public_messages(dict(text=raw, root_native_session_ref="native-root", offset=0))
    assert len(values) == 2
    assert values[0]["text"] == "middle public"
    assert values[1]["text"].startswith("new public")
    assert len(values[1]["text"]) == 2000
    assert values[1]["truncated"]
    assert _public_messages(dict(text=raw, offset=0)) == []
    conflict = json.dumps({"type": "thread.started", "thread_id": "other"}) + "\n" + raw
    assert _public_messages(dict(text=conflict, root_native_session_ref="native-root", offset=0)) == []


def test_target_conversation_scope_excludes_foreign_and_replaced_roots():
    fixture = Fixture()
    foreign = session()
    foreign.update(question_ref="another-question", created_at=1000)
    replaced = session()
    replaced.update(activity_label="已由新会话接续", created_at=2000)
    fixture.index["sessions"] = [foreign, replaced]
    nodes = fixture.nodes()
    assert fixture.output_calls == []
    assert nodes["target:cycle:target-0"]["content"]["progress"]["public_messages"] == []
    fixture.question_quest = "other"
    nodes = fixture.nodes()
    assert nodes["question:question"]["content"]["question"] is None
    assert nodes["question:question"]["content"]["gaps"] == [{"code": "timeline_summary_question_binding_invalid"}]


def test_unknown_quest_and_foreign_foreground_fail_closed():
    fixture = Fixture()
    assert fixture.reader.quest_refs() == ["quest", "other"]
    with pytest.raises(OwnerConflict, match="timeline_summary_quest_not_found"):
        fixture.reader.read("missing")
    fixture.foreground["quest_ref"] = "other"
    with pytest.raises(OwnerConflict, match="timeline_summary_foreground_binding_invalid"):
        fixture.reader.read("quest")


def test_stage_artifact_io_failure_does_not_hide_question_or_target_science():
    fixture = Fixture()
    def fail(*args):
        raise OSError("private source path must not leak")
    fixture.reader.overview._artifact = fail
    nodes = fixture.nodes()
    assert nodes["stage:cycle:bundle"]["content"]["artifacts"][0]["reason"] == {"code": "timeline_stage_artifact_unavailable"}
    assert nodes["target:cycle:target-0"]["content"]["research_context"] is not None
    assert nodes["question:question"]["content"]["question"] is not None
    assert "private source path" not in json.dumps(nodes)


def test_repeated_operation_output_gaps_do_not_change_research_fingerprint():
    fixture = Fixture()
    fixture.index["sessions"] = [session()]
    # The isolated observer cannot read either operation's public spool.
    before = fixture.nodes()
    fixture.index["sessions"][0]["operations"].append({"operation_ref": "later-operation", "created_at": 2})
    after = fixture.nodes()
    assert after == before
    assert after["target:cycle:target-0"]["content"]["progress"]["gaps"] == [{"code": "timeline_public_messages_unavailable"}]


def test_real_accepted_idea_source_is_exact_and_does_not_advance_owners(tmp_path):
    from test_public_plan_stage import (_DeterministicIdeaSkill, _DeterministicPlanSkill,
        _runtime, _confirm_direct_quest, _finish_idea_stage, _owner_revisions)
    runtime = _runtime(tmp_path / "reader", idea_skill=_DeterministicIdeaSkill(),
                       plan_skill=_DeterministicPlanSkill(no_gap=False))
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        before = _owner_revisions(runtime)
        reader = TimelineSummarySourceReader(runtime)
        nodes = reader.read(quest["quest_ref"])
        assert _owner_revisions(runtime) == before
        idea = next(n for n in nodes if n["kind"] == "stage" and n["stage"] == "idea")
        assert idea["content"]["artifacts"][0]["status"] == "accepted"
        run = runtime.owners.agent_runtime.query_idea_stage_run(idea["content"]["artifacts"][0]["source"]["request_ref"])
        expected = runtime.owners.research_memory.query_idea_outcome_content(run.execution.submission_ref).outcome
        assert idea["content"]["artifacts"][0]["content"] == expected
        assert reader.read(quest["quest_ref"]) == nodes
    finally:
        runtime.close()
