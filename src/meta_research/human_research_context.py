"""Bounded human research advice, readable from any authenticated Quest root."""
from __future__ import annotations

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
        if not isinstance(quest_ref, str) or not quest_ref:
            raise OwnerConflict("research_help_quest_unavailable")
        request_ref = arguments.get("request_ref")
        response_ref = arguments.get("response_ref")
        if request_ref is None:
            if response_ref is not None:
                raise OwnerConflict("research_help_response_unbound")
            page = human_collaboration.query_research_help_page(
                quest_ref=quest_ref, cursor=arguments.get("cursor"))
            inputs = human_collaboration.query_research_inputs(quest_ref=quest_ref,offset=arguments.get("input_offset",0),limit=12,query=arguments.get("query",""))
            value = {"research_inputs":inputs,"schema_ref": "meta-research/human-research-context/v1",
                     "summary_only": True, "items": [_request_summary(item) for item in page["items"]],
                     "next_cursor": page["next_cursor"], "selection": "recent_quest_requests"}
            source_ref = quest_ref
        else:
            if arguments.get("cursor") is not None:
                raise OwnerConflict("research_help_cursor_invalid")
            request = human_collaboration.query_human_request(request_ref)
            if request is None or request.get("quest_ref") != quest_ref:
                raise OwnerConflict("research_help_request_unbound")
            value = {key: request.get(key) for key in (
                "request_ref", "quest_ref", "kind", "obligation", "business_purpose",
                "target_assertion", "acceptance_conditions", "required_authorization",
                "status", "current", "predecessor_request_ref", "successor_request_ref")}
            value["request_content_hash"] = _request_content_hash(request)
            if response_ref is None:
                value["responses"] = [_response_summary(response, request_ref)
                                      for response in request.get("responses", [])]
            else:
                response = next((item for item in request.get("responses", [])
                                 if item.get("response_ref") == response_ref), None)
                if response is None:
                    raise OwnerConflict("research_help_response_unbound")
                value["response"] = response
                value["response_content_hash"] = canonical_hash(response)
                evaluation = request.get("evaluation") or {}
                value["used_by_owner_evaluation"] = response_ref in evaluation.get("response_refs", [])
                value["owner_disposition"] = (request.get("disposition") or {}).get("decision")
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
                       "response_ref": response["response_ref"]}}


def _request_summary(request):
    responses = request.get("responses", [])
    return {"request_ref": request["request_ref"], "kind": request["kind"],
            "status": request["status"], "current": request.get("current"),
            "request_content_hash": _request_content_hash(request),
            "obligation": bounded_text(request["obligation"], 512),
            "business_purpose": bounded_text(request["business_purpose"], 1024),
            "response_count": len(responses),
            "latest_response": _response_summary(responses[-1], request["request_ref"]) if responses else None,
            "reader": {**human_research_reader(), "request_ref": request["request_ref"]}}


def human_research_context_operation(*, agent_runtime, human_collaboration):
    return SemanticOperation(
        semantic_operation_id=HUMAN_RESEARCH_READ_OPERATION,
        owning_module="human_collaboration",
        description=("读取当前 Quest 跨阶段、Target 和 Cycle 的人类输入。列表模式同时返回最多 12 条请求摘要和最多 12 条主动提交材料；"
                     "先沿 next_offset 读完当前 UTF-8 字节页，再用 next_cursor 翻旧请求；主动材料用 query 检索，"
                     "按 research_inputs.next_offset 设置 input_offset 独立翻页。切换任一列表页时将字节 offset 归零。"
                     "主动材料的 reader 交给 research_memory.content.read；精确请求使用 request_ref，"
                     "精确回复同时提供 request_ref 和 response_ref，并省略 cursor。offset／limit 按 UTF-8 字节重建原文与 hash。"
                     "有精确来源的专业意见可支持研究判断，执行授权按对应 Owner 事实核验。"),
        input_schema={"type": "object", "properties": {
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
