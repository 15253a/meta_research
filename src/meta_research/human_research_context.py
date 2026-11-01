"""Bounded human research advice, readable from any authenticated Quest root."""
from __future__ import annotations

from meta_research.research_content import _reading_quest
from meta_research.context_presentation import bounded_text, context_read_page
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation

HUMAN_RESEARCH_READ_OPERATION = "human_request.read"


def human_research_reader() -> dict[str, object]:
    return {"operation": HUMAN_RESEARCH_READ_OPERATION, "offset": 0, "limit": 8192}


def read_human_research_context(agent_runtime, human_collaboration, context, arguments):
    """The root determines the Quest; callers may select only an exact request."""
    try:
        scope = agent_runtime.verify_root_agent_runtime_scope(
            root_kind=context.root_kind, run_ref=context.run_ref,
            attempt_ref=context.attempt_ref, root_session_ref=context.root_session_ref,
            fence_ref=context.fence_ref, runtime_binding_hash=context.capability_binding_hash)
        quest_ref = scope.get("quest_ref")
        if "source_quest_ref" in arguments:
            quest_ref = _reading_quest(agent_runtime, context, source_quest_ref=arguments["source_quest_ref"],
                human_collaboration=human_collaboration)
        request_ref = arguments.get("request_ref")
        response_ref = arguments.get("response_ref")
        constraint_ref = arguments.get("constraint_ref")
        if constraint_ref is not None:
            if request_ref is not None or response_ref is not None or arguments.get("cursor") is not None:
                raise OwnerConflict("research_help_selection_invalid")
            if not quest_ref:
                raise OwnerConflict("content_source_unbound")
            value = human_collaboration.read_research_guidance(
                constraint_ref, quest_ref=quest_ref, version_ref=arguments.get("version_ref"))
            source_ref = constraint_ref
        elif arguments.get("entry_kind") == "guidance":
            if not quest_ref or request_ref is not None or response_ref is not None or arguments.get("cursor") is not None:
                raise OwnerConflict("research_help_selection_invalid")
            value = human_collaboration.query_research_guidance(quest_ref=quest_ref,
                query=arguments.get("query", ""), offset=arguments.get("guidance_offset", 0),
                query_revision=arguments.get("query_revision"))
            source_ref = quest_ref
        elif arguments.get("entry_kind") == "responses":
            if not quest_ref or request_ref is None or response_ref is not None or arguments.get("cursor") is not None:
                raise OwnerConflict("research_help_selection_invalid")
            value = human_collaboration.query_research_response_page(quest_ref=quest_ref,
                request_ref=request_ref, offset=arguments.get("response_offset", 0),
                query_revision=arguments.get("query_revision"))
            value = {**value, "items": [_response_summary(response, request_ref) for response in value["items"]]}
            source_ref = request_ref
        elif request_ref is None:
            if not isinstance(quest_ref, str) or not quest_ref:
                raise OwnerConflict("research_help_request_required")
            if response_ref is not None:
                raise OwnerConflict("research_help_response_unbound")
            page = human_collaboration.query_research_help_page(
                quest_ref=quest_ref, cursor=arguments.get("cursor"),
                **({"query": arguments["query"]} if arguments.get("query") else {}))
            inputs = human_collaboration.query_research_inputs(quest_ref=quest_ref,
                offset=arguments.get("input_offset",0),limit=12,query=arguments.get("query",""),
                **({"query_revision": arguments["input_query_revision"]} if "input_query_revision" in arguments else {}))
            value = {"research_inputs":inputs,"schema_ref": "meta-research/human-research-context/v1",
                     "summary_only": True, "items": [_request_summary(item) for item in page["items"]],
                     "formal_guidance_reader": {"operation": HUMAN_RESEARCH_READ_OPERATION,
                                                "entry_kind": "guidance"},
                     "next_cursor": page["next_cursor"], "selection": "recent_quest_requests"}
            for key in ("query_revision", "result_state", "scan_complete"):
                if key in page:
                    value[key] = page[key]
            source_ref = quest_ref
        else:
            if arguments.get("cursor") is not None:
                raise OwnerConflict("research_help_cursor_invalid")
            request = (human_collaboration.query_research_help_request(request_ref, quest_ref=quest_ref)
                       if quest_ref else human_collaboration.query_human_request(request_ref, materialize_expiration=False))
            if request is None or request.get("quest_ref") != quest_ref:
                raise OwnerConflict("research_help_request_unbound")
            if not quest_ref:
                binding = (request.get("open_effect") or {}).get("operation_binding") or {}
                if binding.get("task_ref") != context.run_ref or binding.get("root_session_ref") != context.root_session_ref:
                    raise OwnerConflict("research_help_request_unbound")
            if response_ref is None and arguments.get("version_ref") is not None:
                if arguments["version_ref"] != _request_content_hash(request):
                    raise OwnerConflict("content_version_unbound")
            value = {key: request.get(key) for key in (
                "request_ref", "quest_ref", "kind", "obligation", "business_purpose",
                "target_assertion", "acceptance_conditions", "required_authorization",
                "status", "current", "predecessor_request_ref", "successor_request_ref")}
            value["request_content_hash"] = _request_content_hash(request)
            if response_ref is None:
                if quest_ref:
                    value["response_count"] = request["response_count"]
                    value["responses_reader"] = request["responses_reader"]
                else:
                    value["responses"] = [_response_summary(response, request_ref)
                                          for response in request.get("responses", [])]
            else:
                response_context = (human_collaboration.query_research_help_response(
                    request_ref, response_ref, quest_ref=quest_ref) if quest_ref else request)
                response = (response_context["response"] if quest_ref else
                    next((item for item in request.get("responses", []) if item.get("response_ref") == response_ref), None))
                if response is None:
                    raise OwnerConflict("research_help_response_unbound")
                if arguments.get("version_ref") is not None and arguments["version_ref"] != canonical_hash(response):
                    raise OwnerConflict("content_version_unbound")
                value["response"] = response
                value["response_content_hash"] = canonical_hash(response)
                evaluation = response_context.get("evaluation") or {}
                value["used_by_owner_evaluation"] = response_ref in evaluation.get("response_refs", [])
                value["owner_disposition"] = (response_context.get("disposition") or {}).get("decision")
            source_ref = response_ref or request_ref
        chunk = context_read_page(value, path=[], offset=arguments.get("offset", 0),
                                  limit=arguments.get("limit", 8192))
    except (OwnerConflict, ValueError) as error:
        raise SemanticMcpError(str(error)) from error
    return {key: item for key, item in {
        **chunk, "quest_ref": quest_ref, "source_ref": source_ref,
        "next_cursor": value.get("next_cursor")}.items() if item is not None}


def _request_content_hash(request):
    return canonical_hash({key: request.get(key) for key in (
        "request_ref", "quest_ref", "kind", "obligation", "business_purpose",
        "target_assertion", "acceptance_conditions", "required_authorization")})


def _response_summary(response, request_ref):
    return {"response_ref": response["response_ref"], "decision": response.get("decision"),
            "note": bounded_text(response.get("note", ""), 1024),
            "facts": bounded_text(response.get("facts", {}), 512),
            "response_content_hash": canonical_hash(response),
            "reader": {**human_research_reader(), "request_ref": request_ref,
                       "response_ref": response["response_ref"], "version_ref": canonical_hash(response)}}


def _request_summary(request):
    responses = request.get("responses", [])
    return {"request_ref": request["request_ref"], "kind": request["kind"],
            "status": request["status"], "current": request.get("current"),
            "request_content_hash": _request_content_hash(request),
            "obligation": bounded_text(request["obligation"], 512),
            "business_purpose": bounded_text(request["business_purpose"], 1024),
            "response_count": request.get("response_count", len(responses)),
            "latest_response": _response_summary(responses[-1], request["request_ref"]) if responses else None,
            "responses_reader": {"operation": HUMAN_RESEARCH_READ_OPERATION, "entry_kind": "responses",
                                 "request_ref": request["request_ref"], "response_offset": 0},
            "reader": {**human_research_reader(), "request_ref": request["request_ref"],
                       "version_ref": _request_content_hash(request)}}


def human_research_context_operation(*, agent_runtime, human_collaboration):
    return SemanticOperation(
        semantic_operation_id=HUMAN_RESEARCH_READ_OPERATION,
        owning_module="human_collaboration",
        description=("Companion may select another HC-authorized source_quest_ref for reading; carry that selector when following returned readers. 读取当前 Quest 跨阶段、Target 和 Cycle 的人类输入。列表模式同时返回最多 12 条请求摘要和最多 12 条主动提交材料；"
                     "先沿 next_offset 读完当前 UTF-8 字节页，再用 next_cursor 翻旧请求；query 检索请求研究说明和主动材料，"
                     "按 research_inputs.next_offset 设置 input_offset，并携带 research_inputs.query_revision 为 input_query_revision 独立翻页。"
                     "正式指导沿 formal_guidance_reader 的 entry_kind=guidance 发现；以 guidance_offset 和 query_revision 续页，"
                     "constraint_ref/version_ref 纯读已提交原文、力度、作用范围及当前状态，不产生已读回执或把助手讨论作为指导。"
                     "页间研究事实变化返回 research_query_changed，重启该范围查询。切换任一列表页时将字节 offset 归零。"
                     "主动材料的 reader 交给 research_memory.content.read；精确请求使用 request_ref，"
                     "精确回复同时提供 request_ref 和 response_ref，携带 reader 的 version_ref 并省略 cursor。offset／limit 按 UTF-8 字节重建原文与 hash。"
                     "请求目录只附最新回复；沿 responses_reader 的 entry_kind=responses 及 request_ref 查询全部历史回复，"
                     "以 response_offset 和 query_revision 独立续页。每条回复提供准确 reader，目录不准备请求的完整执行生命周期。"
                     "正式回复的 delivery.reply_reader／uploaded_readers 参数交给 research_workspace.read，linked_locators 用原生工具按原 locator 有界读取。无 Quest 的原根须提供精确 request_ref／response_ref。人类交付是工作材料，按 Agent 判断再正式入库或登记。执行授权按对应 Owner 事实核验。"),
        input_schema={"type": "object", "properties": {"source_quest_ref": {"type": "string", "minLength": 1, "maxLength": 256},
            "entry_kind": {"type": "string", "enum": ["guidance", "responses"]},
            "guidance_offset": {"type": "integer", "minimum": 0},
            "response_offset": {"type": "integer", "minimum": 0},
            "constraint_ref": {"type": "string", "minLength": 1, "maxLength": 96},
            "version_ref": {"type": "string", "minLength": 1, "maxLength": 256},
            "query_revision": {"type": "string", "minLength": 1, "maxLength": 256},
            "input_query_revision": {"type": "string", "minLength": 1, "maxLength": 256},
            "input_offset": {"type":"integer","minimum":0},
            "query": {"type":"string","maxLength":1024},
            "cursor": {"type": "string", "maxLength": 256},
            "request_ref": {"type": "string", "minLength": 1, "maxLength": 96},
            "response_ref": {"type": "string", "minLength": 1, "maxLength": 96},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 16384}},
            "additionalProperties": False},
        output_schema={"type": "object", "properties": {
            "text": {"type": "string"}, "encoding": {"type": "string"},
            "content_hash": {"type": "string"},
            "path": {"type": "array", "items": {"type": "string"}},
            "offset": {"type": "integer"}, "offset_unit": {"type": "string"},
            "returned_bytes": {"type": "integer"}, "total_bytes": {"type": "integer"},
            "next_offset": {"type": "integer"}, "complete": {"type": "boolean"},
            "quest_ref": {"type": "string"}, "source_ref": {"type": "string"},
            "next_cursor": {"type": "string"}}, "additionalProperties": False},
        handler=lambda context, arguments: read_human_research_context(
            agent_runtime, human_collaboration, context, arguments))
