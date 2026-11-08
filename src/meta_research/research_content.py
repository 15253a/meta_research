"""Exact shared reading through the accepting Owners; no materialized copies."""
from __future__ import annotations

import json

from sqlalchemy import text

from meta_research.context_presentation import ExactContextPages, context_read_page
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
from meta_research.research_pagination import bind_page, page_state
from meta_research.accepted_materials import DeepFetchAcceptedMaterials


def _quest(agent_runtime, context):
    scope = agent_runtime.verify_root_agent_runtime_scope(
        root_kind=context.root_kind, run_ref=context.run_ref, attempt_ref=context.attempt_ref,
        root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
        runtime_binding_hash=context.capability_binding_hash)
    if not scope.get("quest_ref"):
        raise OwnerConflict("content_quest_scope_required")
    return scope["quest_ref"]


def _reading_quest(agent_runtime, context, *, source_quest_ref=None, human_collaboration=None):
    """Page selection is context, not the limit of an authorized Companion read.

    Reuse the existing receipt-bound HC authority on each cross-Quest read.
    This function is deliberately separate from the scope used for effects.
    """
    current = _quest(agent_runtime, context)
    if source_quest_ref is None or source_quest_ref == current:
        return current
    if context.root_kind != "companion" or human_collaboration is None:
        raise OwnerConflict("content_quest_not_authorized")
    authorization = human_collaboration.query_broad_research_authorization(source_quest_ref)
    if (authorization is None or authorization.get("quest_ref") != source_quest_ref
            or authorization.get("is_current") is not True
            or authorization.get("effective_decision") != "granted"):
        raise OwnerConflict("content_quest_not_authorized")
    return source_quest_ref


def read_content(graph, memory, *, quest_ref, source_ref, version_ref, **arguments):
    page = _read_content(graph, memory, quest_ref=quest_ref,
                         source_ref=source_ref, version_ref=version_ref, **arguments)
    if source_ref.startswith("human_input_"):
        # Its adoption binding was projected from the same verified read.
        return page
    # Return the Owner's exact adoption identity once the source is read. A
    # discovery title or a work-folder path never creates this authority.
    leaf = graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest_ref, ref=source_ref)
    if leaf is not None and leaf.get("kind") in {"AssetVersion", "HumanInput", "ScientificOutcome", "LiteratureSnapshot"}:
        page["selected_evidence_binding"] = adopted_source_binding(
            graph, quest_ref=quest_ref, source_ref=source_ref, leaf=leaf)
    return page


def adopted_source_binding(graph, *, quest_ref, source_ref, leaf=None):
    """Project an existing accepting Owner identity and its accurate reader."""
    if leaf is None:
        leaf = graph.resolve_reasoning_historical_evidence_leaf(quest_ref=quest_ref, ref=source_ref)
    if leaf is None or leaf.get("kind") not in {"AssetVersion", "HumanInput", "ScientificOutcome", "LiteratureSnapshot"}:
        raise OwnerConflict("plan_evidence_source_invalid")
    kind = leaf["kind"]
    binding = (graph.verify_asset_quest_scope(source_ref, quest_ref=quest_ref).as_dict()
               if kind == "AssetVersion" else leaf)
    version_ref = leaf["content_hash"] if kind == "HumanInput" else source_ref
    return {"evidence_ref": source_ref, "source_kind": kind, "source_ref": source_ref,
            "source_binding": binding,
            "reader": {"operation": "research_memory.content.read",
                       "args": {"source_ref": source_ref, "version_ref": version_ref}}}


def _read_content(graph, memory, *, quest_ref, source_ref, version_ref, entry_path=None, offset=0, limit=8192, human_collaboration=None, page_cache=None, reader_source_quest_ref=None):
    def exact_page(value):
        return context_read_page(value, path=[], offset=offset, limit=min(limit, 16384),
            cache=page_cache, identity=(quest_ref, source_ref, version_ref, 'canonical-json', reader_source_quest_ref))
    if source_ref.startswith("target_commit_"):
        # The same accurate reader remains usable by downstream roles. The
        # accepting Owners resolve the actual commit and exact retained version.
        _, catalog = graph.query_plan_evidence_catalog(quest_ref=quest_ref,
            target_commit_refs=(source_ref,), current_only=False)
        binding = next((item for item in catalog if item["asset_version_ref"] == version_ref), None)
        if binding is None:
            raise OwnerConflict("content_version_unbound")
        from meta_research.target_commit_evidence import target_adoption_binding
        page = memory.read_asset_content_page(version_ref, entry_path=entry_path, offset=offset, limit=limit)
        return {**page, "source_ref": source_ref, "version_ref": version_ref,
                "selected_evidence_binding": target_adoption_binding(binding)}
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
        from meta_research.human_research_input import read_research_input, research_input_leaf
        value=read_research_input(memory._database,source_ref,quest_ref=quest_ref)
        if value is None or value["content_hash"]!=version_ref:raise OwnerConflict("content_version_unbound")
        # The verified body hash does not cover the live material projection.
        # Receipt identity covers the remaining envelope (including created_at).
        identity = (quest_ref, source_ref, version_ref, 'canonical-json',
                    value["receipt"]["payload_hash"], canonical_hash(value["work_materials"]))
        page = context_read_page(value, path=[], offset=offset, limit=min(limit,16384),
                                 cache=page_cache, identity=identity)
        return {**page, "source_ref": source_ref, "version_ref": version_ref,
                "selected_evidence_binding": adopted_source_binding(graph, quest_ref=quest_ref,
                    source_ref=source_ref, leaf=research_input_leaf(value))}
    if human_collaboration is not None:
        with memory._database.read_snapshot() as c:
            response=c.execute(text("SELECT request_ref FROM hc_human_request_responses WHERE response_ref=:ref"),{"ref":source_ref}).first()
        if response is not None:
            selected = human_collaboration.query_research_help_response(
                response.request_ref,source_ref,quest_ref=quest_ref)
            value = selected["response"]
            if canonical_hash(value)!=version_ref:raise OwnerConflict("content_version_unbound")
            return {**exact_page(value),"source_ref":source_ref,"version_ref":version_ref}
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
        return {**exact_page(value),
                "source_ref":source_ref,"version_ref":version_ref}
    with memory._database.read_snapshot() as connection:
        outcome=connection.execute(text("SELECT outcome_ref,transition_json FROM rg_reasoning_outcome_decisions WHERE (outcome_ref=:ref OR scientific_outcome_ref=:ref) AND decision='accepted'"),{"ref":source_ref}).first()
        if outcome is not None:
            import json
            transition=json.loads(outcome.transition_json)
            if version_ref != source_ref or transition.get("source_quest_ref") != quest_ref:
                raise OwnerConflict("content_version_unbound")
            value=graph.read_question_scientific_outcome(quest_ref=quest_ref,question_ref=transition["source_question_ref"],outcome_ref=source_ref)
            return {**exact_page(value),"source_ref":source_ref,"version_ref":version_ref}
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
    value = read_stage_content(memory, quest_ref=quest_ref, source_ref=source_ref, version_ref=version_ref,
        reader_source_quest_ref=reader_source_quest_ref)
    if value is not None:
        return {**exact_page(value),"source_ref":source_ref,"version_ref":version_ref}
    raise OwnerConflict("content_source_unbound")


_STAGE_CONTENT = (("rm_idea_outcome_contents", "query_idea_outcome_content", "outcome"),
                  ("rm_plan_documents", "query_plan_document", "plan_document"),
                  ("rm_reasoning_contents", "query_reasoning_content", "scientific_outcome"))


def read_stage_content(memory, *, quest_ref, source_ref, version_ref, reader_source_quest_ref=None):
    from meta_research.historical_research import read_historical_stage_content
    historical = read_historical_stage_content(memory, quest_ref=quest_ref,
        source_ref=source_ref, version_ref=version_ref)
    if historical is not None:
        if reader_source_quest_ref is not None and "content_reader" in historical:
            historical["content_reader"] = {**historical["content_reader"], "source_quest_ref": reader_source_quest_ref}
        return historical
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


def discover_authorized_quests(graph, human_collaboration, *, query="", offset=0, limit=12, query_revision=None):
    """Page the existing HC authority; no assistant-owned permission records."""
    with graph._database.read_snapshot() as connection:
        rows = connection.execute(text(
            "SELECT a.quest_ref,a.authorization_ref,a.revision,a.decision,q.goal_json, "
            "o.authorization_ref AS override_ref,o.revision AS override_revision "
            "FROM hc_capability_authorizations a JOIN rg_quests q ON q.quest_ref=a.quest_ref "
            "LEFT JOIN hc_capability_authorizations o ON o.authorization_ref=(SELECT latest.authorization_ref "
            "FROM hc_capability_authorizations latest WHERE latest.authorization_kind='capability' "
            "AND latest.scope_ref='quest:' || a.quest_ref AND latest.capability='broad_research' "
            "ORDER BY latest.revision DESC LIMIT 1) "
            "WHERE a.authorization_kind='broad_research' AND a.is_current=1 "
            "AND COALESCE(o.decision,a.decision)='granted' "
            "AND (:query='' OR instr(lower(q.goal_json),lower(:query))>0) "
            "ORDER BY a.quest_ref"), {"query": query}).all()
        revision = bind_page({"entry": "authorized_quests", "query": query, "limit": limit},
            [[row.quest_ref, row.authorization_ref, row.revision, row.override_ref, row.override_revision] for row in rows],
            offset=offset, query_revision=query_revision)
        items = []
        for row in rows[offset:offset + limit]:
            authority = human_collaboration.query_broad_research_authorization(row.quest_ref)
            if (authority is None or authority["authorization_ref"] != row.authorization_ref or not authority["is_current"]
                    or authority.get("effective_decision") != "granted"):
                raise OwnerConflict("research_query_changed")
            items.append({"quest_ref": row.quest_ref, "goal": json.loads(row.goal_json).get("goal", ""),
                "reader": {"operation": "research_graph.questions.page", "args": {"source_quest_ref": row.quest_ref}}})
    next_offset = offset + limit if offset + limit < len(rows) else None
    return {"items": items, "offset": offset, "next_offset": next_offset, "query_revision": revision,
        **page_state(items, next_offset, offset=offset, query=query)}


def _reader(value, source_quest_ref):
    return value if source_quest_ref is None else {**value, "source_quest_ref": source_quest_ref}


def discover_questions(graph, memory, *, quest_ref, query="", offset=0, limit=12, query_revision=None, source_quest_ref=None):
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100:
        raise OwnerConflict("research_library_page_invalid")
    with graph._database.read_snapshot() as c:
        revision = c.execute(text("SELECT question_ref,status,updated_at FROM rg_question_lifecycle "
            "WHERE quest_ref=:quest ORDER BY question_ref"), {"quest": quest_ref}).all()
        revision = bind_page({"entry": "questions", "quest_ref": quest_ref, "query": query, "limit": limit},
            [list(row) for row in revision], offset=offset, query_revision=query_revision)
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
                "reader":_reader({"source_ref":q.question_ref,"version_ref":q.content_ref}, source_quest_ref),
                "history_reader":_reader({"operation":"research_graph.question_history.read","question_ref":q.question_ref,"offset":0,"limit":12}, source_quest_ref)})
    next_offset = offset + limit if len(rows) > limit else None
    return {"items":items,"offset":offset,"next_offset":next_offset,"query_revision":revision,
            **page_state(items,next_offset,offset=offset,query=query)}


def discover_literature(graph, memory, *, quest_ref, query="", offset=0, limit=12, record_ref=None, query_revision=None, source_quest_ref=None):
    if type(offset)is not int or offset<0 or type(limit)is not int or not 1<=limit<=100:
        raise OwnerConflict("research_library_page_invalid")
    with memory._database.read_snapshot() as c:
        revisions = c.execute(text("SELECT revision_ref,revision_number FROM rm_question_literature_revisions "
            "WHERE quest_ref=:quest ORDER BY revision_ref"), {"quest": quest_ref}).all()
        judgment_revisions = []
        for table, _, _ in _STAGE_CONTENT:
            judgment_revisions.extend(list(row) for row in c.execute(text(
                f"SELECT a.content_ref,a.payload_hash FROM {table} a "
                "JOIN ae_stage_run_requests r ON r.request_ref=a.request_ref "
                "WHERE r.quest_ref=:quest ORDER BY a.content_ref"), {"quest":quest_ref}).all())
        ordinary_revisions = c.execute(text("SELECT ref,payload_hash FROM rg_literature_facts WHERE quest_ref=:quest ORDER BY ref"), {"quest":quest_ref}).all()
        query_revision_token = bind_page({"entry":"literature","quest_ref":quest_ref,"query":query,
            "record_ref":record_ref,"limit":limit},[list(row) for row in revisions]+judgment_revisions+[list(row) for row in ordinary_revisions],
            offset=offset,query_revision=query_revision)
        rows=c.execute(text("""SELECT * FROM (
            SELECT r.revision_ref,r.question_ref,json_extract(e.value,'$.ref') AS record_ref,
                   r.accepted_at, NULL AS literature_ref
            FROM rm_question_literature_revisions r,json_each(r.records_json) e
            WHERE r.quest_ref=:quest AND (:record IS NULL OR json_extract(e.value,'$.ref')=:record)
            AND r.revision_number=(SELECT max(x.revision_number) FROM rm_question_literature_revisions x WHERE x.question_ref=r.question_ref)
            UNION ALL
            SELECT ref,NULL,json_extract(payload_json,'$.record_ref'),accepted_at,ref
            FROM rg_literature_facts WHERE quest_ref=:quest AND kind='register'
            AND (:record IS NULL OR json_extract(payload_json,'$.record_ref')=:record))
            ORDER BY accepted_at DESC,revision_ref,record_ref LIMIT :limit OFFSET :offset"""),
            {"quest":quest_ref,"record":record_ref,"limit":limit+1,"offset":offset}).all()
        items=[]
        for row in rows[:limit]:
            if row.literature_ref is not None:
                paper = graph.query_literature(row.literature_ref, quest_ref=quest_ref)
                matches = not query or query.casefold() in json.dumps(paper,ensure_ascii=False).casefold()
                uses = c.execute(text("SELECT ref FROM rg_literature_facts WHERE quest_ref=:quest AND kind='reference' "
                    "AND json_extract(payload_json,'$.literature_ref')=:ref ORDER BY accepted_at,ref"),
                    {"quest":quest_ref,"ref":row.literature_ref}).scalars().all()
                judgments = []
                for ref in uses:
                    usage = graph.query_literature(ref, quest_ref=quest_ref)
                    if query and not matches:
                        matches = any(query.casefold() in str(usage.get(field) or "").casefold()
                            for field in ("question_ref", "research_ref", "purpose", "reading_summary"))
                    readers = [_reader({"source_ref":b["version_ref"],"version_ref":b["version_ref"]}, source_quest_ref) for b in usage["reading_bindings"]]
                    target = graph.literature_reading_target(usage["research_ref"])
                    if readers and target is not None:
                        from meta_research.research_notes import query_target_research_notes
                        for reader in readers:
                            note = query_target_research_notes(memory._database,memory,target,version_ref=reader["version_ref"])[0]
                            if note["entry_path"] is not None:
                                reader["entry_path"] = note["entry_path"]
                    judgments.append({"reference_ref":ref,"question_ref":usage["question_ref"],
                        "research_ref":usage["research_ref"],"purpose":usage["purpose"][:1200],"summary":usage["reading_summary"][:1200],
                        "reader":_reader({"operation":"research_memory.literature.read","literature_ref":ref}, source_quest_ref),
                        "readers":readers})
                if not matches:
                    continue
                items.append({"record_ref":paper["record_ref"],"literature_ref":paper["ref"],"name":paper["title"],
                    "source":paper["source"][:1200],"fulltext_status":paper["fulltext_status"],
                    "reader":_reader({"operation":"research_memory.literature.read","literature_ref":paper["ref"]}, source_quest_ref),
                    "readers":[_reader({"source_ref":b["version_ref"],"version_ref":b["version_ref"]}, source_quest_ref) for b in paper["asset_bindings"]],
                    "judgments":judgments})
                continue
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
                        "reader":_reader({"source_ref":source.content_ref,"version_ref":source.payload_hash}, source_quest_ref)})
            items.append({"record_ref":row.record_ref,"question_ref":row.question_ref,"name":paper.get("title",row.record_ref),
                "summary":str(paper.get("abstract") or paper.get("summary") or "")[:1200],
                "reader":_reader({"source_ref":row.record_ref,"version_ref":row.revision_ref}, source_quest_ref),"judgments":judgments})
    next_offset = offset + limit if len(rows) > limit else None
    return {"items":items,"offset":offset,"next_offset":next_offset,"query_revision":query_revision_token,
            **page_state(items,next_offset,offset=offset,query=query)}


def research_content_operations(*, research_graph, research_memory, agent_runtime, human_collaboration=None,
                                target_run_agent=None):
    from meta_research.asset_lifecycle_operations import asset_lifecycle_operations
    page_cache = ExactContextPages()
    initialization_materials = DeepFetchAcceptedMaterials(agent_runtime, human_collaboration, research_memory)
    def invoke(context, arguments):
        try:
            if context.phase == "creation_materials":
                if "source_quest_ref" in arguments:
                    raise OwnerConflict("content_quest_not_authorized")
                operation = human_collaboration._creation_material_operation(context)
                operation.authorize(context)
                work = operation.work
                if work.read_basis is None or arguments["source_ref"] != arguments["version_ref"]:
                    raise OwnerConflict("content_source_unbound")
                basis = research_memory.creation_bases.query(work.read_basis["basis_ref"], work.read_basis["basis_hash"])
                if not work.read_basis_historical:
                    research_memory.creation_bases.require_current(basis)
                version = arguments["version_ref"]
                snapshot = work.read_literature
                page_arguments = {key: value for key, value in arguments.items() if key not in {"source_ref", "version_ref"}}
                allowed_snapshots = (*work.read_inherited_literature, *((snapshot,) if snapshot is not None else ()))
                accepted_snapshot = next((item for item in allowed_snapshots if version == item["snapshot_ref"]), None)
                if accepted_snapshot is not None:
                    if research_memory.query_literature_snapshot(version).snapshot_hash != accepted_snapshot["snapshot_hash"]:
                        raise OwnerConflict("creation_literature_unbound")
                    return {**research_memory.read_literature_content_page(version, **page_arguments),
                        "source_ref": version, "version_ref": version}
                if version not in {item["binding"]["version_ref"] for item in basis["sources"] if item["binding"] is not None}:
                    raise OwnerConflict("content_source_unbound")
                return research_memory.read_asset_content_page(version, **page_arguments)
            arguments = dict(arguments)
            selected_quest = arguments.pop("source_quest_ref", None)
            try:
                quest_ref = _reading_quest(agent_runtime, context,
                    source_quest_ref=selected_quest, human_collaboration=human_collaboration)
            except OwnerConflict as error:
                if error.code != "content_quest_scope_required" or context.root_kind != "deepfetch":
                    raise
                if selected_quest is not None:
                    arguments["source_quest_ref"] = selected_quest
                return initialization_materials.read(context, arguments)
            result = read_content(research_graph,research_memory,quest_ref=quest_ref,human_collaboration=human_collaboration,page_cache=page_cache,
                reader_source_quest_ref=selected_quest, **arguments)
            if selected_quest is not None:
                exact_reader = result.get("selected_evidence_binding", {}).get("reader")
                if exact_reader is not None:
                    exact_reader["args"] = _reader(exact_reader["args"], selected_quest)
                history_reader = result.get("lifecycle", {}).get("history_reader")
                if history_reader is not None:
                    history_reader["args"] = _reader(history_reader["args"], selected_quest)
            return result
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error
    operations=[SemanticOperation(semantic_operation_id="research_memory.content.read",owning_module="research_memory",
        description="Read an exact authorized source using its returned source_ref/version_ref. Before Quest creation, DeepFetch may read only its current HC request's accepted input versions using the supplied version/version reader. Companion may explicitly select source_quest_ref outside the page selection when current HC authority grants it; other roots retain their current Quest scope. For source-specific pagination and errors, read this operation's tools.contracts.read contract. Reading is reference access; execution inputs remain explicitly bound by their Owner.",
        return_description="Serialized JSON uses UTF-8 byte offsets and ends at next_offset=null; complete only means one page contains the entire source. Asset file pages use byte offsets (binary is base64) and end at complete=true. Asset directory pages use entry offsets and end at next_offset=null. Assets stream from managed or linked_local custody without making an export. A hash names complete digital content, not a physical resource or its verified condition. Preserve returned exact source bindings and readers.",
        error_descriptions={
            "content_version_unbound": "This exact source/version is not bound within the authorized scope; this does not mean all research content is absent.",
            "content_source_unbound": "The supplied source has no readable binding in the current scope.",
            "content_quest_not_authorized": "The requested Quest is not currently authorized for this Companion read; page visibility grants no permission.",
            "content_receipt_invalid": "The stored stage source does not match its accepted receipt; do not substitute latest content.",
        },
        input_schema={"type":"object","properties":{
            "source_quest_ref":{"type":"string","minLength":1,"maxLength":256},
            "source_ref":{"type":"string","minLength":1,"maxLength":1024},
            "version_ref":{"type":"string","minLength":1,"maxLength":1024},
            "entry_path":{"type":"string","minLength":1,"maxLength":4096},
            "offset":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":1,"maximum":65536}},
            "required":["source_ref","version_ref"],"additionalProperties":False},
        output_schema={"type":"object"},handler=invoke)]
    for name,reader in (("research_graph.questions.page",discover_questions),("research_memory.literature.page",discover_literature)):
        def discover(context,arguments,reader=reader):
            try:
                arguments = dict(arguments)
                entry_kind = arguments.pop("entry_kind", "questions")
                if entry_kind == "quests":
                    _quest(agent_runtime, context)
                    if context.root_kind != "companion" or human_collaboration is None or "source_quest_ref" in arguments:
                        raise OwnerConflict("content_quest_not_authorized")
                    return discover_authorized_quests(research_graph, human_collaboration, **arguments)
                selected_quest = arguments.pop("source_quest_ref", None)
                quest_ref = _reading_quest(agent_runtime, context,
                    source_quest_ref=selected_quest, human_collaboration=human_collaboration)
                return reader(research_graph, research_memory, quest_ref=quest_ref,
                    source_quest_ref=selected_quest, **arguments)
            except OwnerConflict as error:raise SemanticMcpError(error.code) from error
        properties={"source_quest_ref":{"type":"string","minLength":1,"maxLength":256}, "query":{"type":"string","maxLength":1024},"offset":{"type":"integer","minimum":0},
                    "limit":{"type":"integer","minimum":1,"maximum":100},
                    "query_revision":{"type":"string","pattern":"^[0-9a-f]{64}$"}}
        if reader is discover_questions:properties["entry_kind"]={"type":"string","enum":["questions","quests"]}
        if reader is discover_literature:properties["record_ref"]={"type":"string","minLength":1,"maxLength":1024}
        operations.append(SemanticOperation(semantic_operation_id=name,owning_module=name.split('.')[0],
            description="Discover accepted sources by research clues. For questions.page, Companion can first select entry_kind=quests to page current HC-authorized Quest identities. Companion may set source_quest_ref for another currently HC-authorized Quest; other roots retain the current Quest. Follow each reader with research_memory.content.read. Continue next_offset with the returned query_revision and unchanged filters/limit; research_query_changed requires restarting at offset=0. An empty page_pending is an unfinished scan, not no_match. Literature also links accepted Question-specific judgments to their exact content.",
            input_schema={"type":"object","properties":properties,"additionalProperties":False},output_schema={"type":"object"},handler=discover))
    return (*operations, *asset_lifecycle_operations(
        research_graph, research_memory, agent_runtime, target_run_agent, human_collaboration
    ))
