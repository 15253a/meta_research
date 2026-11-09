"""Explicit, authenticated human research input; ordinary chat stays chat."""
from __future__ import annotations
import json
import time
from sqlalchemy import text
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.dataset_contract import dataset_asset_binding
from meta_research.owners.asset_lifecycle import assert_asset_usable


def read_research_input(database, input_ref, *, quest_ref=None):
    with database.read_snapshot() as c:
        row=c.execute(text("SELECT * FROM hc_research_inputs WHERE input_ref=:ref"),{"ref":input_ref}).first()
        if row is None or quest_ref is not None and row.quest_ref!=quest_ref:return None
        value = _verified_input_value(row)
        from meta_research.work_materials import material_submissions_for
        return {"input_ref":row.input_ref,**value,"work_materials": material_submissions_for(database, "input", row.input_ref), "content_hash":row.content_hash,"created_at":row.created_at,
                "receipt":{"issuer":"human_collaboration","kind":"research_input","receipt_ref":row.receipt_ref,
                           "subject_ref":row.input_ref,"payload_hash":row.receipt_hash}}


def research_input_leaf(record):
    """Project the adoption identity from this call's verified Owner record."""
    return {"kind": "HumanInput", "ref": record["input_ref"],
            "input_ref": record["input_ref"], "content_hash": record["content_hash"],
            "owner_acceptance_receipt_ref": record["receipt"]["receipt_ref"]}


def _verified_input_value(row):
    try:
        value=json.loads(row.payload_json)
    except (TypeError, ValueError) as error:
        raise OwnerConflict("human_input_receipt_invalid") from error
    if not isinstance(value, dict):
        raise OwnerConflict("human_input_receipt_invalid")
    if (canonical_hash(value)!=row.content_hash or value.get("quest_ref")!=row.quest_ref
        or value.get("question_ref")!=row.question_ref
        or row.receipt_hash!=canonical_hash({"input_ref":row.input_ref,"content_hash":row.content_hash,
            "receipt_ref":row.receipt_ref,"created_at":row.created_at})):
        raise OwnerConflict("human_input_receipt_invalid")
    return value


_TREATMENT_JOIN = (
    " FROM hc_guidance_effects e JOIN hc_guidance_deliveries d USING(delivery_ref) "
    "JOIN hc_guidance_snapshots s USING(snapshot_ref) "
    "WHERE e.kind='feedback' AND s.quest_ref=:quest"
)
_TREATMENT_SELECT = (
    "SELECT e.*,d.constraint_ref,d.revision,d.guidance_hash,s.root_kind,s.run_ref,"
    "s.operation_ref,s.quest_ref,s.snapshot_ref,s.snapshot_hash" + _TREATMENT_JOIN
)


def _verified_treatment(row):
    try:
        receipt = json.loads(row.receipt_json)
        payload = {key: value for key, value in receipt.items() if key != "receipt"}
        binding = receipt["binding"]
        guide = receipt["guide_ref"]
        if (canonical_hash(receipt) != row.receipt_hash
                or receipt["receipt"]["payload_hash"] != canonical_hash(payload)
                or receipt["receipt"]["kind"] != "guidance_feedback"
                or receipt["delivery_ref"] != row.delivery_ref
                or binding != {"identity": {"root_kind": row.root_kind, "run_ref": row.run_ref,
                    "operation_ref": row.operation_ref}, "quest_ref": row.quest_ref,
                    "snapshot_ref": row.snapshot_ref, "snapshot_hash": row.snapshot_hash}
                or guide["constraint_ref"] != row.constraint_ref
                or guide["revision"] != row.revision or guide["guidance_hash"] != row.guidance_hash
                or any(not isinstance(receipt[key], str) or not receipt[key].strip()
                    for key in ("understanding", "changes", "continuing_work", "reasons", "disposition", "goal_impact"))):
            raise ValueError("feedback identity mismatch")
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        raise OwnerConflict("guidance_receipt_invalid") from error
    return receipt


class HumanResearchInputMixin:
    def query_research_guidance(self, *, quest_ref, query="", offset=0, limit=12,
                                query_revision=None):
        """Discover confirmed guidance without claiming delivery or reading it."""
        from meta_research.research_pagination import bind_page, page_state
        from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row
        if (type(offset) is not int or offset < 0 or type(limit) is not int
                or not 1 <= limit <= 100 or not isinstance(query, str) or len(query) > 1024):
            raise OwnerConflict("human_input_page_invalid")
        with self._database.read_snapshot() as connection:
            revisions = connection.execute(text(
                "SELECT constraint_ref,revision,guidance_hash,status,receipt_hash FROM hc_soft_constraints "
                "WHERE scope_ref=:scope ORDER BY constraint_ref"
            ), {"scope": "quest:" + quest_ref}).all()
            treatments = connection.execute(text(
                "SELECT e.effect_key,e.receipt_hash FROM hc_guidance_effects e "
                "JOIN hc_guidance_deliveries d USING(delivery_ref) "
                "JOIN hc_soft_constraints g ON g.constraint_ref=d.constraint_ref "
                "WHERE g.scope_ref=:scope AND e.kind='feedback' ORDER BY e.effect_key"
            ), {"scope": "quest:" + quest_ref}).all()
            revision = bind_page({"entry": "guidance", "quest_ref": quest_ref, "query": query},
                [list(row) for row in (*revisions, *treatments)], offset=offset, query_revision=query_revision)
            rows = connection.execute(text(
                "SELECT * FROM hc_soft_constraints WHERE scope_ref=:scope "
                "AND (:query='' OR instr(lower(guidance_json),lower(:query))>0) "
                "ORDER BY created_at DESC,constraint_ref LIMIT :limit OFFSET :offset"
            ), {"scope": "quest:" + quest_ref, "query": query, "limit": limit + 1, "offset": offset}).all()
            items = []
            for row in rows[:limit]:
                binding = guidance_binding_from_row(row)
                guide = binding["guidance"]
                items.append({"constraint_ref": row.constraint_ref,
                    "name": guide.get("text", "")[:80],
                    "summary": guide.get("text", "")[:1200], "strength": guide.get("strength", 3),
                    "semantic_scope": guide.get("semantic_scope"), "status": row.status,
                    "meaning": "formally_submitted_guidance", "revision": row.revision,
                    "content_reader": {"source_ref": row.constraint_ref, "version_ref": row.guidance_hash},
                    "reader": {"operation": "human_request.read", "constraint_ref": row.constraint_ref,
                               "version_ref": row.guidance_hash}})
            next_offset = offset + limit if len(rows) > limit else None
        return {"items": items, "offset": offset, "next_offset": next_offset,
                "query_revision": revision, **page_state(items, next_offset, offset=offset, query=query)}

    def read_research_guidance(self, constraint_ref, *, quest_ref, version_ref):
        """Read the retained formal instruction; current status is explicit."""
        from meta_research.owners.human_collaboration_ladder import guidance_binding_from_row
        from meta_research.work_materials import material_submissions_for
        with self._database.read_snapshot() as connection:
            row = connection.execute(text(
                "SELECT * FROM hc_soft_constraints WHERE constraint_ref=:ref AND scope_ref=:scope"
            ), {"ref": constraint_ref, "scope": "quest:" + quest_ref}).first()
            if row is None:
                raise OwnerConflict("content_source_unbound")
            binding = guidance_binding_from_row(row)
            if version_ref != binding["guidance_hash"]:
                raise OwnerConflict("content_version_unbound")
            count = connection.execute(text(
                "SELECT count(*) FROM hc_guidance_effects e JOIN hc_guidance_deliveries d USING(delivery_ref) "
                "WHERE e.kind='feedback' AND d.constraint_ref=:ref AND d.guidance_hash=:version"
            ), {"ref": constraint_ref, "version": version_ref}).scalar_one()
            return {**binding, "status": row.status, "meaning": "formally_submitted_guidance",
                    "treatment_count": count, "treatment_state": "recorded" if count else "not_processed",
                    "treatments_reader": {"operation": "human_request.read", "entry_kind": "treatments",
                        "constraint_ref": constraint_ref, "version_ref": version_ref, "treatment_offset": 0},
                    "work_materials": material_submissions_for(self._database, "guidance", row.constraint_ref)}

    def query_research_guidance_treatments(self, constraint_ref, *, quest_ref, version_ref,
                                          offset=0, limit=12, query_revision=None):
        """Discover every retained feedback, including assessments later resolved."""
        from meta_research.research_pagination import bind_page, page_state
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise OwnerConflict("human_input_page_invalid")
        with self._database.read_snapshot() as connection:
            self.read_research_guidance(constraint_ref, quest_ref=quest_ref, version_ref=version_ref)
            params = {"quest": quest_ref, "ref": constraint_ref, "version": version_ref}
            selection = " AND d.constraint_ref=:ref AND d.guidance_hash=:version"
            revisions = connection.execute(text(
                "SELECT e.effect_key,e.receipt_hash" + _TREATMENT_JOIN + selection + " ORDER BY e.effect_key"
            ), params).all()
            revision = bind_page({"entry": "guidance_treatments", "quest_ref": quest_ref,
                "constraint_ref": constraint_ref, "version_ref": version_ref},
                [[row.effect_key, row.receipt_hash] for row in revisions], offset=offset,
                query_revision=query_revision)
            rows = connection.execute(text(_TREATMENT_SELECT + selection
                + " ORDER BY e.created_at DESC,e.effect_key LIMIT :limit OFFSET :offset"),
                {**params, "limit": limit + 1, "offset": offset}).all()
            items = []
            for row in rows[:limit]:
                receipt = _verified_treatment(row)
                items.append({"treatment_ref": row.effect_key,
                    "root_kind": row.root_kind, "run_ref": row.run_ref, "operation_ref": row.operation_ref,
                    "disposition": receipt["disposition"], "goal_impact": receipt["goal_impact"],
                    "created_at": row.created_at,
                    "reader": {"operation": "human_request.read", "constraint_ref": constraint_ref,
                        "treatment_ref": row.effect_key, "version_ref": row.receipt_hash},
                    "content_reader": {"source_ref": row.effect_key, "version_ref": row.receipt_hash}})
            next_offset = offset + limit if len(rows) > limit else None
        return {"items": items, "offset": offset, "next_offset": next_offset,
            "query_revision": revision, **page_state(items, next_offset, offset=offset)}

    def read_research_guidance_treatment(self, treatment_ref, *, quest_ref, version_ref, constraint_ref=None):
        with self._database.read_snapshot() as connection:
            row = connection.execute(text(_TREATMENT_SELECT + " AND e.effect_key=:ref"),
                {"quest": quest_ref, "ref": treatment_ref}).first()
            if row is None or constraint_ref is not None and constraint_ref != row.constraint_ref:
                raise OwnerConflict("content_source_unbound")
            if version_ref != row.receipt_hash:
                raise OwnerConflict("content_version_unbound")
            return _verified_treatment(row)

    def submit_research_input(self, *, quest_ref, text_content, question_ref=None, asset_bindings=(), work_materials=None, idempotency_key):
        if not isinstance(text_content,str) or not text_content.strip() or len(text_content)>65536:
            raise OwnerConflict("human_input_text_invalid")
        if not isinstance(idempotency_key,str) or not 1<=len(idempotency_key)<=128:
            raise OwnerConflict("human_input_idempotency_invalid")
        if self._research_graph.query_quest_by_ref(quest_ref) is None:
            raise OwnerConflict("human_input_quest_invalid")
        if question_ref is not None:
            question=self._research_graph.query_question_history_by_ref(question_ref)
            if question is None or question.quest_ref!=quest_ref:raise OwnerConflict("human_input_question_invalid")
        bindings=[dataset_asset_binding(b) for b in asset_bindings]
        value={"quest_ref":quest_ref,"question_ref":question_ref,"text":text_content,
               "asset_bindings":[b.as_dict() for b in bindings],"source":"explicit_user_submission"}
        if work_materials is not None:
            from meta_research.work_materials import MaterialSubmission
            submission = MaterialSubmission.parse(work_materials)
            if submission.receiver.get("kind") != "current" or submission.receiver.get("quest_ref") != quest_ref or question_ref is not None and submission.receiver.get("question_ref") != question_ref:
                raise OwnerConflict("material_receiver_invalid")
            value["material_command"] = work_materials
        digest=canonical_hash(value)
        with self._database.read() as c:
            replay=c.execute(text("SELECT input_ref,content_hash FROM hc_research_inputs WHERE idempotency_key=:key"),{"key":idempotency_key}).first()
        if replay is not None:
            if replay.content_hash!=digest:raise OwnerConflict("human_input_idempotency_conflict")
            return self.query_research_input(replay.input_ref,quest_ref=quest_ref)
        if work_materials is not None:
            self._prepare_material_submission(work_materials)
        for binding in bindings:
            self._research_memory.verify_asset_binding(asset_ref=binding.asset_ref,version_ref=binding.version_ref,content_hash=binding.content_hash,manifest_hash=binding.manifest_hash,receipt=binding.receipt)
            self._research_graph.verify_asset_quest_scope(binding.version_ref,quest_ref=quest_ref)
        if work_materials is not None:
            self._prepare_material_upload_delivery(work_materials, idempotency_key, 'input')
        with self._database.fenced_write() as c:
            row=c.execute(text("SELECT input_ref,content_hash FROM hc_research_inputs WHERE idempotency_key=:key"),{"key":idempotency_key}).first()
            if row is not None:
                if row.content_hash!=digest:raise OwnerConflict("human_input_idempotency_conflict")
                return self.query_research_input(row.input_ref,quest_ref=quest_ref)
            for binding in bindings:
                self._research_memory.verify_asset_receipt(asset_ref=binding.asset_ref,version_ref=binding.version_ref,content_hash=binding.content_hash,manifest_hash=binding.manifest_hash,receipt=binding.receipt)
                assert_asset_usable(c,binding.version_ref)
                self._research_graph.verify_asset_quest_scope(binding.version_ref,quest_ref=quest_ref)
            ref=new_ref("human_input");receipt_ref=new_ref("hc_research_input_receipt");now=time.time()
            if work_materials is not None:
                self._insert_material_submission(c, command=work_materials, key=idempotency_key,
                    anchor_kind="input", anchor_ref=ref)
            receipt_hash=canonical_hash({"input_ref":ref,"content_hash":digest,"receipt_ref":receipt_ref,"created_at":now})
            c.execute(text("INSERT INTO hc_research_inputs (input_ref,quest_ref,question_ref,payload_json,content_hash,idempotency_key,receipt_ref,receipt_hash,created_at) VALUES (:ref,:quest,:question,:payload,:hash,:key,:receipt,:receipt_hash,:now)"),
                {"ref":ref,"quest":quest_ref,"question":question_ref,"payload":canonical_json(value),"hash":digest,
                 "key":idempotency_key,"receipt":receipt_ref,"receipt_hash":receipt_hash,"now":now})
            c.execute(text("UPDATE human_collaboration_state SET revision=revision+1 WHERE singleton='owner'"))
            self._feed.record(c,"human_collaboration.research_input_submitted",{"input_ref":ref,"quest_ref":quest_ref,"question_ref":question_ref})
        return self.query_research_input(ref,quest_ref=quest_ref)

    def query_research_input(self,input_ref,*,quest_ref=None):
        return read_research_input(self._database,input_ref,quest_ref=quest_ref)

    def query_research_inputs(self,*,quest_ref,query="",offset=0,limit=12,query_revision=None):
        from meta_research.research_pagination import bind_page, page_state
        if (type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100
                or not isinstance(query, str) or len(query) > 1024):
            raise OwnerConflict("human_input_page_invalid")
        with self._database.read_snapshot() as c:
            revisions=c.execute(text("SELECT input_ref,content_hash,receipt_hash FROM hc_research_inputs "
                "WHERE quest_ref=:quest ORDER BY input_ref"),{"quest":quest_ref}).all()
            revision=bind_page({"entry":"human_inputs","quest_ref":quest_ref,"query":query},
                [list(row) for row in revisions],offset=offset,query_revision=query_revision)
            rows=c.execute(text("SELECT * FROM hc_research_inputs WHERE quest_ref=:quest AND (:query='' OR instr(lower(payload_json),lower(:query))>0) ORDER BY created_at DESC,input_ref LIMIT :limit OFFSET :offset"),
                {"quest":quest_ref,"query":query,"limit":limit+1,"offset":offset}).all()
            values=[{"input_ref": row.input_ref, "content_hash": row.content_hash,
                     **_verified_input_value(row)} for row in rows[:limit]]
        items=[{"input_ref":v["input_ref"],"question_ref":v["question_ref"],"name":v["text"][:80],
                          "summary":v["text"][:1200],"status":"accepted","reader":{"source_ref":v["input_ref"],"version_ref":v["content_hash"]}}
                         for v in values]
        next_offset=offset+limit if len(rows)>limit else None
        return {"items":items,"offset":offset,"next_offset":next_offset,"query_revision":revision,
                **page_state(items,next_offset,offset=offset,query=query)}
