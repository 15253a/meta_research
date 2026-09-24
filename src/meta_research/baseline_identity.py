"""Stable method names and versions, independent of Target execution bindings.

Legacy byte-addressed Baselines stay immutable and remain explicitly selectable.
New method versions use an agent-selected name/version; their content hash checks
consistency and is only a discovery hint between independently named methods.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref

METHOD_SCHEMA = "meta-research/baseline-method-version/v1"
_IDENTITY_KEYS = {"baseline_ref", "method_key", "method_version", "method_contract"}
_ENVELOPE_KEYS = _IDENTITY_KEYS | {"schema_ref", "run_bindings", "notes", "research_note_ref"}

# Domain corrections only: integrity, receipt and execution-scope faults must
# continue to fail closed instead of becoming a revisable research proposal.
BASELINE_METHOD_REJECTION_FEEDBACK = {
    "baseline_method_identity_required": "Select an existing baseline_ref, or declare method_key, method_version and a nonempty method_contract. An unseen free-form contract cannot register a new method.",
    "baseline_method_envelope_invalid": "Keep stable semantics inside method_contract; place execution bindings and research notes in their separate fields. Select a baseline_ref or provide a complete named method version.",
    "baseline_reference_invalid": "Supply one exact nonempty baseline_ref from research_graph.baselines.page/read.",
    "baseline_reference_not_found": "The selected baseline_ref does not exist. Search research_graph.baselines.page/read and explicitly select a registered method, or register a named method version.",
    "baseline_method_key_required": "A new method requires a stable, nonempty method_key together with method_version and method_contract.",
    "baseline_method_version_required": "Declare a nonempty method_version for the named method, or select its existing baseline_ref.",
    "baseline_method_contract_required": "Declare a nonempty method_contract containing the stable method meaning and input/output semantics, or select an existing baseline_ref.",
    "baseline_method_version_conflict": "The selected baseline_ref does not match the supplied method name and version. Read the exact registered method and correct the selection.",
    "baseline_method_version_content_conflict": "The named method version already has different immutable semantics. Reuse its exact contract or explicitly declare a new method version when the input/output meaning changes.",
}


def _ref(value, code):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > 1024 or any(char in value for char in ("\x00", "\r", "\n"))):
        raise OwnerConflict(code)
    return value


def _method_envelope(forward):
    # Historical free-form contracts already used method_key without an explicit
    # version envelope. Preserve that original byte-addressed meaning on replay.
    if not {"baseline_ref", "method_version", "method_contract"}.intersection(forward):
        return None
    if set(forward) - _ENVELOPE_KEYS:
        raise OwnerConflict("baseline_method_envelope_invalid")
    if "baseline_ref" in forward:
        _ref(forward["baseline_ref"], "baseline_reference_invalid")
    named = "method_key" in forward or "method_version" in forward
    if named:
        _ref(forward.get("method_key"), "baseline_method_key_required")
        _ref(forward.get("method_version"), "baseline_method_version_required")
    if "baseline_ref" not in forward and not named:
        raise OwnerConflict("baseline_method_identity_required")
    if "method_contract" in forward:
        if not isinstance(forward["method_contract"], dict) or not forward["method_contract"]:
            raise OwnerConflict("baseline_method_contract_required")
    elif "baseline_ref" not in forward:
        raise OwnerConflict("baseline_method_contract_required")
    return forward


def _stored_method(forward):
    return {
        "schema_ref": METHOD_SCHEMA,
        "method_key": forward["method_key"],
        "method_version": forward["method_version"],
        "method_contract": forward["method_contract"],
    }


def verify_baseline_method_identity(forward, baseline_row, method_row=None):
    """Verify immutable native bytes and the selected identity independently."""
    if baseline_row is None:
        raise OwnerConflict("baseline_reference_not_found")
    try:
        stored = json.loads(baseline_row.forward_contract_json)
    except (ValueError, TypeError) as error:
        raise OwnerConflict("target_measurement_native_identity_integrity_invalid") from error
    if (not isinstance(stored, dict) or not stored
            or baseline_row.forward_contract_json != canonical_json(stored)
            or baseline_row.forward_contract_hash != canonical_hash(stored)):
        raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
    if method_row is not None:
        try:
            semantic = json.loads(method_row.method_contract_json)
        except (ValueError, TypeError) as error:
            raise OwnerConflict("baseline_method_version_integrity_invalid") from error
        expected = _stored_method({"method_key": method_row.method_key,
            "method_version": method_row.method_version, "method_contract": semantic})
        if (not isinstance(semantic, dict) or not semantic
                or method_row.baseline_ref != baseline_row.baseline_ref
                or method_row.method_contract_json != canonical_json(semantic)
                or method_row.method_contract_hash != canonical_hash(semantic)
                or stored != expected):
            raise OwnerConflict("baseline_method_version_integrity_invalid")
    elif isinstance(stored, dict) and stored.get("schema_ref") == METHOD_SCHEMA:
        raise OwnerConflict("baseline_method_version_integrity_invalid")
    if method_row is None and forward == stored:
        return
    envelope = _method_envelope(forward)
    if envelope is None:
        # The exact original contract remains authoritative for historical rows.
        if stored != forward:
            raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
        return
    if "baseline_ref" in forward and forward["baseline_ref"] != baseline_row.baseline_ref:
        raise OwnerConflict("baseline_reference_invalid")
    if "method_key" in forward and (method_row is None
            or forward["method_key"] != method_row.method_key
            or forward["method_version"] != method_row.method_version):
        raise OwnerConflict("baseline_method_version_conflict")
    if "method_contract" in forward:
        semantic = stored if method_row is None else json.loads(method_row.method_contract_json)
        if semantic != forward["method_contract"]:
            raise OwnerConflict("baseline_method_version_content_conflict")


def resolve_baseline_method_identity(connection, *, forward, quest_ref, accepted_at):
    """Register or select a Baseline inside the caller's RG transaction."""
    exact = connection.execute(text(
        "SELECT * FROM rg_experiment_baselines WHERE forward_contract_hash = :hash"),
        {"hash": canonical_hash(forward)}).first()
    if exact is not None:
        method = connection.execute(text(
            "SELECT * FROM rg_baseline_method_versions WHERE baseline_ref = :ref"),
            {"ref": exact.baseline_ref}).first()
        verify_baseline_method_identity(forward, exact, method)
        return exact.baseline_ref, False
    envelope = _method_envelope(forward)
    if envelope is None:
        # Only an already registered, byte-identical historical contract may use
        # legacy identity. Every new method must declare its identity explicitly.
        raise OwnerConflict("baseline_method_identity_required")
    method_row = None
    if "baseline_ref" in envelope:
        baseline_row = connection.execute(text(
            "SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref"),
            {"ref": envelope["baseline_ref"]}).first()
        method_row = connection.execute(text(
            "SELECT * FROM rg_baseline_method_versions WHERE baseline_ref = :ref"),
            {"ref": envelope["baseline_ref"]}).first()
        verify_baseline_method_identity(forward, baseline_row, method_row)
        return baseline_row.baseline_ref, False
    method_row = connection.execute(text(
        "SELECT * FROM rg_baseline_method_versions WHERE method_key = :method_key "
        "AND method_version = :method_version"), envelope).first()
    if method_row is not None:
        baseline_row = connection.execute(text(
            "SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref"),
            {"ref": method_row.baseline_ref}).one()
        verify_baseline_method_identity(forward, baseline_row, method_row)
        return baseline_row.baseline_ref, False
    stored = _stored_method(envelope)
    digest = canonical_hash(stored)
    existing = connection.execute(text(
        "SELECT * FROM rg_experiment_baselines WHERE forward_contract_hash = :hash"),
        {"hash": digest}).first()
    if existing is not None:
        verify_baseline_method_identity(forward, existing, method_row)
        return existing.baseline_ref, False
    baseline_ref = new_ref("baseline")
    connection.execute(text(
        "INSERT INTO rg_experiment_baselines (baseline_ref, quest_ref, forward_contract_json, "
        "forward_contract_hash, accepted_at) VALUES (:ref, :quest, :document, :hash, :at)"),
        {"ref": baseline_ref, "quest": quest_ref, "document": canonical_json(stored), "hash": digest, "at": accepted_at})
    semantic = envelope["method_contract"]
    connection.execute(text(
        "INSERT INTO rg_baseline_method_versions (method_key, method_version, baseline_ref, "
        "method_contract_json, method_contract_hash, accepted_at) VALUES "
        "(:key, :version, :ref, :document, :hash, :at)"),
        {"key": envelope["method_key"], "version": envelope["method_version"], "ref": baseline_ref,
         "document": canonical_json(semantic), "hash": canonical_hash(semantic), "at": accepted_at})
    return baseline_ref, True


class BaselineIdentityQueries:
    """Read-only discovery; selected refs still undergo graph admission checks."""

    def query_baseline(self, baseline_ref, *, quest_ref=None):
        _ref(baseline_ref, "baseline_reference_invalid")
        with self._database.read_snapshot() as connection:
            row = connection.execute(text("SELECT * FROM rg_experiment_baselines WHERE baseline_ref = :ref"),
                {"ref": baseline_ref}).first()
            if row is None or (quest_ref is not None and row.quest_ref != quest_ref):
                return None
            method = connection.execute(text("SELECT * FROM rg_baseline_method_versions WHERE baseline_ref = :ref"),
                {"ref": baseline_ref}).first()
        verify_baseline_method_identity({"baseline_ref": baseline_ref}, row, method)
        contract = json.loads(row.forward_contract_json if method is None else method.method_contract_json)
        return {
            "baseline_ref": baseline_ref,
            "method_key": contract.get("method_key") if method is None else method.method_key,
            "method_version": None if method is None else method.method_version,
            "method_contract": contract,
            "method_contract_hash": row.forward_contract_hash if method is None else method.method_contract_hash,
            "identity_kind": "legacy_exact_contract" if method is None else "explicit_method_version",
            "selection": {"baseline_ref": baseline_ref},
        }

    def query_baseline_variants(self, baseline_ref, *, variant_ref=None, limit=20, offset=0, quest_ref=None):
        """Page recipes, or read one selected recipe with a page of evaluations."""
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise OwnerConflict("baseline_query_invalid")
        if variant_ref is not None:
            _ref(variant_ref, "variant_reference_invalid")
        with self._database.read_snapshot() as connection:
            if self.query_baseline(baseline_ref, quest_ref=quest_ref) is None:
                raise OwnerConflict("baseline_reference_not_found")
            rows = connection.execute(text(
                "SELECT * FROM rg_experiment_variants WHERE baseline_ref = :baseline "
                "AND (:variant IS NULL OR variant_ref = :variant) "
                "ORDER BY accepted_at, variant_ref LIMIT :limit OFFSET :offset"),
                {"baseline": baseline_ref, "variant": variant_ref,
                 "limit": limit + 1, "offset": offset if variant_ref is None else 0}).all()
            if variant_ref is not None and not rows:
                raise OwnerConflict("variant_reference_invalid")
            variants = []
            for row in rows[:limit]:
                recipe = json.loads(row.recipe_json)
                if row.recipe_hash != canonical_hash(recipe) or row.recipe_json != canonical_json(recipe):
                    raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
                item = {"variant_ref": row.variant_ref, "baseline_ref": baseline_ref,
                    "recipe_hash": row.recipe_hash, "recipe_summary": canonical_json(recipe)[:600]}
                if variant_ref is not None:
                    item["recipe"] = recipe
                    runs = connection.execute(text(
                        "SELECT variant_run_ref FROM rg_variant_runs WHERE variant_ref=:variant "
                        "ORDER BY created_at DESC,variant_run_ref LIMIT :limit OFFSET :offset"),
                        {"variant":variant_ref,"limit":limit+1,"offset":offset}).scalars().all()
                    item["runs"] = [{"variant_run_ref":ref,"reader":{"operation":"research_graph.formal_results.read","ref":ref}}
                                    for ref in runs[:limit]]
                    item["next_run_offset"] = offset+limit if len(runs)>limit else None
                    evaluations = connection.execute(text(
                        "SELECT e.evaluation_ref, p.* FROM rg_evaluations e "
                        "JOIN rg_protocol_versions p ON p.protocol_version_ref = e.protocol_version_ref "
                        "WHERE e.variant_ref = :variant ORDER BY e.accepted_at, e.evaluation_ref "
                        "LIMIT :limit OFFSET :offset"),
                        {"variant": variant_ref, "limit": limit + 1, "offset": offset}).all()
                    evaluated = []
                    for evaluation in evaluations[:limit]:
                        protocol = json.loads(evaluation.protocol_json)
                        metrics = json.loads(evaluation.required_metrics_json)
                        if (evaluation.protocol_hash != canonical_hash(protocol)
                                or evaluation.protocol_json != canonical_json(protocol)
                                or evaluation.required_metrics_hash != canonical_hash(metrics)):
                            raise OwnerConflict("target_measurement_native_identity_integrity_invalid")
                        evaluated.append({"evaluation_ref": evaluation.evaluation_ref,
                            "protocol_version_ref": evaluation.protocol_version_ref,
                            "evaluation_protocol_ref": evaluation.evaluation_protocol_ref,
                            "protocol_hash": evaluation.protocol_hash, "protocol": protocol,
                            "required_metric_keys": metrics})
                    item["evaluations"] = evaluated
                    item["next_evaluation_offset"] = offset + limit if len(evaluations) > limit else None
                variants.append(item)
        return {"items": variants,
            "next_offset": offset + limit if variant_ref is None and len(rows) > limit else None}

    def query_baselines(self, *, query="", method_contract_hash=None, limit=20, offset=0, quest_ref=None):
        if (not isinstance(query, str) or len(query) > 1024 or type(limit) is not int
                or not 1 <= limit <= 100 or type(offset) is not int or offset < 0):
            raise OwnerConflict("baseline_query_invalid")
        if method_contract_hash is not None and (not isinstance(method_contract_hash, str)
                or len(method_contract_hash) != 64 or any(c not in "0123456789abcdef" for c in method_contract_hash)):
            raise OwnerConflict("baseline_query_invalid")
        where = "WHERE (:quest IS NULL OR b.quest_ref=:quest) AND (:query = '' OR instr(lower(b.forward_contract_json), lower(:query)) > 0) "
        if method_contract_hash is not None:
            where += "AND COALESCE(m.method_contract_hash, b.forward_contract_hash) = :hash "
        with self._database.read_snapshot() as connection:
            refs = connection.execute(text(
                "SELECT b.baseline_ref FROM rg_experiment_baselines b LEFT JOIN rg_baseline_method_versions m "
                "ON m.baseline_ref = b.baseline_ref " + where +
                "ORDER BY b.accepted_at DESC, b.baseline_ref LIMIT :limit OFFSET :offset"),
                {"quest": quest_ref, "query": query, "hash": method_contract_hash, "limit": limit + 1, "offset": offset}).scalars().all()
            items = []
            for ref in refs[:limit]:
                item = self.query_baseline(ref)
                contract = item.pop("method_contract")
                item["summary"] = canonical_json(contract)[:600]
                item["match_kind"] = "exact_content_candidate" if method_contract_hash else "text_candidate"
                items.append(item)
        return {"items": items, "next_offset": offset + limit if len(refs) > limit else None,
                "selection_required": True, "automatic_merge": False}
