"""Execute the research-context SQL, including the appended Target branch."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.target_run_runtime import SQLiteTargetRunGraphAuthority


@pytest.fixture
def context_owner():
    engine = create_engine("sqlite://")
    plan = {
        "question_ref": "question-1",
        "experiment_briefs": [{"experiment_key": "audit", "gap_obligation_keys": ["split"]}],
        "answer_contract": {"obligations": [{"obligation_key": "split"}]},
        "notes": "Plan reasoning",
    }
    context = {
        "accepted_formal_plan_binding": {"formal_plan_ref": "plan-1", "plan_document": plan},
        "accepted_question_binding": {
            "question_ref": "question-1", "quest_ref": "quest-1",
            "content_ref": "question-content-1", "content_hash": "a" * 64,
        },
    }
    spec = {"candidate": {"experiment_keys": ["audit"]}}
    proposal = {"strategy_update": {"notes": "Accepted audit handoff"}}
    later_proposal = {"strategy_update": {"notes": "Later unrelated handoff"}}
    # The append table owns the immutable accepted relation; proposal content
    # belongs to AR. In particular, the append table has no proposal_json column.
    with engine.begin() as connection:
        for statement in (
            "CREATE TABLE rg_targets (target_ref TEXT PRIMARY KEY, graph_ref TEXT, spec_json TEXT, spec_hash TEXT, append_ref TEXT, dependency_refs_json TEXT)",
            "CREATE TABLE rg_target_graphs (graph_ref TEXT PRIMARY KEY, request_ref TEXT)",
            "CREATE TABLE ae_stage_run_requests (request_ref TEXT PRIMARY KEY, context_pack_json TEXT, context_pack_hash TEXT)",
            "CREATE TABLE rg_target_graph_appends (append_ref TEXT PRIMARY KEY, graph_ref TEXT, proposal_ref TEXT)",
            "CREATE TABLE ar_bundle_target_proposals (proposal_ref TEXT PRIMARY KEY, proposal_json TEXT)",
        ):
            connection.execute(text(statement))
        connection.execute(text("INSERT INTO rg_targets VALUES ('target-1', 'graph-1', :spec, :hash, 'append-1', '[]')"),
            {"spec": canonical_json(spec), "hash": canonical_hash(spec)})
        connection.execute(text("INSERT INTO rg_target_graphs VALUES ('graph-1', 'request-1')"))
        connection.execute(text("INSERT INTO ae_stage_run_requests VALUES ('request-1', :context, :hash)"),
            {"context": canonical_json(context), "hash": canonical_hash(context)})
        connection.execute(text("INSERT INTO rg_target_graph_appends VALUES ('append-1', 'graph-1', 'proposal-1')"))
        connection.execute(text("INSERT INTO ar_bundle_target_proposals VALUES (:ref, :proposal)"), [
            {"ref": "proposal-1", "proposal": canonical_json(proposal)},
            {"ref": "proposal-later", "proposal": canonical_json(later_proposal)},
        ])

    class Database:
        @contextmanager
        def read(self):
            with engine.connect() as connection:
                yield connection

    owner = object.__new__(SQLiteTargetRunGraphAuthority)
    owner._database = Database()
    owner._memory = SimpleNamespace(read_question_content=lambda *_: {"unknown_statement": "Is the split valid?"})
    graph = SimpleNamespace(
        quest_ref="quest-1", submission_ref="initial-submission", target_plan_hash="b" * 64,
        target_plan={"initial_strategy_update": {"notes": "Initial handoff"}},
    )
    source = SimpleNamespace(formal_plan_ref="plan-1", plan_document_hash=canonical_hash(plan))
    owner._current_formal_plan_facts = lambda _: (graph, source, None, ())
    yield owner, engine, proposal
    engine.dispose()


def test_appended_context_reads_exact_accepted_proposal_content(context_owner):
    owner, _engine, proposal = context_owner
    result = owner.query_target_research_context(target_ref="target-1")
    assert result["bundle_notes"] == "Accepted audit handoff"
    assert result["bundle_notes_source_ref"] == "proposal-1"
    assert result["bundle_notes_source_hash"] == canonical_hash(proposal)
    assert result["experiment_briefs"][0]["experiment_key"] == "audit"
    assert result["obligations"] == [{"obligation_key": "split"}]


def test_initial_context_retains_initial_notes(context_owner):
    owner, engine, _proposal = context_owner
    with engine.begin() as connection:
        connection.execute(text("UPDATE rg_targets SET append_ref = NULL"))
    result = owner.query_target_research_context(target_ref="target-1")
    assert result["bundle_notes"] == "Initial handoff"
    assert result["bundle_notes_source_ref"] == "initial-submission"
    assert result["bundle_notes_source_hash"] == "b" * 64


def test_appended_context_without_notes_retains_initial_notes(context_owner):
    owner, engine, _proposal = context_owner
    with engine.begin() as connection:
        connection.execute(text("UPDATE ar_bundle_target_proposals SET proposal_json = '{}' WHERE proposal_ref = 'proposal-1'"))
    result = owner.query_target_research_context(target_ref="target-1")
    assert result["bundle_notes"] == "Initial handoff"
    assert result["bundle_notes_source_ref"] == "initial-submission"


@pytest.mark.parametrize("damage", [
    "DELETE FROM ar_bundle_target_proposals WHERE proposal_ref = 'proposal-1'",
    "UPDATE rg_target_graph_appends SET graph_ref = 'other-graph'",
    "DELETE FROM rg_target_graph_appends WHERE append_ref = 'append-1'",
])
def test_appended_context_requires_the_accepted_graph_proposal_relation(context_owner, damage):
    owner, engine, _proposal = context_owner
    with engine.begin() as connection:
        connection.execute(text(damage))
    with pytest.raises(OwnerConflict, match="target_research_context_invalid"):
        owner.query_target_research_context(target_ref="target-1")


def test_context_still_rejects_changed_frozen_spec(context_owner):
    owner, engine, _proposal = context_owner
    with engine.begin() as connection:
        connection.execute(text("UPDATE rg_targets SET spec_hash = :hash"), {"hash": "0" * 64})
    with pytest.raises(OwnerConflict, match="target_research_context_invalid"):
        owner.query_target_research_context(target_ref="target-1")
