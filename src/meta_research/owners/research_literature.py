"""Immutable ordinary Literature facts over accepted RM assets and RG research.

The index records external authorship separately from the current reading work.
It does not manufacture a DeepFetch execution or copy the paper into another store.
"""
from __future__ import annotations
import json
import time
from typing import Protocol
from sqlalchemy import text
from meta_research.dataset_contract import dataset_asset_binding, dataset_metadata, dataset_text
from meta_research.owners.common import AcceptanceReceipt, OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.owners.asset_lifecycle import assert_asset_usable


def _receipt(ref, kind, digest, receipt_ref, accepted_at):
    return AcceptanceReceipt("research_graph", "literature_" + kind + "_acceptance", receipt_ref, ref,
        canonical_hash([ref, kind, digest, receipt_ref, accepted_at])).as_public_dict()


class ResearchLiteratureOwnerInterface(Protocol):
    def register_literature(self, **values) -> dict: ...
    def reference_literature(self, **values) -> dict: ...
    def query_literature(self, ref, *, quest_ref) -> dict | None: ...
    def reconcile_literature_operation(self, *, operation, idempotency_key) -> dict | None: ...


class ResearchLiteratureOwnerMixin:
    def register_literature(self, *, quest_ref, record_ref, title, source, fulltext_status,
                            asset_bindings=(), metadata=None, idempotency_key, effect_scope=None):
        payload = {k: dataset_text(v, k) for k, v in dict(quest_ref=quest_ref,
            record_ref=record_ref, title=title, source=source).items()}
        if fulltext_status not in {"obtained", "missing", "waiting_user"}:
            raise OwnerConflict("literature_fulltext_status_invalid")
        payload.update(fulltext_status=fulltext_status, asset_bindings=self._literature_bindings(asset_bindings),
                       metadata=dataset_metadata(metadata))
        if (fulltext_status == "obtained") != bool(payload["asset_bindings"]):
            raise OwnerConflict("literature_fulltext_binding_required", {
                "path": ["asset_bindings"],
                "message": "asset_bindings must be nonempty exactly when fulltext_status=obtained.",
                "correction": "Make status and bindings match the full text actually available; do not invent acquisition."})
        return self._accept_literature("register", payload, idempotency_key, effect_scope)

    def reference_literature(self, *, quest_ref, literature_ref, question_ref, purpose,
                             research_ref=None, reading_summary="", reading_bindings=(),
                             idempotency_key, effect_scope=None):
        payload = {k: dataset_text(v, k) for k, v in dict(quest_ref=quest_ref,
            literature_ref=literature_ref, question_ref=question_ref, purpose=purpose).items()}
        payload.update(research_ref=research_ref, reading_summary=dataset_text(reading_summary, "reading_summary", optional=True),
                       reading_bindings=self._literature_bindings(reading_bindings))
        return self._accept_literature("reference", payload, idempotency_key, effect_scope)

    @staticmethod
    def _literature_bindings(values):
        if not isinstance(values, (list, tuple)):
            raise OwnerConflict("literature_asset_bindings_invalid")
        bindings = [dataset_asset_binding(v).as_dict() for v in values]
        if len({v["version_ref"] for v in bindings}) != len(bindings):
            raise OwnerConflict("literature_asset_bindings_invalid")
        return sorted(bindings, key=lambda v: v["version_ref"])

    def _verify_literature(self, kind, payload, *, current=False):
        quest = payload["quest_ref"]
        with self._database.read() as c:
            if c.execute(text("SELECT 1 FROM rg_quests WHERE quest_ref=:q"), {"q":quest}).first() is None:
                raise OwnerConflict("literature_quest_scope_invalid")
        if kind == "reference":
            source = self.query_literature(payload["literature_ref"], quest_ref=quest)
            if source is None or source["kind"] != "register":
                raise OwnerConflict("literature_source_unbound")
            question = self.query_question_history_by_ref(payload["question_ref"])
            if question is None or question.quest_ref != quest:
                raise OwnerConflict("literature_question_scope_invalid")
            if payload["research_ref"] is not None:
                self.verify_dataset_research_ref_scope(payload["research_ref"], quest_ref=quest)
        values = payload["asset_bindings"] if kind == "register" else payload["reading_bindings"]
        for value in values:
            binding = dataset_asset_binding(value)
            self._verify_dataset_asset(binding, current=current)
            self.verify_asset_quest_scope(binding.version_ref, quest_ref=quest)
            if current:
                with self._database.read() as c:
                    assert_asset_usable(c, binding.version_ref)
        # A Target relationship must identify that Target's actual accepted note,
        # not merely an unrelated note in the same Quest.
        target = self.literature_reading_target(payload.get("research_ref")) if kind == "reference" else None
        if target is not None:
            if self.query_target_root_commit_transition(target) is None:
                raise OwnerConflict("literature_target_not_accepted")
            from meta_research.research_notes import query_target_research_notes
            memory = self._literature_source_reader
            for value in values:
                if not query_target_research_notes(self._database, memory, target, version_ref=value["version_ref"]):
                    raise OwnerConflict("literature_reading_source_invalid")

    def literature_reading_target(self, research_ref):
        with self._database.read() as c:
            return c.execute(text("SELECT target_ref FROM rg_targets WHERE target_ref=:ref UNION "
                "SELECT target_ref FROM rg_target_commits WHERE commit_ref=:ref"),
                {"ref":research_ref}).scalar_one_or_none()

    def _accept_literature(self, kind, payload, key, effect_scope):
        dataset_text(key, "idempotency_key")
        if len(key) > 128:
            raise OwnerConflict("literature_idempotency_key_invalid")
        digest = canonical_hash(payload)
        with self._database.fenced_write() as c:
            if effect_scope is not None:
                effect_scope()
            self._verify_literature(kind, payload)
            prior = c.execute(text("SELECT * FROM rg_literature_commands WHERE idempotency_key=:key"), {"key":key}).first()
            if prior is not None:
                if prior.kind != kind or prior.request_hash != digest:
                    raise OwnerConflict("literature_idempotency_conflict")
                return self.query_literature(prior.result_ref, quest_ref=payload["quest_ref"])
            row = c.execute(text("SELECT * FROM rg_literature_facts WHERE kind=:kind AND payload_hash=:hash"),
                            {"kind":kind,"hash":digest}).first()
            if row is None:
                self._verify_literature(kind, payload, current=True)
                ref, receipt_ref, now = new_ref("literature" if kind == "register" else "literature_reference"), new_ref("rg_receipt"), time.time()
                receipt = _receipt(ref, kind, digest, receipt_ref, now)
                c.execute(text("INSERT INTO rg_literature_facts VALUES (:ref,:kind,:quest,:payload,:hash,:receipt,:receipt_hash,:now)"),
                    dict(ref=ref,kind=kind,quest=payload["quest_ref"],payload=canonical_json(payload),hash=digest,
                         receipt=receipt_ref,receipt_hash=receipt["payload_hash"],now=now))
                c.execute(text("UPDATE research_graph_state SET revision=revision+1 WHERE singleton='owner'"))
                self._feed.record(c, "research_graph.literature_" + kind + "_accepted", {"ref":ref,"payload_hash":digest})
            else:
                ref = row.ref
            c.execute(text("INSERT INTO rg_literature_commands VALUES (:key,:kind,:hash,:ref)"),
                      dict(key=key,kind=kind,hash=digest,ref=ref))
        return self.query_literature(ref, quest_ref=payload["quest_ref"])

    def query_literature(self, ref, *, quest_ref):
        with self._database.read_snapshot() as c:
            row = c.execute(text("SELECT * FROM rg_literature_facts WHERE ref=:ref AND quest_ref=:quest"),
                            {"ref":ref,"quest":quest_ref}).first()
            if row is None:
                return None
            receipt = _receipt(ref, row.kind, row.payload_hash, row.receipt_ref, row.accepted_at)
            try:
                payload = json.loads(row.payload_json)
                if (not isinstance(payload,dict) or row.kind not in {"register","reference"}
                        or canonical_hash(payload) != row.payload_hash or payload.get("quest_ref") != row.quest_ref
                        or receipt["payload_hash"] != row.receipt_hash):
                    raise ValueError
            except (TypeError, ValueError) as error:
                raise OwnerConflict("literature_receipt_invalid") from error
            self._verify_literature(row.kind, payload)
            return {**payload,"ref":ref,"kind":row.kind,"payload_hash":row.payload_hash,
                    "receipt":receipt,"accepted_at":row.accepted_at}

    def reconcile_literature_operation(self, *, operation, idempotency_key):
        if operation not in {"register", "reference"}:
            raise OwnerConflict("literature_operation_invalid")
        with self._database.read() as c:
            row = c.execute(text("SELECT a.request_hash,a.kind,f.ref,f.quest_ref FROM rg_literature_commands a "
                "JOIN rg_literature_facts f ON f.ref=a.result_ref WHERE a.idempotency_key=:key"),
                {"key":idempotency_key}).first()
        if row is None:
            return None
        result = self.query_literature(row.ref, quest_ref=row.quest_ref)
        if row.kind != operation or result["kind"] != operation or result["payload_hash"] != row.request_hash:
            raise OwnerConflict("literature_command_invalid")
        return result
