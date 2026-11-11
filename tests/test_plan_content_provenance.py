from types import SimpleNamespace

import pytest
from sqlalchemy import text

from meta_research.owners import research_graph as rg, research_memory as rm
from meta_research.owners.common import OwnerConflict, canonical_hash
from test_public_bundle_stage import (
    _bundle_runtime, _confirm_direct_quest, _finish_idea_stage, _finish_plan_stage,
)


@pytest.fixture
def accepted_plan(tmp_path):
    runtime = _bundle_runtime(tmp_path / "provenance")
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        with runtime._database.read() as connection:
            row = connection.execute(text(
                "SELECT formal_plan_ref,submission_ref FROM rg_formal_plan_decisions "
                "WHERE decision='accepted'"
            )).one()
        owner = runtime.owners.research_graph
        acceptance = owner.accept_formal_plan_content(
            formal_plan_ref=row.formal_plan_ref, idempotency_key="provenance-acceptance"
        )
        yield runtime, row, acceptance
    finally:
        runtime.close()


@pytest.mark.parametrize("field", ["review_hash", "reviewed_draft_hash"])
def test_plan_acceptance_rejects_conflicting_review_provenance(accepted_plan, field):
    runtime, plan, original = accepted_plan
    owner = runtime.owners.research_graph
    assert owner.query_formal_plan_content_acceptance(plan.formal_plan_ref) == original
    with runtime._database.write() as connection:
        decision = SimpleNamespace(**dict(connection.execute(text(
            "SELECT * FROM rg_formal_plan_decisions WHERE formal_plan_ref=:ref"
        ), {"ref": plan.formal_plan_ref}).one()._mapping))
        setattr(decision, field, "0" * 64)
        decision_hash = rg._formal_plan_decision_receipt_hash(decision)
        connection.execute(text(
            f"UPDATE rg_formal_plan_decisions SET {field}=:value,receipt_hash=:digest "
            "WHERE formal_plan_ref=:ref"
        ), {"value": "0" * 64, "digest": decision_hash, "ref": plan.formal_plan_ref})
        acceptance = SimpleNamespace(**dict(connection.execute(text(
            "SELECT * FROM rg_formal_plan_content_acceptances WHERE formal_plan_ref=:ref"
        ), {"ref": plan.formal_plan_ref}).one()._mapping))
        acceptance.formal_plan_receipt_hash = decision_hash
        acceptance_hash = rg._formal_plan_content_acceptance_receipt_hash(acceptance)
        connection.execute(text(
            "UPDATE rg_formal_plan_content_acceptances SET formal_plan_receipt_hash=:decision_hash,"
            "receipt_hash=:digest WHERE formal_plan_ref=:ref"
        ), {"decision_hash": decision_hash, "digest": acceptance_hash, "ref": plan.formal_plan_ref})
    with pytest.raises(OwnerConflict, match="plan_content_receipt_invalid"):
        owner.query_formal_plan_content_acceptance(plan.formal_plan_ref)


def test_plan_acceptance_reuses_one_verified_body(accepted_plan, monkeypatch):
    runtime, plan, expected = accepted_plan
    document = runtime.owners.research_memory.query_plan_document(plan.submission_ref)
    assert document is not None
    counts = {"body_reads": 0, "payload_parses": 0}

    def counted(name, function):
        def read(*args, **kwargs):
            counts[name] += 1
            return function(*args, **kwargs)
        return read

    monkeypatch.setattr(rm, "_read_plan_object", counted("body_reads", rm._read_plan_object))
    monkeypatch.setattr(rm, "_read_verified_plan_payload", counted("payload_parses", rm._read_verified_plan_payload))
    result = runtime.owners.research_graph.query_formal_plan_content_acceptance(plan.formal_plan_ref)
    assert result == expected
    assert result.plan_document_hash == canonical_hash(document.plan_document)
    assert counts == {"body_reads": 1, "payload_parses": 1}
