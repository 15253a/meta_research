"""Reusable resource descriptions in RG; digital content remains in RM.

An environment_ref names an immutable index snapshot, not a frozen device or
service. A semantic_key can group successive descriptions of the same resource.
"""
from __future__ import annotations

import json
import time
from typing import Protocol

from sqlalchemy import text

from meta_research.dataset_contract import dataset_asset_binding, dataset_metadata, dataset_text
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json, new_ref


_FACTS = {
    "register": ("rg_environments", "environment_ref", ("semantic_key", "origin_quest_ref")),
    "reference": ("rg_environment_references", "environment_reference_ref", ("environment_ref", "question_ref")),
}


def _validate(validator, *args, **kwargs):
    # The accepted digital binding and JSON contracts are shared with Dataset.
    try:
        return validator(*args, **kwargs)
    except OwnerConflict as error:
        raise OwnerConflict(error.code.replace("dataset_", "environment_", 1)) from error


def _receipt(operation, ref, payload_hash, receipt_ref, accepted_at):
    return AcceptanceReceipt(issuer="research_graph", kind="environment_" + operation + "_acceptance",
        receipt_ref=receipt_ref, subject_ref=ref,
        payload_hash=canonical_hash({"operation": operation, "subject_ref": ref,
            "payload_hash": payload_hash, "receipt_ref": receipt_ref, "accepted_at": accepted_at})).as_public_dict()


class ResearchEnvironmentOwnerInterface(Protocol):
    def register_environment(self, **values) -> dict[str, object]: ...
    def reference_environment(self, **values) -> dict[str, object]: ...
    def query_environment(self, environment_ref: str, *, quest_ref=None) -> dict[str, object] | None: ...
    def query_environment_reference(self, environment_reference_ref: str, *, quest_ref=None) -> dict[str, object] | None: ...
    def query_environments(self, **values) -> dict[str, object]: ...
    def reconcile_environment_operation(self, *, operation, idempotency_key) -> dict[str, object] | None: ...
    def query_environment_asset_references(self, asset_version_ref: str) -> tuple[str, ...]: ...
    def verify_environment_quest_scope(self, environment_ref: str, *, quest_ref: str) -> None: ...


class ResearchEnvironmentOwnerMixin:
    def register_environment(self, *, semantic_key, name, meaning, source, asset_bindings=None,
                             metadata=None, notes="", source_environment_ref=None, quest_ref=None,
                             idempotency_key, effect_scope=None):
        payload = {field: _validate(dataset_text, value, field, optional=field == "notes")
            for field, value in (("semantic_key", semantic_key), ("name", name),
                                 ("meaning", meaning), ("source", source), ("notes", notes))}
        if asset_bindings is None:
            asset_bindings = []
        if not isinstance(asset_bindings, (list, tuple)) or len(asset_bindings) > 256:
            raise OwnerConflict("environment_asset_bindings_invalid")
        bindings = [_validate(dataset_asset_binding, item) for item in asset_bindings]
        if len({item.version_ref for item in bindings}) != len(bindings):
            raise OwnerConflict("environment_asset_bindings_invalid")
        payload.update(asset_bindings=[item.as_dict() for item in sorted(bindings, key=lambda item: item.version_ref)],
                       metadata=_validate(dataset_metadata, metadata), origin_quest_ref=quest_ref,
                       source_environment_ref=source_environment_ref)
        if quest_ref is not None:
            _validate(dataset_text, quest_ref, "quest_ref")
        if source_environment_ref is not None:
            _validate(dataset_text, source_environment_ref, "source_environment_ref")

        def verify_new():
            for binding in bindings:
                self._verify_environment_asset(binding, current=True)

        return self._accept_environment_fact("register", payload, idempotency_key,
                                             effect_scope=effect_scope, verify_new=verify_new)

    def reference_environment(self, *, environment_ref, question_ref, purpose="", research_ref=None,
                              notes="", idempotency_key, effect_scope=None):
        payload = {field: _validate(dataset_text, value, field, optional=field in {"purpose", "notes"})
            for field, value in (("environment_ref", environment_ref), ("question_ref", question_ref),
                                 ("purpose", purpose), ("notes", notes))}
        payload["research_ref"] = research_ref
        return self._accept_environment_fact("reference", payload, idempotency_key, effect_scope=effect_scope)

    def _verify_environment_sources(self, operation, payload):
        if operation == "register":
            quest_ref = payload["origin_quest_ref"]
            if quest_ref is not None:
                with self._database.read() as connection:
                    exists = connection.execute(text("SELECT 1 FROM rg_quests WHERE quest_ref=:ref"),
                                                {"ref": quest_ref}).first()
                if exists is None:
                    raise OwnerConflict("environment_quest_not_found")
                for binding in payload["asset_bindings"]:
                    self.verify_asset_quest_scope(binding["version_ref"], quest_ref=quest_ref)
            source_ref = payload["source_environment_ref"]
            if source_ref is not None and self.query_environment(source_ref, quest_ref=quest_ref) is None:
                raise OwnerConflict("environment_source_not_found")
        else:
            question = self.query_question_history_by_ref(payload["question_ref"])
            if question is None:
                raise OwnerConflict("environment_question_not_found")
            environment = self.query_environment(payload["environment_ref"])
            if environment is None:
                raise OwnerConflict("environment_not_found")
            # Origin records provenance, not an exclusive resource owner. An
            # exact snapshot may be adopted by another Quest, but its digital
            # bindings must already have independent accepted source facts there.
            for binding in environment["asset_bindings"]:
                self.verify_asset_quest_scope(binding["version_ref"], quest_ref=question.quest_ref)
            if payload["research_ref"] is not None:
                self.verify_dataset_research_ref_scope(payload["research_ref"], quest_ref=question.quest_ref)

    def _verify_environment_asset(self, binding, *, current):
        verify = self._asset_verifier.verify_asset_binding if current else self._asset_verifier.verify_asset_receipt
        verify(asset_ref=binding.asset_ref, version_ref=binding.version_ref,
               content_hash=binding.content_hash, manifest_hash=binding.manifest_hash, receipt=binding.receipt)

    def verify_environment_quest_scope(self, environment_ref, *, quest_ref):
        record = self.query_environment(environment_ref)
        if record is None:
            raise OwnerConflict("environment_quest_scope_invalid")
        for binding in record["asset_bindings"]:
            self.verify_asset_quest_scope(binding["version_ref"], quest_ref=quest_ref)

    def _accept_environment_fact(self, operation, payload, idempotency_key, *, effect_scope=None, verify_new=None):
        _validate(dataset_text, idempotency_key, "idempotency_key")
        if len(idempotency_key) > 128:
            raise OwnerConflict("environment_idempotency_key_invalid")
        table, key, indexed = _FACTS[operation]
        digest = canonical_hash(payload)
        with self._database.fenced_write() as connection:
            if effect_scope is not None:
                if not callable(effect_scope):
                    raise OwnerConflict("environment_effect_scope_invalid")
                effect_scope()
            self._verify_environment_sources(operation, payload)
            command = connection.execute(text("SELECT * FROM rg_environment_commands WHERE idempotency_key=:key"),
                                         {"key": idempotency_key}).first()
            if command is not None:
                if command.operation != operation or command.request_hash != digest:
                    raise OwnerConflict("environment_idempotency_conflict")
                result = self._query_environment_fact(operation, command.result_ref)
                if result is None or result["payload_hash"] != digest:
                    raise OwnerConflict("environment_command_invalid")
                return result
            row = connection.execute(text(f"SELECT * FROM {table} WHERE payload_hash=:hash"), {"hash": digest}).first()
            if row is None:
                if verify_new is not None:
                    verify_new()
                ref, now, receipt_ref = new_ref(key.removesuffix("_ref")), time.time(), new_ref("rg_environment_receipt")
                receipt = _receipt(operation, ref, digest, receipt_ref, now)
                values = {key: ref, "payload_json": canonical_json(payload), "payload_hash": digest,
                          "receipt_ref": receipt_ref, "receipt_hash": receipt["payload_hash"], "accepted_at": now,
                          **{field: payload[field] for field in indexed}}
                connection.execute(text(f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join(':' + field for field in values)})"), values)
                if operation == "register":
                    for binding in payload["asset_bindings"]:
                        connection.execute(text("INSERT INTO rg_environment_assets (environment_ref,asset_version_ref) VALUES (:ref,:asset)"),
                                           {"ref": ref, "asset": binding["version_ref"]})
                connection.execute(text("UPDATE research_graph_state SET revision=revision+1 WHERE singleton='owner'"))
                self._feed.record(connection, f"research_graph.environment_{operation}_accepted", {key: ref, "payload_hash": digest})
                result = {key: ref, **payload, "schema_ref": f"meta-research/environment-{operation}/v1",
                          "payload_hash": digest, "receipt": receipt, "accepted_at": now}
            else:
                result = self._environment_record(operation, row)
                ref = result[key]
            connection.execute(text("INSERT INTO rg_environment_commands (idempotency_key,operation,request_hash,result_ref,recorded_at) "
                                    "VALUES (:key,:op,:hash,:ref,:now)"),
                               {"key": idempotency_key, "op": operation, "hash": digest, "ref": ref, "now": time.time()})
        return result

    @staticmethod
    def _environment_scope_clause(operation):
        if operation == "reference":
            return "EXISTS (SELECT 1 FROM rg_question_lifecycle q WHERE q.question_ref=rg_environment_references.question_ref AND q.quest_ref=:quest)"
        return ("(origin_quest_ref=:quest OR EXISTS (SELECT 1 FROM rg_environment_references r "
                "JOIN rg_question_lifecycle q ON q.question_ref=r.question_ref "
                "WHERE r.environment_ref=rg_environments.environment_ref AND q.quest_ref=:quest))")

    def _query_environment_fact(self, operation, ref, *, quest_ref=None):
        _validate(dataset_text, ref, "ref")
        table, key, _ = _FACTS[operation]
        clause = " AND " + self._environment_scope_clause(operation) if quest_ref is not None else ""
        with self._database.read_snapshot() as connection:
            row = connection.execute(text(f"SELECT * FROM {table} WHERE {key}=:ref{clause}"),
                                     {"ref": ref, "quest": quest_ref}).first()
            if row is None:
                return None
            result = self._environment_record(operation, row)
            if operation == "register" and quest_ref is not None:
                self._verify_environment_visibility(result, quest_ref=quest_ref)
            return result

    def _verify_environment_visibility(self, environment, *, quest_ref):
        if environment["origin_quest_ref"] == quest_ref:
            return
        # The SQL index only finds a candidate. Verify the accepting reference
        # before letting it make another Quest's resource visible to a reader.
        with self._database.read() as connection:
            row = connection.execute(text("SELECT r.* FROM rg_environment_references r "
                "JOIN rg_question_lifecycle q ON q.question_ref=r.question_ref "
                "WHERE r.environment_ref=:ref AND q.quest_ref=:quest "
                "ORDER BY r.accepted_at,r.environment_reference_ref LIMIT 1"),
                {"ref": environment["environment_ref"], "quest": quest_ref}).first()
        if row is None:
            raise OwnerConflict("environment_quest_scope_invalid")
        self._environment_record("reference", row)

    def _environment_record(self, operation, row):
        _, key, indexed = _FACTS[operation]
        ref = row._mapping[key]
        receipt = _receipt(operation, ref, row.payload_hash, row.receipt_ref, row.accepted_at)
        try:
            payload = json.loads(row.payload_json)
            if (not isinstance(payload, dict) or canonical_hash(payload) != row.payload_hash
                    or receipt["payload_hash"] != row.receipt_hash
                    or any(payload[field] != row._mapping[field] for field in indexed)):
                raise ValueError
        except (ValueError, TypeError, KeyError) as error:
            raise OwnerConflict("environment_receipt_invalid") from error
        if operation == "register":
            bindings = [_validate(dataset_asset_binding, item) for item in payload["asset_bindings"]]
            with self._database.read() as connection:
                assets = connection.execute(text("SELECT asset_version_ref FROM rg_environment_assets WHERE environment_ref=:ref"),
                                            {"ref": ref}).scalars().all()
            if sorted(assets) != sorted(item.version_ref for item in bindings):
                raise OwnerConflict("environment_asset_reference_invalid")
            for binding in bindings:
                self._verify_environment_asset(binding, current=False)
        else:
            self._verify_environment_sources(operation, payload)
        return {key: ref, **payload, "schema_ref": f"meta-research/environment-{operation}/v1",
                "payload_hash": row.payload_hash, "receipt": receipt, "accepted_at": row.accepted_at}

    def query_environment(self, environment_ref, *, quest_ref=None):
        return self._query_environment_fact("register", environment_ref, quest_ref=quest_ref)

    def query_environment_reference(self, environment_reference_ref, *, quest_ref=None):
        return self._query_environment_fact("reference", environment_reference_ref, quest_ref=quest_ref)

    def query_environments(self, *, query="", environment_ref=None, question_ref=None, semantic_key=None,
                           offset=0, limit=50, quest_ref=None):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise OwnerConflict("environment_page_invalid")
        _validate(dataset_text, query, "query", optional=True)
        filters = {field: value for field, value in (("environment_ref", environment_ref),
                    ("question_ref", question_ref), ("semantic_key", semantic_key)) if value is not None}
        if len(filters) > 1 or query and filters:
            raise OwnerConflict("environment_page_filter_invalid")
        operation = "reference" if environment_ref is not None or question_ref is not None else "register"
        table, key, _ = _FACTS[operation]
        clauses, params = [], {"limit": limit, "offset": offset, "quest": quest_ref}
        for field, value in filters.items():
            _validate(dataset_text, value, field)
            clauses.append(f"{field}=:filter")
            params["filter"] = value
        if query:
            clauses.append("instr(lower(payload_json),lower(:query))>0")
            params["query"] = query
        if quest_ref is not None:
            clauses.append(self._environment_scope_clause(operation))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._database.read_snapshot() as connection:
            total = connection.execute(text(f"SELECT count(*) FROM {table}{where}"), params).scalar_one()
            rows = connection.execute(text(f"SELECT * FROM {table}{where} ORDER BY accepted_at,{key} LIMIT :limit OFFSET :offset"), params).all()
            items = [self._environment_record(operation, row) for row in rows]
            if operation == "register" and quest_ref is not None:
                for item in items:
                    self._verify_environment_visibility(item, quest_ref=quest_ref)
        return {"kind": operation, "items": items, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + len(items) if offset + len(items) < total else None}

    def reconcile_environment_operation(self, *, operation, idempotency_key):
        if operation not in _FACTS:
            raise OwnerConflict("environment_operation_invalid")
        _validate(dataset_text, idempotency_key, "idempotency_key")
        with self._database.read() as connection:
            command = connection.execute(text("SELECT * FROM rg_environment_commands WHERE idempotency_key=:key"),
                                         {"key": idempotency_key}).first()
        if command is None:
            return None
        if command.operation != operation:
            raise OwnerConflict("environment_idempotency_conflict")
        result = self._query_environment_fact(operation, command.result_ref)
        if result is None or result["payload_hash"] != command.request_hash:
            raise OwnerConflict("environment_command_invalid")
        return result

    def query_environment_asset_references(self, asset_version_ref):
        with self._database.read_snapshot() as connection:
            refs = connection.execute(text("SELECT environment_ref FROM rg_environment_assets WHERE asset_version_ref=:ref ORDER BY environment_ref"),
                                      {"ref": asset_version_ref}).scalars().all()
            for ref in refs:
                if self.query_environment(ref) is None:
                    raise OwnerConflict("environment_asset_reference_invalid")
        return tuple(f"environment:{ref}" for ref in refs)
