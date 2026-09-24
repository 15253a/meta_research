"""Exact shared reading through the accepting Owners; no materialized copies."""
from __future__ import annotations

import json

from sqlalchemy import text

from meta_research.context_presentation import context_read_page
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


def _quest(agent_runtime, context):
    scope = agent_runtime.verify_root_agent_runtime_scope(
        root_kind=context.root_kind, run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        runtime_binding_hash=context.capability_binding_hash)
    if not scope.get("quest_ref"):
        raise OwnerConflict("content_quest_scope_required")
    return scope["quest_ref"]


def read_content(graph, memory, *, quest_ref, source_ref, version_ref, entry_path=None, offset=0, limit=8192, human_collaboration=None):
    if source_ref.startswith("literature_snapshot_"):
        if version_ref != source_ref:
            raise OwnerConflict("content_version_unbound")
        leaf = graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest_ref, ref=source_ref)
        if leaf is None or leaf.get("kind") != "LiteratureSnapshot":
            raise OwnerConflict("content_source_unbound")
        value = memory.read_literature_content_page(source_ref, entry_path=entry_path, offset=offset, limit=limit)
        return {**value,
                "source_ref": source_ref, "version_ref": version_ref,
                "source_binding": leaf}
    if source_ref.startswith("human_input_"):
        from meta_research.human_research_input import read_research_input
        value=read_research_input(memory._database,source_ref,quest_ref=quest_ref)
        if value is None or value["content_hash"]!=version_ref:raise OwnerConflict("content_version_unbound")
        return {**context_read_page(value,path=[],offset=offset,limit=min(limit,16384)),"source_ref":source_ref,"version_ref":version_ref}
    if human_collaboration is not None:
        with memory._database.read_snapshot() as c:
            response=c.execute(text("SELECT request_ref FROM hc_human_request_responses WHERE response_ref=:ref"),{"ref":source_ref}).first()
        if response is not None:
            request=human_collaboration.query_human_request(response.request_ref)
            if request is None or request.get("quest_ref")!=quest_ref:raise OwnerConflict("content_version_unbound")
            value=next((x for x in request.get("responses",[]) if x["response_ref"]==source_ref),None)
            if value is None or canonical_hash(value)!=version_ref:raise OwnerConflict("content_version_unbound")
            return {**context_read_page(value,path=[],offset=offset,limit=min(limit,16384)),"source_ref":source_ref,"version_ref":version_ref}
    if source_ref == version_ref and memory.query_asset_version(version_ref) is not None:
        graph.verify_asset_quest_scope(version_ref, quest_ref=quest_ref)
        return memory.read_asset_content_page(version_ref, entry_path=entry_path, offset=offset, limit=limit)
    dataset = graph.query_dataset_version(source_ref, quest_ref=quest_ref)
    if dataset is not None:
        if version_ref not in {b["version_ref"] for b in dataset["asset_bindings"]}:
            raise OwnerConflict("content_version_unbound")
        graph.verify_asset_quest_scope(version_ref, quest_ref=quest_ref)
        return {**memory.read_asset_content_page(version_ref,entry_path=entry_path,offset=offset,limit=limit),"source_ref":source_ref}
    environment = graph.query_environment(source_ref, quest_ref=quest_ref)
    if environment is not None:
        if version_ref not in {binding["version_ref"] for binding in environment["asset_bindings"]}:
            raise OwnerConflict("content_version_unbound")
        graph.verify_asset_quest_scope(version_ref, quest_ref=quest_ref)
        return {**memory.read_asset_content_page(version_ref,entry_path=entry_path,offset=offset,limit=limit),"source_ref":source_ref}
    question = graph.query_question_history_by_ref(source_ref)
    if question is not None:
        if question.quest_ref != quest_ref or version_ref != question.content_ref:
            raise OwnerConflict("content_version_unbound")
        value=memory.read_question_content(question.content_ref,question.content_hash)
        return {**context_read_page(value,path=[],offset=offset,limit=min(limit,16384)),
                "source_ref":source_ref,"version_ref":version_ref}
    with memory._database.read_snapshot() as connection:
        outcome=connection.execute(text("SELECT outcome_ref,transition_json FROM rg_reasoning_outcome_decisions WHERE (outcome_ref=:ref OR scientific_outcome_ref=:ref) AND decision='accepted'"),{"ref":source_ref}).first()
        if outcome is not None:
            import json
            transition=json.loads(outcome.transition_json)
            if version_ref != source_ref or transition.get("source_quest_ref") != quest_ref:
                raise OwnerConflict("content_version_unbound")
            value=graph.read_question_scientific_outcome(quest_ref=quest_ref,question_ref=transition["source_question_ref"],outcome_ref=source_ref)
            return {**context_read_page(value,path=[],offset=offset,limit=min(limit,16384)),"source_ref":source_ref,"version_ref":version_ref}
        revision=connection.execute(text("SELECT question_ref,quest_ref FROM rm_question_literature_revisions WHERE revision_ref=:ref"),{"ref":version_ref}).first()
        if revision is not None:
            if revision.quest_ref != quest_ref:raise OwnerConflict("content_version_unbound")
            binding=memory.query_question_literature_revision_ref(question_ref=revision.question_ref,revision_ref=version_ref)
            if binding is None:raise OwnerConflict("content_version_unbound")
            record=next((r for r in binding["records"] if r["ref"]==source_ref),None)
            if record is None:raise OwnerConflict("content_source_unbound")
            page=memory.read_literature_content_page(binding["literature_snapshot_ref"], record_ref=source_ref,
                evidence_basis_ref=record["evidence_basis_ref"], entry_path=entry_path, offset=offset, limit=limit)
            paper=page.pop("paper",None) or {}
            return {**page,
                    "source_ref":source_ref,"version_ref":version_ref,"evidence_basis":record["evidence_basis"],
                    "citation_source":{"paper_ref":source_ref,"question_literature_revision_ref":version_ref,
                        "literature_snapshot_ref":binding["literature_snapshot_ref"],
                        "evidence_basis_ref":record["evidence_basis_ref"],"title":paper.get("title"),
                        "doi":paper.get("doi"),"url":paper.get("url"),
                        "location":{"unit":page.get("offset_unit","serialized_source_character"),"offset":offset,
                                    "note":"For article sections or page numbers, use locators present in the retrieved source; do not invent them."}}}
    value = read_stage_content(memory, quest_ref=quest_ref, source_ref=source_ref, version_ref=version_ref)
    if value is not None:
        return {**context_read_page(value,path=[],offset=offset,limit=min(limit,16384)),"source_ref":source_ref,"version_ref":version_ref}
    raise OwnerConflict("content_source_unbound")


_STAGE_CONTENT = (("rm_idea_outcome_contents", "query_idea_outcome_content", "outcome"),
                  ("rm_plan_documents", "query_plan_document", "plan_document"),
                  ("rm_reasoning_contents", "query_reasoning_content", "scientific_outcome"))


def read_stage_content(memory, *, quest_ref, source_ref, version_ref):
    with memory._database.read_snapshot() as connection:
        for table, method, field in _STAGE_CONTENT:
            row=connection.execute(text(f"SELECT c.submission_ref,c.payload_hash,r.quest_ref FROM {table} c "
                "JOIN ae_stage_run_requests r ON r.request_ref=c.request_ref WHERE c.content_ref=:ref"),{"ref":source_ref}).first()
            if row is None:continue
            if row.quest_ref!=quest_ref or row.payload_hash!=version_ref:raise OwnerConflict("content_version_unbound")
            accepted=getattr(memory,method)(row.submission_ref)
            if accepted is None or accepted.content_ref!=source_ref or accepted.payload_hash!=version_ref:
                raise OwnerConflict("content_receipt_invalid")
            return getattr(accepted,field)
    return None


def discover_questions(graph, memory, *, quest_ref, query="", offset=0, limit=12):
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100:
        raise OwnerConflict("research_library_page_invalid")
    with graph._database.read_snapshot() as c:
        rows=c.execute(text("""SELECT l.question_ref,l.status FROM rg_question_lifecycle l
            LEFT JOIN rg_questions a ON a.question_ref=l.question_ref
            LEFT JOIN rg_manual_questions b ON b.question_ref=l.question_ref
            LEFT JOIN rg_autonomous_questions d ON d.question_ref=l.question_ref
            LEFT JOIN rm_formal_question_contents ac ON ac.content_ref=a.content_ref
            LEFT JOIN rm_manual_question_contents bc ON bc.content_ref=b.content_ref
            LEFT JOIN rm_autonomous_question_contents dc ON dc.content_ref=d.content_ref
            WHERE l.quest_ref=:quest AND (:query='' OR instr(lower(COALESCE(ac.content_json,bc.content_json,dc.question_json)),lower(:query))>0)
            ORDER BY l.updated_at DESC,l.question_ref LIMIT :limit OFFSET :offset"""),
            {"quest":quest_ref,"query":query,"limit":limit+1,"offset":offset}).all()
        items=[]
        for row in rows[:limit]:
            q=graph.query_question_history_by_ref(row.question_ref)
            if q is None or q.quest_ref!=quest_ref:raise OwnerConflict("question_source_invalid")
            body=memory.read_question_content(q.content_ref,q.content_hash)
            items.append({"question_ref":q.question_ref,"name":body.get("title",q.question_ref),
                "summary":body.get("unknown_statement","")[:1200],"status":row.status,
                "reader":{"source_ref":q.question_ref,"version_ref":q.content_ref},
                "history_reader":{"operation":"research_graph.question_history.read","question_ref":q.question_ref,"offset":0,"limit":12}})
    return {"items":items,"offset":offset,"next_offset":offset+limit if len(rows)>limit else None}


def discover_literature(graph, memory, *, quest_ref, query="", offset=0, limit=12, record_ref=None):
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100:
        raise OwnerConflict("research_library_page_invalid")
    with memory._database.read_snapshot() as c:
        rows=c.execute(text("""SELECT r.revision_ref,r.question_ref,json_extract(e.value,'$.ref') AS record_ref
            FROM rm_question_literature_revisions r,json_each(r.records_json) e
            WHERE r.quest_ref=:quest AND (:record IS NULL OR json_extract(e.value,'$.ref')=:record)
            AND r.revision_number=(SELECT max(x.revision_number) FROM rm_question_literature_revisions x WHERE x.question_ref=r.question_ref)
            ORDER BY r.accepted_at DESC,r.revision_ref,e.key LIMIT :limit OFFSET :offset"""),
            {"quest":quest_ref,"record":record_ref,"limit":limit+1,"offset":offset}).all()
        items=[]
        for row in rows[:limit]:
            revision=memory.query_question_literature_revision_ref(question_ref=row.question_ref,revision_ref=row.revision_ref)
            if revision is None:raise OwnerConflict("literature_revision_invalid")
            snapshot=memory.read_literature_snapshot_metadata(revision["literature_snapshot_ref"])
            paper=next((p for p in snapshot["papers"] if row.record_ref=="doi:"+str(p.get("doi","")).strip().lower()
                        or row.record_ref=="url:"+canonical_hash(str(p.get("url","")).strip())[:32]),{})
            if query and query.casefold() not in json.dumps(paper,ensure_ascii=False).casefold():continue
            judgments=[]
            for table,method,field in _STAGE_CONTENT:
                document_column = "plan_document_json" if table == "rm_plan_documents" else "payload_json"
                sources=c.execute(text(f"SELECT a.content_ref,a.payload_hash,a.submission_ref,r.question_ref FROM {table} a "
                    "JOIN ae_stage_run_requests r ON r.request_ref=a.request_ref "
                    f"WHERE r.quest_ref=:quest AND instr(a.{document_column},:record)>0 ORDER BY a.accepted_at DESC LIMIT 12"),
                    {"quest":quest_ref,"record":row.record_ref}).all()
                for source in sources:
                    accepted=getattr(memory,method)(source.submission_ref)
                    if accepted is None:raise OwnerConflict("literature_judgment_source_invalid")
                    value=getattr(accepted,field)
                    judgments.append({"question_ref":source.question_ref,"summary":json.dumps(value,ensure_ascii=False)[:1200],
                        "reader":{"source_ref":source.content_ref,"version_ref":source.payload_hash}})
            items.append({"record_ref":row.record_ref,"question_ref":row.question_ref,"name":paper.get("title",row.record_ref),
                "summary":str(paper.get("abstract") or paper.get("summary") or "")[:1200],
                "reader":{"source_ref":row.record_ref,"version_ref":row.revision_ref},"judgments":judgments})
    return {"items":items,"offset":offset,"next_offset":offset+limit if len(rows)>limit else None}


def research_content_operations(*, research_graph, research_memory, agent_runtime, human_collaboration=None):
    def invoke(context, arguments):
        try:
            quest_ref=_quest(agent_runtime,context)
            return read_content(research_graph,research_memory,quest_ref=quest_ref,human_collaboration=human_collaboration,**arguments)
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
    operations=[SemanticOperation(semantic_operation_id="research_memory.content.read",owning_module="research_memory",
        description="Read an exact source found in this Quest's six research entries. Pass the returned source_ref/version_ref and continue with next_offset. Assets stream from managed or linked_local custody without making an export; text pages use byte offsets, binary pages are base64, directory entries use entry offsets. A hash names complete digital content, not a physical resource or its verified condition. Reading is reference access; execution inputs remain explicitly bound by their Owner.",
        input_schema={"type":"object","properties":{
            "source_ref":{"type":"string","minLength":1,"maxLength":1024},
            "version_ref":{"type":"string","minLength":1,"maxLength":1024},
            "entry_path":{"type":"string","minLength":1,"maxLength":4096},
            "offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":65536}},
            "required":["source_ref","version_ref"],"additionalProperties":False},
        output_schema={"type":"object"},handler=invoke)]
    for name,reader in (("research_graph.questions.page",discover_questions),("research_memory.literature.page",discover_literature)):
        def discover(context,arguments,reader=reader):
            try:return reader(research_graph,research_memory,quest_ref=_quest(agent_runtime,context),**arguments)
            except OwnerConflict as error:raise SemanticMcpError(error.code) from error
        properties={"query":{"type":"string","maxLength":1024},"offset":{"type":"integer","minimum":0},
                    "limit":{"type":"integer","minimum":1,"maximum":100}}
        if reader is discover_literature:properties["record_ref"]={"type":"string","minLength":1,"maxLength":1024}
        operations.append(SemanticOperation(semantic_operation_id=name,owning_module=name.split('.')[0],
            description="Discover accepted sources in the current Quest by research clues, including related Questions and retained history. Follow each reader with research_memory.content.read; next_offset continues the independent entry page. Literature also links accepted Question-specific judgments to their exact content.",
            input_schema={"type":"object","properties":properties,"additionalProperties":False},output_schema={"type":"object"},handler=discover))
    return tuple(operations)
