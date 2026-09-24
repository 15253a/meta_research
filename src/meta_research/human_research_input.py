"""Explicit, authenticated human research input; ordinary chat stays chat."""
from __future__ import annotations
import json
import time
from sqlalchemy import text
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json, new_ref
from meta_research.dataset_contract import dataset_asset_binding


def read_research_input(database, input_ref, *, quest_ref=None):
    with database.read_snapshot() as c:
        row=c.execute(text("SELECT * FROM hc_research_inputs WHERE input_ref=:ref"),{"ref":input_ref}).first()
        if row is None or quest_ref is not None and row.quest_ref!=quest_ref:return None
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
        return {"input_ref":row.input_ref,**value,"content_hash":row.content_hash,"created_at":row.created_at,
                "receipt":{"issuer":"human_collaboration","kind":"research_input","receipt_ref":row.receipt_ref,
                           "subject_ref":row.input_ref,"payload_hash":row.receipt_hash}}


class HumanResearchInputMixin:
    def submit_research_input(self, *, quest_ref, text_content, question_ref=None, asset_bindings=(), idempotency_key):
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
        digest=canonical_hash(value)
        with self._database.fenced_write() as c:
            row=c.execute(text("SELECT input_ref,content_hash FROM hc_research_inputs WHERE idempotency_key=:key"),{"key":idempotency_key}).first()
            if row is not None:
                if row.content_hash!=digest:raise OwnerConflict("human_input_idempotency_conflict")
                return self.query_research_input(row.input_ref,quest_ref=quest_ref)
            for binding in bindings:
                self._research_memory.verify_asset_binding(asset_ref=binding.asset_ref,version_ref=binding.version_ref,content_hash=binding.content_hash,manifest_hash=binding.manifest_hash,receipt=binding.receipt)
                self._research_graph.verify_asset_quest_scope(binding.version_ref,quest_ref=quest_ref)
            ref=new_ref("human_input");receipt_ref=new_ref("hc_research_input_receipt");now=time.time()
            receipt_hash=canonical_hash({"input_ref":ref,"content_hash":digest,"receipt_ref":receipt_ref,"created_at":now})
            c.execute(text("INSERT INTO hc_research_inputs (input_ref,quest_ref,question_ref,payload_json,content_hash,idempotency_key,receipt_ref,receipt_hash,created_at) VALUES (:ref,:quest,:question,:payload,:hash,:key,:receipt,:receipt_hash,:now)"),
                {"ref":ref,"quest":quest_ref,"question":question_ref,"payload":canonical_json(value),"hash":digest,
                 "key":idempotency_key,"receipt":receipt_ref,"receipt_hash":receipt_hash,"now":now})
            c.execute(text("UPDATE human_collaboration_state SET revision=revision+1 WHERE singleton='owner'"))
            self._feed.record(c,"human_collaboration.research_input_submitted",{"input_ref":ref,"quest_ref":quest_ref,"question_ref":question_ref})
        return self.query_research_input(ref,quest_ref=quest_ref)

    def query_research_input(self,input_ref,*,quest_ref=None):
        return read_research_input(self._database,input_ref,quest_ref=quest_ref)

    def query_research_inputs(self,*,quest_ref,query="",offset=0,limit=12):
        if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100:
            raise OwnerConflict("human_input_page_invalid")
        with self._database.read_snapshot() as c:
            rows=c.execute(text("SELECT input_ref FROM hc_research_inputs WHERE quest_ref=:quest AND (:query='' OR instr(lower(payload_json),lower(:query))>0) ORDER BY created_at DESC,input_ref LIMIT :limit OFFSET :offset"),
                {"quest":quest_ref,"query":query,"limit":limit+1,"offset":offset}).all()
            values=[self.query_research_input(row.input_ref,quest_ref=quest_ref) for row in rows[:limit]]
        return {"items":[{"input_ref":v["input_ref"],"question_ref":v["question_ref"],"name":v["text"][:80],
                          "summary":v["text"][:1200],"status":"accepted","reader":{"source_ref":v["input_ref"],"version_ref":v["content_hash"]}}
                         for v in values],"offset":offset,"next_offset":offset+limit if len(rows)>limit else None}
