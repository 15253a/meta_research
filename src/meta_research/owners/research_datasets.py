"""RG's global Dataset identities, semantic versions and research references.

This mixin only uses the owning RG's database, feed and public issuer verifiers.
It never stores data bytes, creates RM versions, or changes experiment identity.
"""
from __future__ import annotations

import json
import time
from typing import Protocol

from sqlalchemy import text

from meta_research.dataset_contract import (
    DATASET_SCHEMA, DATASET_VERSION_SCHEMA, DATASET_REFERENCE_SCHEMA, DATASET_DERIVATION_SCHEMA,
    dataset_text, dataset_metadata, dataset_asset_binding,
)
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json, new_ref


_FACTS = {
    "register": ("rg_datasets", "dataset_ref", DATASET_SCHEMA),
    "register_version": ("rg_dataset_versions", "dataset_version_ref", DATASET_VERSION_SCHEMA),
    "reference": ("rg_dataset_references", "dataset_reference_ref", DATASET_REFERENCE_SCHEMA),
    "derive": ("rg_dataset_derivations", "dataset_derivation_ref", DATASET_DERIVATION_SCHEMA),
}
_INDEXED_FIELDS = {
    "register": ("semantic_key",),
    "register_version": ("dataset_ref", "version_label"),
    "reference": ("dataset_version_ref", "question_ref"),
    "derive": ("source_dataset_version_ref", "derived_dataset_version_ref", "question_ref"),
}
_RESEARCH_REF_QUEST_QUERY = """
    SELECT quest_ref FROM rg_experiment_baselines WHERE baseline_ref = :ref
    UNION ALL
    SELECT b.quest_ref FROM rg_experiment_variants v
        JOIN rg_experiment_baselines b ON b.baseline_ref = v.baseline_ref
        WHERE v.variant_ref = :ref
    UNION ALL
    SELECT b.quest_ref FROM rg_variant_runs r
        JOIN rg_experiment_variants v ON v.variant_ref = r.variant_ref
        JOIN rg_experiment_baselines b ON b.baseline_ref = v.baseline_ref
        WHERE r.variant_run_ref = :ref
    UNION ALL
    SELECT b.quest_ref FROM rg_evaluations e
        JOIN rg_experiment_variants v ON v.variant_ref = e.variant_ref
        JOIN rg_experiment_baselines b ON b.baseline_ref = v.baseline_ref
        WHERE e.evaluation_ref = :ref
    UNION ALL
    SELECT b.quest_ref FROM rg_evaluation_attempts a
        JOIN rg_evaluations e ON e.evaluation_ref = a.evaluation_ref
        JOIN rg_variant_runs r ON r.variant_run_ref = a.variant_run_ref
            AND r.variant_ref = e.variant_ref
        JOIN rg_experiment_variants v ON v.variant_ref = e.variant_ref
        JOIN rg_experiment_baselines b ON b.baseline_ref = v.baseline_ref
        WHERE a.evaluation_attempt_ref = :ref
    UNION ALL
    SELECT g.quest_ref FROM rg_targets t
        JOIN rg_target_graphs g ON g.graph_ref = t.graph_ref
        WHERE t.target_ref = :ref
    UNION ALL
    SELECT g.quest_ref FROM rg_target_commits c
        JOIN rg_targets t ON t.target_ref = c.target_ref
        JOIN rg_target_graphs g ON g.graph_ref = t.graph_ref
        WHERE c.commit_ref = :ref
"""


class ResearchDatasetOwnerInterface(Protocol):
    def register_dataset(self, **values) -> dict[str, object]: ...
    def register_dataset_version(self, **values) -> dict[str, object]: ...
    def reference_dataset(self, **values) -> dict[str, object]: ...
    def derive_dataset(self, **values) -> dict[str, object]: ...
    def query_dataset(self, dataset_ref: str) -> dict[str, object] | None: ...
    def query_dataset_version(self, dataset_version_ref: str) -> dict[str, object] | None: ...
    def query_dataset_reference(self, dataset_reference_ref: str) -> dict[str, object] | None: ...
    def query_dataset_derivation(self, dataset_derivation_ref: str) -> dict[str, object] | None: ...
    def query_datasets(self, **values) -> dict[str, object]: ...
    def reconcile_dataset_operation(self, *, operation: str, idempotency_key: str) -> dict[str, object] | None: ...
    def query_dataset_asset_references(self, asset_version_ref: str) -> tuple[str, ...]: ...
    def verify_dataset_research_ref_scope(self, research_ref: str, *, quest_ref: str) -> None: ...


class ResearchDatasetOwnerMixin:
    def register_dataset(self, *, semantic_key, name, meaning, metadata=None, notes="", idempotency_key, effect_scope=None):
        payload = {
            "semantic_key": dataset_text(semantic_key, "semantic_key"),
            "name": dataset_text(name, "name"),
            "meaning": dataset_text(meaning, "meaning"),
            "metadata": dataset_metadata(metadata),
            "notes": dataset_text(notes, "notes", optional=True),
        }
        return self._accept_dataset_fact("register", payload, idempotency_key,
            {"semantic_key": payload["semantic_key"]}, effect_scope=effect_scope)

    def register_dataset_version(self, *, dataset_ref, version_label, meaning,
                                 asset_bindings, metadata=None, notes="", idempotency_key, effect_scope=None):
        dataset_text(dataset_ref, "ref")
        if self.query_dataset(dataset_ref) is None:
            raise OwnerConflict("dataset_not_found")
        if not isinstance(asset_bindings, (list, tuple)) or not 1 <= len(asset_bindings) <= 256:
            raise OwnerConflict("dataset_asset_bindings_invalid")
        bindings = [dataset_asset_binding(value) for value in asset_bindings]
        if len({item.version_ref for item in bindings}) != len(bindings):
            raise OwnerConflict("dataset_asset_bindings_invalid")
        payload = {
            "dataset_ref": dataset_ref,
            "version_label": dataset_text(version_label, "version_label"),
            "meaning": dataset_text(meaning, "meaning"),
            "asset_bindings": [item.as_dict() for item in sorted(bindings, key=lambda item: item.version_ref)],
            "metadata": dataset_metadata(metadata),
            "notes": dataset_text(notes, "notes", optional=True),
        }
        def verify_current():
            for binding in bindings:
                self._verify_dataset_asset(binding, current=True)
        return self._accept_dataset_fact("register_version", payload, idempotency_key,
            {"dataset_ref": dataset_ref, "version_label": payload["version_label"]},
            verify_new=verify_current, effect_scope=effect_scope)

    def reference_dataset(self, *, dataset_version_ref, question_ref, purpose="",
                          research_ref=None, notes="", idempotency_key, effect_scope=None):
        dataset_text(dataset_version_ref, "version_ref")
        dataset_text(question_ref, "question_ref")
        version = self.query_dataset_version(dataset_version_ref)
        if version is None:
            raise OwnerConflict("dataset_version_not_found")
        payload = {
            "dataset_version_ref": dataset_version_ref, "question_ref": question_ref,
            "research_ref": research_ref, "purpose": dataset_text(purpose, "purpose", optional=True),
            "notes": dataset_text(notes, "notes", optional=True),
        }
        return self._accept_dataset_fact("reference", payload, idempotency_key,
            {"payload_hash": canonical_hash(payload)}, effect_scope=effect_scope)

    def derive_dataset(self, *, source_dataset_version_ref, derived_dataset_version_ref,
                       question_ref, processing, research_ref=None, notes="", idempotency_key,
                       effect_scope=None):
        """Record a retained data transformation without copying any data bytes."""
        for ref in (source_dataset_version_ref, derived_dataset_version_ref):
            dataset_text(ref, "version_ref")
            if self.query_dataset_version(ref) is None:
                raise OwnerConflict("dataset_version_not_found")
        if source_dataset_version_ref == derived_dataset_version_ref:
            raise OwnerConflict("dataset_derivation_cycle_invalid")
        payload = {
            "source_dataset_version_ref": source_dataset_version_ref,
            "derived_dataset_version_ref": derived_dataset_version_ref,
            "question_ref": question_ref, "research_ref": research_ref,
            "processing": dataset_text(processing, "processing"),
            "notes": dataset_text(notes, "notes", optional=True),
        }
        def verify_new():
            # Run after acquiring the writer: concurrent A->B and B->A cannot
            # each validate against the same old graph and introduce a cycle.
            with self._database.read() as connection:
                cycle = connection.execute(text(
                    "WITH RECURSIVE descendants(ref) AS ("
                    "SELECT derived_dataset_version_ref FROM rg_dataset_derivations "
                    "WHERE source_dataset_version_ref = :derived UNION "
                    "SELECT d.derived_dataset_version_ref FROM rg_dataset_derivations d "
                    "JOIN descendants p ON p.ref = d.source_dataset_version_ref) "
                    "SELECT 1 FROM descendants WHERE ref = :source LIMIT 1"),
                    {"source": source_dataset_version_ref, "derived": derived_dataset_version_ref}).first()
            if cycle is not None:
                raise OwnerConflict("dataset_derivation_cycle_invalid")
        return self._accept_dataset_fact("derive", payload, idempotency_key,
            {"payload_hash": canonical_hash(payload)}, verify_new=verify_new, effect_scope=effect_scope)

    def query_dataset(self, dataset_ref, *, quest_ref=None):
        return self._query_dataset_fact("register", dataset_ref, quest_ref=quest_ref)

    def query_dataset_version(self, dataset_version_ref, *, quest_ref=None):
        return self._query_dataset_fact("register_version", dataset_version_ref, quest_ref=quest_ref)

    def query_dataset_reference(self, dataset_reference_ref, *, quest_ref=None):
        return self._query_dataset_fact("reference", dataset_reference_ref, quest_ref=quest_ref)

    def query_dataset_derivation(self, dataset_derivation_ref, *, quest_ref=None):
        return self._query_dataset_fact("derive", dataset_derivation_ref, quest_ref=quest_ref)

    def verify_asset_quest_scope(self, version_ref, *, quest_ref):
        """Use accepted source facts, never the reference that is being written."""
        roles = self.query_asset_roles(quest_ref=quest_ref, version_refs=(version_ref,))
        if roles:
            role=roles[0]
            from meta_research.owners.common import AcceptedAssetBinding
            return AcceptedAssetBinding(asset_ref=role.asset_ref,version_ref=role.version_ref,
                content_hash=role.asset_hash,manifest_hash=role.manifest_hash,receipt=role.asset_receipt)
        with self._database.read_snapshot() as connection:
            rows = connection.execute(text(
                "SELECT DISTINCT t.target_ref,m.manifest_ref FROM rm_target_root_completion_manifests m "
                "JOIN rg_targets t ON t.target_ref=m.target_ref "
                "JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
                "JOIN rg_target_commits c ON c.target_ref=t.target_ref, json_each(m.entries_json) e "
                "WHERE g.quest_ref=:quest AND json_extract(e.value,'$.binding.version_ref')=:version"),
                {"quest": quest_ref, "version": version_ref}).all()
            for row in rows:
                manifest = self._target_root_manifest_reader.query(row.manifest_ref)
                if (manifest is not None and any(entry.binding.version_ref == version_ref for entry in manifest.entries)
                        and self.query_target_root_commit_transition(row.target_ref) is not None):
                    return next(entry.binding for entry in manifest.entries if entry.binding.version_ref==version_ref)
        raise OwnerConflict("asset_quest_scope_invalid")

    def verify_dataset_version_quest_scope(self, version_ref, *, quest_ref):
        version = self.query_dataset_version(version_ref)
        if version is None:
            raise OwnerConflict("dataset_version_not_found")
        for binding in version["asset_bindings"]:
            self.verify_asset_quest_scope(binding["version_ref"], quest_ref=quest_ref)

    @staticmethod
    def _dataset_scope_clause(operation, table):
        if operation in {"reference", "derive"}:
            return f"EXISTS (SELECT 1 FROM rg_question_lifecycle q WHERE q.question_ref={table}.question_ref AND q.quest_ref=:quest_scope)"
        key = (f"v.dataset_ref={table}.dataset_ref" if operation == "register" else
               f"v.dataset_version_ref={table}.dataset_version_ref")
        return ("EXISTS (SELECT 1 FROM rg_dataset_versions v JOIN rg_dataset_references r "
                "ON r.dataset_version_ref=v.dataset_version_ref JOIN rg_question_lifecycle q "
                f"ON q.question_ref=r.question_ref WHERE {key} AND q.quest_ref=:quest_scope)")

    def reconcile_dataset_operation(self, *, operation, idempotency_key):
        if operation not in _FACTS:
            raise OwnerConflict("dataset_operation_invalid")
        self._dataset_effect_key(idempotency_key)
        with self._database.read() as connection:
            row = connection.execute(text("SELECT * FROM rg_dataset_commands WHERE idempotency_key = :key"),
                                     {"key": idempotency_key}).first()
        if row is None:
            return None
        if row.operation != operation:
            raise OwnerConflict("dataset_idempotency_conflict")
        result = self._query_dataset_fact(operation, row.result_ref)
        if result is None or row.request_hash != result["payload_hash"]:
            raise OwnerConflict("dataset_command_invalid")
        return result

    def query_datasets(self, *, query="", dataset_ref=None, question_ref=None,
                       dataset_version_ref=None, direction="both", offset=0, limit=50, quest_ref=None):
        """Page identities, versions, Question usage, or exact version lineage."""
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise OwnerConflict("dataset_page_invalid")
        dataset_text(query, "query", optional=True)
        if (sum(value is not None for value in (dataset_ref, question_ref, dataset_version_ref)) > 1
                or query and any(value is not None for value in (dataset_ref, question_ref, dataset_version_ref))
                or direction not in {"sources", "derived", "both"}
                or dataset_version_ref is None and direction != "both"):
            raise OwnerConflict("dataset_page_filter_invalid")
        operation, clauses, params = "register", [], {"offset": offset, "limit": limit}
        if dataset_ref is not None:
            dataset_text(dataset_ref, "ref")
            operation, clauses, params["filter"] = "register_version", ["dataset_ref = :filter"], dataset_ref
        elif question_ref is not None:
            dataset_text(question_ref, "question_ref")
            operation, clauses, params["filter"] = "reference", ["question_ref = :filter"], question_ref
        elif dataset_version_ref is not None:
            dataset_text(dataset_version_ref, "version_ref")
            if self.query_dataset_version(dataset_version_ref) is None:
                raise OwnerConflict("dataset_version_not_found")
            operation, params["filter"] = "derive", dataset_version_ref
            clauses = [{
                "sources": "derived_dataset_version_ref = :filter",
                "derived": "source_dataset_version_ref = :filter",
                "both": "(source_dataset_version_ref = :filter OR derived_dataset_version_ref = :filter)",
            }[direction]]
        elif query:
            clauses = ["instr(lower(payload_json), lower(:filter)) > 0"]
            params["filter"] = query
        table, key, _schema = _FACTS[operation]
        if quest_ref is not None:
            clauses.append(self._dataset_scope_clause(operation, table))
            params["quest_scope"] = quest_ref
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._database.read_snapshot() as connection:
            total = int(connection.execute(text(f"SELECT COUNT(*) FROM {table}{where}"), params).scalar_one())
            rows = connection.execute(text(f"SELECT * FROM {table}{where} ORDER BY accepted_at, {key} LIMIT :limit OFFSET :offset"), params).all()
            items = [self._dataset_record(operation, row) for row in rows]
        return {"kind": operation, "items": items, "total": total, "offset": offset,
                "limit": limit, "next_offset": offset + len(items) if offset + len(items) < total else None}

    def _verify_dataset_asset(self, binding, *, current):
        verify = self._asset_verifier.verify_asset_binding if current else self._asset_verifier.verify_asset_receipt
        verify(asset_ref=binding.asset_ref, version_ref=binding.version_ref,
               content_hash=binding.content_hash, manifest_hash=binding.manifest_hash, receipt=binding.receipt)

    def _verify_dataset_research_scope(self, question_ref, research_ref):
        dataset_text(question_ref, "question_ref")
        question = self.query_question_history_by_ref(question_ref)
        if question is None:
            raise OwnerConflict("dataset_question_not_found")
        if research_ref is not None:
            self.verify_dataset_research_ref_scope(research_ref, quest_ref=question.quest_ref)

    def verify_dataset_research_ref_scope(self, research_ref, *, quest_ref):
        """Resolve the accepted research object's own lineage within one Quest."""
        dataset_text(research_ref, "research_ref")
        dataset_text(quest_ref, "quest_ref")
        with self._database.read() as connection:
            quests = connection.execute(text(_RESEARCH_REF_QUEST_QUERY),
                {"ref": research_ref}).scalars().all()
        if not quests:
            raise OwnerConflict("dataset_research_ref_not_found")
        if any(owner_quest != quest_ref for owner_quest in quests):
            raise OwnerConflict("dataset_research_ref_scope_invalid")

    def _dataset_effect_key(self, value):
        dataset_text(value, "idempotency_key")
        if len(value) > 128:
            raise OwnerConflict("dataset_idempotency_key_invalid")

    def _accept_dataset_fact(self, operation, payload, idempotency_key, unique, verify_new=None, effect_scope=None):
        self._dataset_effect_key(idempotency_key)
        table, key, schema = _FACTS[operation]
        payload_hash = canonical_hash(payload)
        with self._database.fenced_write() as connection:
            # The bound MCP gateway supplies the AR authority check. Revalidate
            # after acquiring SQLite's writer so retirement cannot interleave
            # between scope validation and this effect (including its replay).
            if effect_scope is not None:
                if not callable(effect_scope):
                    raise OwnerConflict("dataset_effect_scope_invalid")
                effect_scope()
            if operation in {"reference", "derive"}:
                # Direct Owner callers and idempotent replays receive the same
                # lineage check as the bound gateway, under this writer fence.
                self._verify_dataset_research_scope(payload["question_ref"], payload["research_ref"])
            command = connection.execute(text("SELECT * FROM rg_dataset_commands WHERE idempotency_key = :key"),
                                         {"key": idempotency_key}).first()
            if command is not None:
                if command.operation != operation or command.request_hash != payload_hash:
                    raise OwnerConflict("dataset_idempotency_conflict")
                row = connection.execute(text(f"SELECT * FROM {table} WHERE {key} = :ref"), {"ref": command.result_ref}).first()
                if row is None:
                    raise OwnerConflict("dataset_command_invalid")
                result = self._dataset_record(operation, row)
                if result["payload_hash"] != payload_hash:
                    raise OwnerConflict("dataset_command_invalid")
                return result
            where = " AND ".join(f"{column} = :{column}" for column in unique)
            row = connection.execute(text(f"SELECT * FROM {table} WHERE {where}"), unique).first()
            if row is not None:
                result = self._dataset_record(operation, row)
                if row.payload_hash != payload_hash:
                    raise OwnerConflict("dataset_semantic_identity_conflict")
                ref = result[key]
            else:
                if verify_new is not None:
                    verify_new()
                ref = new_ref(key.removesuffix("_ref"))
                now, receipt_ref = time.time(), new_ref("rg_dataset_receipt")
                values = {key: ref, "payload_json": canonical_json(payload), "payload_hash": payload_hash,
                          "accepted_at": now, "receipt_ref": receipt_ref,
                          "receipt_hash": _receipt_hash(schema, ref, payload_hash, receipt_ref, now)}
                for column in _INDEXED_FIELDS[operation]:
                    values[column] = payload[column]
                columns = ", ".join(values)
                connection.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({', '.join(':' + column for column in values)})"), values)
                if operation == "register_version":
                    for binding in payload["asset_bindings"]:
                        connection.execute(text("INSERT INTO rg_dataset_version_assets (dataset_version_ref, asset_version_ref) VALUES (:dataset, :asset)"),
                                           {"dataset": ref, "asset": binding["version_ref"]})
                connection.execute(text("UPDATE research_graph_state SET revision = revision + 1 WHERE singleton = 'owner'"))
                self._feed.record(connection, f"research_graph.dataset_{operation}_accepted", {key: ref, "payload_hash": payload_hash})
                result = {"schema_ref": schema, key: ref, **payload, "payload_hash": payload_hash,
                          "accepted_at": now, "receipt": _receipt(schema, ref, payload_hash, receipt_ref, now)}
            connection.execute(text("INSERT INTO rg_dataset_commands (idempotency_key, operation, request_hash, result_ref, recorded_at) VALUES (:key, :operation, :hash, :ref, :now)"),
                               {"key": idempotency_key, "operation": operation, "hash": payload_hash, "ref": ref, "now": time.time()})
        return result

    def _query_dataset_fact(self, operation, ref, *, quest_ref=None):
        dataset_text(ref, "ref")
        table, key, _schema = _FACTS[operation]
        where = f"{key} = :ref"
        if quest_ref is not None:
            where += " AND " + self._dataset_scope_clause(operation, table)
        with self._database.read_snapshot() as connection:
            row = connection.execute(text(f"SELECT * FROM {table} WHERE {where}"), {"ref": ref, "quest_scope": quest_ref}).first()
            return None if row is None else self._dataset_record(operation, row)

    def _dataset_record(self, operation, row):
        _table, key, schema = _FACTS[operation]
        mapping = row._mapping
        try:
            payload = json.loads(row.payload_json)
            if not isinstance(payload, dict) or canonical_hash(payload) != row.payload_hash or row.receipt_hash != _receipt_hash(schema, mapping[key], row.payload_hash, row.receipt_ref, row.accepted_at):
                raise ValueError
            indexed = _INDEXED_FIELDS[operation]
            if any(payload[field] != mapping[field] for field in indexed):
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise OwnerConflict("dataset_receipt_invalid") from error
        if operation == "register_version":
            if self.query_dataset(payload["dataset_ref"]) is None:
                raise OwnerConflict("dataset_receipt_invalid")
            bindings = [dataset_asset_binding(value) for value in payload["asset_bindings"]]
            with self._database.read() as connection:
                assets = connection.execute(text("SELECT asset_version_ref FROM rg_dataset_version_assets WHERE dataset_version_ref = :ref"), {"ref": mapping[key]}).scalars().all()
            if sorted(assets) != sorted(binding.version_ref for binding in bindings):
                raise OwnerConflict("dataset_asset_reference_invalid")
            for binding in bindings:
                self._verify_dataset_asset(binding, current=False)
        elif operation == "reference":
            if self.query_dataset_version(payload["dataset_version_ref"]) is None or self.query_question_history_by_ref(payload["question_ref"]) is None:
                raise OwnerConflict("dataset_reference_invalid")
            self._verify_dataset_research_scope(payload["question_ref"], payload["research_ref"])
        elif operation == "derive":
            if (payload["source_dataset_version_ref"] == payload["derived_dataset_version_ref"]
                    or any(self.query_dataset_version(payload[field]) is None for field in
                           ("source_dataset_version_ref", "derived_dataset_version_ref"))):
                raise OwnerConflict("dataset_derivation_invalid")
            self._verify_dataset_research_scope(payload["question_ref"], payload["research_ref"])
        return {"schema_ref": schema, key: mapping[key], **payload, "payload_hash": row.payload_hash,
                "accepted_at": row.accepted_at,
                "receipt": _receipt(schema, mapping[key], row.payload_hash, row.receipt_ref, row.accepted_at)}

    def query_dataset_asset_references(self, asset_version_ref):
        with self._database.read_snapshot() as connection:
            refs = connection.execute(text("SELECT dataset_version_ref FROM rg_dataset_version_assets WHERE asset_version_ref = :ref ORDER BY dataset_version_ref"),
                                      {"ref": asset_version_ref}).scalars().all()
            for ref in refs:
                if self.query_dataset_version(ref) is None:
                    raise OwnerConflict("dataset_asset_reference_invalid")
        return tuple(f"dataset-version:{ref}" for ref in refs)


def _receipt_hash(schema, ref, payload_hash, receipt_ref, accepted_at):
    return canonical_hash({"schema_ref": schema, "subject_ref": ref, "payload_hash": payload_hash,
                           "receipt_ref": receipt_ref, "accepted_at": accepted_at})


def _receipt(schema, ref, payload_hash, receipt_ref, accepted_at):
    return AcceptanceReceipt(issuer="research_graph", kind=schema.split("/")[1].replace("-", "_") + "_acceptance",
        receipt_ref=receipt_ref, subject_ref=ref,
        payload_hash=_receipt_hash(schema, ref, payload_hash, receipt_ref, accepted_at)).as_public_dict()
