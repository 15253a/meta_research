"""Seed legacy RG receipts directly; never restore retired production writers."""
from types import SimpleNamespace
from sqlalchemy import text
from meta_research.owners.common import canonical_hash, canonical_json
import meta_research.owners.research_graph as graph_module


def seed_legacy_rejection(database, decision, *, scientific=False):
    table = "rg_reasoning_scientific_decisions" if scientific else "rg_reasoning_outcome_decisions"
    feedback = ["Historical scientific reassessment was requested."]
    with database.write() as connection:
        row = connection.execute(text(f"SELECT * FROM {table} WHERE decision_ref = :ref"), {"ref": decision.decision_ref}).one()
        values = dict(row._mapping)
        values.update(decision="rejected", outcome_ref=None,
                      reason_code="historical_scientific_reassessment", feedback_json=canonical_json(feedback),
                      feedback_hash=canonical_hash(feedback))
        hasher = graph_module._reasoning_scientific_decision_receipt_hash if scientific else graph_module._reasoning_decision_receipt_hash
        values["receipt_hash"] = hasher(SimpleNamespace(**values))
        connection.execute(text(f"UPDATE {table} SET decision = :decision, outcome_ref = NULL, reason_code = :reason_code, feedback_json = :feedback_json, feedback_hash = :feedback_hash, receipt_hash = :receipt_hash WHERE decision_ref = :decision_ref"), values)
        accepted = "reasoning_scientific_outcome_count" if scientific else "reasoning_outcome_count"
        rejected = "reasoning_scientific_rejection_count" if scientific else "reasoning_rejection_count"
        connection.execute(text(f"UPDATE research_graph_state SET {accepted} = {accepted} - 1, {rejected} = {rejected} + 1 WHERE singleton = 'owner'"))
