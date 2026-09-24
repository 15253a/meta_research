"""Environment indexing through the existing scoped Root owner gateway."""
from meta_research.owners.common import OwnerConflict
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation


def _scope(agent_runtime, context, *, reconcile=False):
    if context.root_kind is None:
        raise OwnerConflict("environment_root_scope_required")
    verify = (agent_runtime.verify_root_agent_human_request_reconcile_scope if reconcile
              else agent_runtime.verify_root_agent_runtime_scope)
    scope = verify(root_kind=context.root_kind, run_ref=context.run_ref, attempt_ref=context.attempt_ref,
                   root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
                   runtime_binding_hash=context.capability_binding_hash)
    if not scope.get("quest_ref"):
        raise OwnerConflict("environment_quest_scope_required")
    return scope


def environment_operations(graph, agent_runtime):
    string = {"type": "string", "minLength": 1, "maxLength": 1024}
    schemas = {
        "register": ({"semantic_key": string, "name": string, "source": string,
                      "meaning": {"type": "string", "minLength": 1, "maxLength": 65536},
                      "metadata": {"type": "object"}, "source_environment_ref": string,
                      "asset_bindings": {"type": "array", "maxItems": 256, "items": {"type": "object"}}},
                     ["semantic_key", "name", "meaning", "source"]),
        "reference": ({"environment_ref": string, "question_ref": string, "research_ref": string,
                       "purpose": {"type": "string", "maxLength": 65536}},
                      ["environment_ref", "question_ref"]),
    }
    descriptions = {
        "register": "登记当前 Quest 可复用的 Environment 含义快照。source 写真实来源、持久路径、安装/服务位置或设备设施身份与位置；metadata 保留能力和已知条件。现成资源可直接登记，无需构建 Target、打包或重建配方，asset_bindings 可空。本 Target 新建或改动的成果先完成正式交接后登记。数字材料使用当前 Quest 已正式接纳的 RM 精确 binding；说明 hash 不代表实体或服务被冻结或核验。semantic_key 由 Agent 决定；相同内容复用，不同内容形成新的不可变 environment_ref，旧引用不漂移。适配工作用 source_environment_ref 关联原环境，沿既有 Baseline 工作保存。",
        "reference": "关联当前 Quest 的 Question 对已知精确 Environment 索引的用途，research_ref 可关联本 Quest 已有 Baseline/Variant/VariantRun 等实际工作。可引用其他 Quest 的环境；其每个数字 binding 须已在当前 Quest 正式接纳，无数字 binding 的设备设施可直接引用。成功引用后本 Quest 可发现和读取该索引。Target 新产物在正式接纳后与 Dataset 同一整理环节登记。用途记录不表示资源已可用、权限已授予或运行条件已更改；索引不替代实际执行输入绑定或本次选卡与预算配置。",
    }

    def effect(context, arguments, action, reconcile):
        try:
            key = context.environment_effect_key(arguments["effect_id"])
            if reconcile:
                _scope(agent_runtime, context, reconcile=True)
                result = graph.reconcile_environment_operation(operation=action, idempotency_key=key)
                return {"status": "not_found" if result is None else "accepted", "result": result}
            scope = _scope(agent_runtime, context)
            payload = {name: value for name, value in arguments.items() if name != "effect_id"}
            if action == "register":
                payload["quest_ref"] = scope["quest_ref"]

            def effect_scope():
                current = _scope(agent_runtime, context)
                if current["quest_ref"] != scope["quest_ref"]:
                    raise OwnerConflict("environment_quest_scope_invalid")
                if action == "reference":
                    question = graph.query_question_history_by_ref(payload["question_ref"])
                    if question is None or question.quest_ref != current["quest_ref"]:
                        raise OwnerConflict("environment_question_scope_invalid")
                    graph.verify_environment_quest_scope(payload["environment_ref"], quest_ref=current["quest_ref"])

            handler = graph.register_environment if action == "register" else graph.reference_environment
            result = handler(**payload, idempotency_key=key, effect_scope=effect_scope)
            return {"status": "accepted", "result": result}
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    def read(context, arguments, page):
        try:
            quest_ref = _scope(agent_runtime, context)["quest_ref"]
            if page:
                return graph.query_environments(**arguments, quest_ref=quest_ref)
            if len(arguments) != 1:
                raise OwnerConflict("environment_read_ref_invalid")
            field, ref = next(iter(arguments.items()))
            handler = graph.query_environment if field == "environment_ref" else graph.query_environment_reference
            result = handler(ref, quest_ref=quest_ref)
            return {"status": "not_found" if result is None else "accepted", "result": result}
        except OwnerConflict as error:
            raise SemanticMcpError(error.code) from error

    operations = []
    for action, (properties, required) in schemas.items():
        name = "research_graph.environments." + action
        operations.append(SemanticOperation(semantic_operation_id=name, owning_module="research_graph",
            description=descriptions[action], access_mode="effect", reconciliation_operation_id=name + ".reconcile",
            input_schema={"type": "object", "properties": {**properties,
                "effect_id": {"type": "string", "minLength": 1, "maxLength": 128},
                "notes": {"type": "string", "maxLength": 65536}}, "required": ["effect_id", *required], "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments, action=action: effect(context, arguments, action, False)))
        operations.append(SemanticOperation(semantic_operation_id=name + ".reconcile", owning_module="research_graph",
            description="Read the original Environment effect after a lost response or Root recovery; keep the same effect_id.",
            access_mode="reconcile", input_schema={"type": "object", "properties": {
                "effect_id": {"type": "string", "minLength": 1, "maxLength": 128}}, "required": ["effect_id"], "additionalProperties": False},
            output_schema={"type": "object"},
            handler=lambda context, arguments, action=action: effect(context, arguments, action, True)))
    operations.append(SemanticOperation(semantic_operation_id="research_graph.environments.page", owning_module="research_graph",
        description="分页发现本 Quest 登记或使用的 Environment。query 搜索含义/来源，semantic_key 查看同资源不可变快照，environment_ref 或 question_ref 查看用途（过滤模式互斥）。数字原件用 content.read，source_ref=environment_ref，version_ref 取 asset_bindings。物理设备、服务或现成目录可仅有资源说明，hash不证明实体状态。",
        input_schema={"type": "object", "properties": {"query": {"type": "string", "maxLength": 1024},
            "semantic_key": string, "environment_ref": string, "question_ref": string,
            "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False}, output_schema={"type": "object"},
        handler=lambda context, arguments: read(context, arguments, True)))
    operations.append(SemanticOperation(semantic_operation_id="research_graph.environments.read", owning_module="research_graph",
        description="Read an exact Environment index snapshot or its research reference in this Quest; supply exactly one ref.",
        input_schema={"type": "object", "properties": {"environment_ref": string, "environment_reference_ref": string},
            "additionalProperties": False}, output_schema={"type": "object"},
        handler=lambda context, arguments: read(context, arguments, False)))
    return tuple(operations)
