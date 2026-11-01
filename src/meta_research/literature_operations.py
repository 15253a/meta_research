"""Scoped ordinary Literature effects reuse RG/RM and Root effect reconciliation."""
from meta_research.environment_operations import _scope
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


def literature_operations(graph, agent_runtime):
    string = {"type":"string","minLength":1,"maxLength":65536}
    bindings = {"type":"array","items":{"type":"object"}}
    schemas = {
        "register": ({"record_ref":string,"title":string,"source":string,
            "fulltext_status":{"type":"string","enum":["obtained","missing","waiting_user"]},
            "asset_bindings":bindings,"metadata":{"type":"object"}},
            ["record_ref","title","source","fulltext_status"]),
        "reference": ({"literature_ref":string,"question_ref":string,"purpose":string,
            "research_ref":string,"reading_summary":{"type":"string","maxLength":65536},"reading_bindings":bindings},
            ["literature_ref","question_ref","purpose"]),
    }

    def invoke(context, arguments, action, reconcile=False):
        try:
            scope = _scope(agent_runtime, context, reconcile=reconcile)
            key = "mcp-literature:" + canonical_hash({"run_ref":context.run_ref,
                "root_session_ref":context.root_session_ref,"root_kind":context.root_kind,
                "action":action,"effect_id":arguments["effect_id"]})
            if reconcile:
                result = graph.reconcile_literature_operation(operation=action,idempotency_key=key)
                if result is not None and result["quest_ref"] != scope["quest_ref"]:
                    raise OwnerConflict("literature_quest_scope_invalid")
            else:
                def verify():
                    if _scope(agent_runtime, context)["quest_ref"] != scope["quest_ref"]:
                        raise OwnerConflict("literature_quest_scope_invalid")
                result = getattr(graph, action + "_literature")(
                    **{k:v for k,v in arguments.items() if k != "effect_id"}, quest_ref=scope["quest_ref"],
                    idempotency_key=key,effect_scope=verify)
            return {"status":"not_found" if result is None else "accepted","result":result}
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    def read(context, arguments):
        try:
            quest = _scope(agent_runtime,context)["quest_ref"]
            result = graph.query_literature(arguments["literature_ref"],quest_ref=quest)
            return {"status":"not_found" if result is None else "accepted","result":result}
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    operations=[]
    for action,(properties,required) in schemas.items():
        name="research_memory.literature."+action
        operations.append(SemanticOperation(semantic_operation_id=name,owning_module="research_graph",
            description=("Register an ordinary paper's external source and exact accepted RM originals. obtained requires originals; missing/waiting_user retain honest limitations. Does not claim reading or create a DeepFetch run. "
                if action == "register" else "Associate a registered paper with Question/research purpose and actual reading summary/accepted note assets. Keep external authorship separate from this reading work. Target reading assets must belong to the accepted Target. ") +
                "Use the same effect_id to reconcile unknown effects. Empty or valueless candidates need no record.",
            access_mode="effect",reconciliation_operation_id=name+".reconcile",
            input_schema={"type":"object","properties":{**properties,"effect_id":{"type":"string","minLength":1,"maxLength":128}},
                "required":["effect_id",*required],"additionalProperties":False},output_schema={"type":"object"},
            handler=lambda c,a,action=action:invoke(c,a,action)))
        operations.append(SemanticOperation(semantic_operation_id=name+".reconcile",owning_module="research_graph",
            description="Read the original Literature effect after a lost response or native recovery; preserve effect_id.",access_mode="reconcile",
            input_schema={"type":"object","properties":{"effect_id":{"type":"string","minLength":1,"maxLength":128}},
                "required":["effect_id"],"additionalProperties":False},output_schema={"type":"object"},
            handler=lambda c,a,action=action:invoke(c,a,action,True)))
    operations.append(SemanticOperation(semantic_operation_id="research_memory.literature.read",owning_module="research_graph",
        description="Read an exact ordinary Literature record or reference. Original files and reading notes use content.read with their exact AssetVersion refs.",
        input_schema={"type":"object","properties":{"literature_ref":string},"required":["literature_ref"],"additionalProperties":False},
        output_schema={"type":"object"},handler=read))
    return tuple(operations)
