"""Invoke a configured content model; Companion owns research and file delivery.

META_RESEARCH_CONTENT_MODELS names a private JSON config file. Each named entry
contains endpoint (a complete chat/completions URL), model and api_key_env.
Only the explicit request file is sent. No directory or conversation discovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # A configured endpoint never delegates its credential to another URL.
        return None


def _open_content_model(request, *, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def generate(config_ref: str, request: dict, *, environment=None, opener=None) -> str:
    env = os.environ if environment is None else environment
    opener = _open_content_model if opener is None else opener
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config_ref):
        raise ValueError("content_model_configuration_invalid")
    config_path = env.get("META_RESEARCH_CONTENT_MODELS")
    if not config_path:
        raise ValueError("content_model_configuration_unavailable")
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))[config_ref]
        endpoint, model, key_name = config["endpoint"], config["model"], config["api_key_env"]
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or not isinstance(model, str) or not model.strip()
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name)):
            raise ValueError()
        key = env[key_name]
        if not key:
            raise ValueError()
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise ValueError("content_model_configuration_unavailable") from None
    messages = request.get("messages")
    if (set(request) != {"messages"} or not isinstance(messages, list) or not messages
            or any(not isinstance(item, dict) or set(item) != {"role", "content"}
                or item["role"] not in {"system", "user", "assistant"}
                or not isinstance(item["content"], str) for item in messages)):
        raise ValueError("content_model_request_invalid")
    body = json.dumps({"model": model, "messages": messages}).encode("utf-8")
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("content_model_request_too_large")
    call = urllib.request.Request(endpoint, data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
    try:
        with opener(call, timeout=180) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        result = json.loads(raw)["choices"][0]["message"]["content"]
        if not isinstance(result, str) or not result.strip():
            raise ValueError()
        return result
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError):
        # Provider error bodies may contain credentials or submitted content.
        raise ValueError("content_model_call_failed") from None




def content_model_operation(agent_runtime):
    """The daemon retains credentials; the native assistant receives only text."""
    from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
    def call(context, arguments):
        agent_runtime.verify_root_agent_runtime_scope(root_kind=context.root_kind,
            run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        try:
            result = generate(arguments["config_ref"], {"messages": arguments["messages"]})
        except ValueError as error:
            raise SemanticMcpError(str(error)) from None
        return {"config_ref": arguments["config_ref"], "content": result}
    return SemanticOperation(semantic_operation_id="assistant.content.generate",
        owning_module="agent_runtime", access_mode="read",
        description="调用用户本次明确选择的已配置内容模型。仅发送本次任务所需、已授权的消息和材料；不发送整个Quest或目录。config_ref是受限配置引用，凭据不会返回。失败如实反馈且不换模型；输出文本由原助手继续核对、修订和制作文件。此调用不改变研究状态。",
        input_schema={"type": "object", "properties": {
            "config_ref": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
            "messages": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                "type": "object", "properties": {
                    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                    "content": {"type": "string", "maxLength": 1000000}},
                "required": ["role", "content"], "additionalProperties": False}}},
            "required": ["config_ref", "messages"], "additionalProperties": False},
        output_schema={"type": "object"}, handler=call)
