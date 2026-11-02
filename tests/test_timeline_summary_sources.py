import json
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import text, event

from meta_research.database import Database
from meta_research.owners.common import OwnerConflict
from meta_research.timeline_summary_sources import TimelineSummarySourceReader, _public_messages


class Fixture:
    """Seed exactly the same identity/revision columns the scanner reads."""

    def __init__(self, path, target_count=3):
        self.db = Database(path)
        self.queries = []
        self.body_reads = []
        self.results = {}
        self.public = ""
        self.read_sizes = []
        self.change_ns = 1
        self.sessions = []
        self.current = {
            "quest_ref": "quest",
            "cycle_ref": "active",
            "question_ref": "q",
            "stage": "bundle",
            "status": "active",
            "grant_status": "active",
            "epoch": 1,
        }
        with self.db.write() as db:
            for table, columns in {
                "rg_quests": "quest_ref, accepted_at",
                "ae_cycles": "cycle_ref, question_ref, quest_ref, created_at",
                "ae_stage_commits": "cycle_ref, stage, epoch, commit_ref, receipt_hash, request_ref, outcome_ref, disposition",
                "ae_stage_run_requests": "cycle_ref, question_ref, stage, request_ref, quest_ref, epoch",
                "ar_stage_runs": "cycle_ref, stage, epoch, run_ref",
                "ar_bundle_reports": "request_ref, report_ref, report_hash, ordinal, run_ref",
                "rg_targets": "target_ref, spec_hash, graph_ref, ordinal, append_ref",
                "rg_target_graphs": "graph_ref, quest_ref, cycle_ref, plan_document_hash, request_ref, submission_ref, target_plan_hash",
                "rg_target_root_measurements": "target_ref,target_run_ref,manifest_ref,completion_ref",
                "rg_target_graph_appends": "append_ref, graph_ref, receipt_hash, generation, proposal_hash, proposal_ref",
                "rg_target_commits": "target_ref, commit_ref,target_run_ref",
                "rm_target_root_completion_manifests": "target_ref,target_run_ref,manifest_ref,result_document_hash,completion_ref",
                "rm_target_research_notes": "target_ref,version_ref,note_hash,accepted_at",
                **{
                    k: "question_ref,quest_ref,content_ref,content_hash"
                    for k in ("rg_questions", "rg_autonomous_questions", "rg_manual_questions")
                },
            }.items():
                db.execute(text(f"CREATE TABLE {table} ({columns})"))
            db.execute(text("INSERT INTO rg_quests VALUES ('quest',1),('other',2)"))
            db.execute(
                text(
                    "INSERT INTO rg_questions VALUES ('q','quest','question-content','hash-q'),('past-q','quest','past-content','old')"
                )
            )
            db.execute(text("INSERT INTO ae_cycles VALUES ('past','past-q','quest',1),('active','q','quest',2)"))
            db.execute(
                text(
                    "INSERT INTO rg_target_graphs VALUES ('graph','quest','active','plan-hash','request','notes-source','notes-hash')"
                )
            )
            db.execute(
                text("INSERT INTO rg_targets VALUES (:t,'spec','graph',:ordinal,NULL)"),
                [{"t": f"t{x}", "ordinal": x} for x in range(target_count)],
            )
        event.listen(
            self.db._engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, params, context, many: self.queries.append(statement),
        )

        def context(*, target_ref):
            self.body_reads.append(target_ref)
            return dict(
                target_ref=target_ref,
                question_ref="q",
                plan_document_hash="plan-hash",
                question="Estimate effect",
                formal_plan_ref="plan",
                obligations=[],
                experiment_briefs=[{"objective": "Estimate effect"}],
                plan_notes="",
                stage_request_ref="request",
                graph_ref="graph",
                bundle_notes_source_ref="notes-source",
                bundle_notes_source_hash="notes-hash",
                bundle_notes="",
                selection_boundary="Only the selected treatment",
            )

        def transition(t):
            if t not in self.results:
                return None
            return NS(
                target_ref=t,
                target_commit_ref="commit",
                target_run_ref="run",
                target_execution_closure_ref="completion",
                canonical_terminal=NS(asset_manifest_ref="manifest"),
            )

        def manifest(_):
            self.body_reads.append("result")
            body = self.results["t0"]
            if body is None:
                return None
            return NS(
                target_ref="t0",
                target_run_ref="run",
                completion_ref="completion",
                manifest_ref="manifest",
                result_document_hash="result-hash",
                result_document=NS(as_dict=lambda: body),
                entries=(),
            )

        owners = NS(
            research_graph=NS(query_target_root_commit_transition=transition),
            advancement_engine=NS(query_foreground=lambda _: self.current),
            research_memory=NS(),
            agent_runtime=NS(),
        )
        runtime = NS(
            _database=self.db,
            owners=owners,
            data_root=NS(root=path.parent),
            target_run_authorities=NS(research_graph=NS(query_target_research_context=context)),
            target_run_finalizer=NS(_memory=NS(query=manifest)),
        )
        self.reader = TimelineSummarySourceReader(runtime)
        self.reader.observations = NS(research_sessions=lambda q, f: self.sessions, query_output=self.output)

    def output(self, quest, session, *, operation_ref, after=0, limit=65536):
        self.read_sizes.append(limit)
        raw = self.public.encode()
        return dict(
            quest_ref=quest,
            session_ref=session,
            operation_ref=operation_ref,
            text=raw[after : after + limit].decode(),
            offset=after,
            next_offset=min(len(raw), after + limit),
            root_native_session_ref="native",
            source_bytes=len(raw),
            source_change_ns=self.change_ns,
            stream_ref="stream",
            exact=True,
        )

    def nodes(self):
        return {n["node_key"]: n for n in self.reader.read("quest")}

    def commit(self, result):
        self.results["t0"] = result
        with self.db.write() as db:
            db.execute(text("INSERT INTO rg_target_commits VALUES ('t0','commit','run')"))
            db.execute(text("INSERT INTO rg_target_root_measurements VALUES ('t0','run','manifest','completion')"))
            db.execute(
                text(
                    "INSERT INTO rm_target_root_completion_manifests VALUES ('t0','run','manifest','result-hash','completion')"
                )
            )


def test_one_changed_target_updates_only_its_ancestors_without_reading_any_history_body(tmp_path):
    f = Fixture(tmp_path / "source.sqlite", target_count=513)
    before = f.nodes()
    f.queries.clear()
    assert f.nodes() == before
    query_count = len(f.queries)
    assert f.body_reads == []
    f.commit({"estimate": 0, "finding": "no effect", "missing_measurement": None})
    f.queries.clear()
    after = f.nodes()
    changed = {k for k in before if before[k]["source_hash"] != after[k]["source_hash"]}
    assert changed == {"target:active:t0", "stage:active:bundle", "cycle:active", "question:q"}
    assert f.body_reads == []
    assert len(f.queries) == query_count
    print(f"513 targets; stable scan and one change: {query_count} statements, 0 research bodies")
    # Only a Cycle is in the job, with no child row, yet its exact inputs can be opened separately.
    cycle = after["cycle:active"]
    assert f.reader.read_source("quest", cycle, "commit")["result_document"]["estimate"] == 0
    assert f.body_reads == ["result"]
    assert "Estimate effect" in str(f.reader.read_source("quest", cycle, "t0"))
    assert f.body_reads == ["result", "t0"]
    with pytest.raises(OwnerConflict, match="not_granted"):
        f.reader.read_source("quest", before["cycle:active"], "commit")
    with pytest.raises(OwnerConflict, match="not_granted"):
        f.reader.read_source("other", cycle, "t0")


def test_missing_body_can_be_retried_without_changing_the_cut_and_is_not_an_empty_stage(tmp_path):
    f = Fixture(tmp_path / "source.sqlite")
    f.commit(None)
    frozen = f.nodes()
    with pytest.raises(OwnerConflict, match="target_result_binding_invalid"):
        f.reader.read_source("quest", frozen["cycle:active"], "commit")
    f.results["t0"] = {
        "result_disposition": "negative",
        "measured_zero": 0,
        "not_measured": None,
        "limitation": "underpowered",
    }
    assert frozen == f.nodes()
    assert f.reader.read_source("quest", frozen["cycle:active"], "commit")["result_document"] == f.results["t0"]
    # Read the captured Stage, even after a later Foreground has replaced it.
    f.current.update(stage="plan")
    empty = f.reader.read_source("quest", frozen["stage:active:plan"], "stage:active:plan")
    assert empty["artifacts"] == [] and empty["current"] is None
    assert (
        f.reader.read_source("quest", f.nodes()["stage:active:plan"], "stage:active:plan")["current"]["stage"]
        == "plan"
    )
    assert f.reader.read_source("quest", frozen["cycle:active"], "stage:active:bundle")["current"]["stage"] == "bundle"


def message(body, **fields):
    return {"type": "item.completed", "thread_id": "native", "item": {"type": "agent_message", "text": body}, **fields}


def test_verified_public_output_retains_full_limitations_and_excludes_private_or_partial_records(tmp_path):
    f = Fixture(tmp_path / "source.sqlite")
    f.sessions = [(("target", "t0"), "root", "waiting", ["op"])]
    explanation = "Checking the baseline. " + "x" * 195000 + " A 0 effect is within measurement error."

    def lines(records):
        return "".join(json.dumps(r) + "\n" for r in records)

    prefix = lines(
        [
            message("child", parent_thread_id="native"),
            message("foreign", thread_id="wrong"),
            message("analysis", channel="analysis"),
            message("hidden", visibility="hidden"),
            {"type": "item.completed", "item": {"type": "command_execution", "output": "secret"}},
        ]
    )
    f.public = prefix + lines([message(explanation)]) + json.dumps(message("unfinished"))
    frozen = f.nodes()["cycle:active"]
    observed = f.reader.read_source("quest", frozen, "op")["public_messages"]
    assert [m["text"] for m in observed] == [explanation]
    f.read_sizes.clear()
    f.nodes()
    assert f.read_sizes == [4]  # only the active public transport revision
    f.public += "\n"
    f.change_ns += 1
    assert f.reader.read_source("quest", frozen, "op")["public_messages"] == observed
    assert (
        f.reader.read_source("quest", f.nodes()["cycle:active"], "op")["public_messages"][-1]["text"] == "unfinished"
    )
    f.public = f.public.replace("Checking", "Spoofing")
    f.change_ns += 1  # same inode, size and mtime; a corrected line has a new ctime
    with pytest.raises(OwnerConflict, match="drift"):
        f.reader.read_source("quest", frozen, "op")
    changed = f.nodes()["cycle:active"]
    assert f.reader.read_source("quest", changed, "op")["public_messages"][0]["text"].startswith("Spoofing")
    assert _public_messages({"text": prefix}) == []


def test_foreign_target_cannot_grant_public_output(tmp_path):
    f = Fixture(tmp_path / "source.sqlite")
    f.sessions = [(("target", "other-target"), "foreign", "waiting", ["op"])]
    with pytest.raises(OwnerConflict, match="not_granted"):
        f.reader.read_source("quest", f.nodes()["cycle:active"], "op")
    f.current["quest_ref"] = "other"
    with pytest.raises(OwnerConflict, match="foreground_binding_invalid"):
        f.reader.read("quest")
    with pytest.raises(OwnerConflict, match="quest_not_found"):
        f.reader.read("missing")


def test_real_accepted_idea_source_is_exact_and_does_not_advance_owners(tmp_path):
    from test_public_plan_stage import (
        _DeterministicIdeaSkill,
        _DeterministicPlanSkill,
        _runtime,
        _confirm_direct_quest,
        _finish_idea_stage,
        _owner_revisions,
    )

    runtime = _runtime(
        tmp_path / "reader", idea_skill=_DeterministicIdeaSkill(), plan_skill=_DeterministicPlanSkill(no_gap=False)
    )
    try:
        quest = _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        before = _owner_revisions(runtime)
        reader = TimelineSummarySourceReader(runtime)
        nodes = reader.read(quest["quest_ref"])
        assert _owner_revisions(runtime) == before
        idea = next(n for n in nodes if n["kind"] == "stage" and n["stage"] == "idea")
        assert "content" not in idea
        outcome_ref = next(s["ref"] for s in idea["sources"] if s["label"] == "idea outcome")
        artifact = reader.read_source(quest["quest_ref"], idea, outcome_ref)
        assert artifact["status"] == "accepted"
        run = runtime.owners.agent_runtime.query_idea_stage_run(artifact["source"]["request_ref"])
        expected = runtime.owners.research_memory.query_idea_outcome_content(run.execution.submission_ref).outcome
        assert artifact["content"] == expected
        assert reader.read(quest["quest_ref"]) == nodes
    finally:
        runtime.close()
