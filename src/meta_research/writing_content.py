"""Invoke the selected text protocol. Companion owns research and file delivery."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from pydantic import ValidationError

from meta_research.companion_writing import ContentModel, validate_content_endpoint


class ContentModelError(ValueError):
    """Stable failure code with diagnostics safe to return to the caller."""

    def __init__(self, reason_code: str, category: str, message: str, correction: str,
                 **context: object) -> None:
        super().__init__("content_model_call_failed")
        self.details = {"reason_code": reason_code, "category": category,
                        "message": message, "correction": correction, **context}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # A configured endpoint never delegates its credential to another URL.
        return None


def _open_content_model(request, *, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _configuration(selection, env, credentials, quest_ref):
    if isinstance(selection, str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", selection):
            raise ValueError("content_model_configuration_invalid")
        try:
            config = json.loads(Path(env["META_RESEARCH_CONTENT_MODELS"]).read_text(encoding="utf-8"))[selection]
            endpoint, model = config["endpoint"], config["model"]
            validate_content_endpoint(endpoint)
            key = env[config["api_key_env"]]
            protocol = config.get("protocol", "openai_chat_completions")
            if protocol not in {"openai_chat_completions", "openai_responses", "anthropic_messages"}:
                raise ValueError()
            if (not isinstance(model, str) or not model.strip() or not isinstance(key, str) or not key
                    or any(ord(c) < 32 or ord(c) > 126 for c in key)
                    or (config.get("instructions") is not None and not isinstance(config["instructions"], str))):
                raise ValueError()
            return endpoint, model, protocol, key, config.get("instructions")
        except (KeyError, OSError, TypeError, ValueError):
            raise ValueError("content_model_configuration_unavailable") from None
    try:
        selected = ContentModel.model_validate(selection)
    except ValueError:
        raise ValueError("content_model_configuration_invalid") from None
    if selected.kind != "task_api":
        raise ValueError("content_model_configuration_invalid")
    key = None if credentials is None else credentials.resolve(selected.api_key_ref,
        quest_ref=quest_ref, endpoint=selected.endpoint, model=selected.model)
    if key is None:
        try:
            entry = json.loads(Path(env["META_RESEARCH_CONTENT_CREDENTIALS"]).read_text(encoding="utf-8"))[selected.api_key_ref]
        except (KeyError, OSError, TypeError, ValueError):
            raise ValueError("content_model_credential_unavailable") from None
        if (not isinstance(entry, dict) or not isinstance(entry.get("endpoints"), list)
                or selected.endpoint not in entry["endpoints"]
                or any(name in entry and not isinstance(entry[name], list) for name in ("models", "quest_refs"))
                or ("models" in entry and selected.model not in entry["models"])
                or ("quest_refs" in entry and quest_ref not in entry["quest_refs"])):
            raise ValueError("content_model_credential_scope_denied")
        try:
            key = env[entry["api_key_env"]]
        except (KeyError, TypeError):
            raise ValueError("content_model_credential_unavailable") from None
    if not isinstance(key, str) or not key or any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise ValueError("content_model_credential_unavailable")
    return selected.endpoint, selected.model, selected.protocol, key, selected.instructions


def _response_text(value, protocol):
    if protocol == "openai_chat_completions":
        choice = value["choices"][0]
        if choice.get("finish_reason") not in {None, "stop"} or choice["message"].get("refusal"):
            raise ValueError()
        return choice["message"]["content"]
    if protocol == "openai_responses":
        if value.get("status") != "completed" or value.get("error"):
            raise ValueError()
        blocks = [block for item in value["output"] if item.get("type") == "message" for block in item["content"]]
        if any(block.get("type") != "output_text" for block in blocks):
            raise ValueError()
        return "\n".join(block["text"] for block in blocks)
    if value.get("stop_reason") != "end_turn":
        raise ValueError()
    if any(block.get("type") != "text" for block in value["content"]):
        raise ValueError()
    return "\n".join(block["text"] for block in value["content"])


def generate(config_ref: str | dict, request: dict, *, environment=None, opener=None,
             credentials=None, quest_ref=None) -> str:
    env = os.environ if environment is None else environment
    opener = _open_content_model if opener is None else opener
    endpoint, model, protocol, key, instructions = _configuration(config_ref, env, credentials, quest_ref)
    messages = request.get("messages")
    if (set(request) != {"messages"} or not isinstance(messages, list) or not messages
            or any(not isinstance(item, dict) or set(item) != {"role", "content"}
                or item["role"] not in {"system", "user", "assistant"}
                or not isinstance(item["content"], str) for item in messages)):
        raise ValueError("content_model_request_invalid")
    messages = list(messages)
    if instructions:
        messages.insert(0, {"role": "system", "content": instructions})
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if protocol == "openai_responses":
        payload = {"model": model, "input": messages}
    elif protocol == "anthropic_messages":
        payload = {"model": model, "max_tokens": 8192,
            "messages": [item for item in messages if item["role"] != "system"]}
        system = "\n\n".join(item["content"] for item in messages if item["role"] == "system")
        if system:
            payload["system"] = system
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
    else:
        payload = {"model": model, "messages": messages}
    body = json.dumps(payload).encode("utf-8")
    if len(body) > 8 * 1024 * 1024:
        raise ValueError("content_model_request_too_large")
    call = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    try:
        with opener(call, timeout=180) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024:
            raise ValueError()
        result = _response_text(json.loads(raw), protocol)
        if not isinstance(result, str) or not result.strip() or key in result:
            raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        error.close()
        if error.code == 401:
            raise ContentModelError("authentication_failed", "authorization",
                "The provider rejected the configured credentials.",
                "Ask the configuration owner to repair credentials; keep the draft and selected model.",
                http_status=error.code) from None
        if error.code == 403:
            reason, category, message, correction = ("authorization_denied", "authorization",
                "The selected provider denied access to this model.",
                "Ask the configuration owner to restore model permission; keep the draft.")
        elif error.code == 429:
            reason, category, message, correction = ("rate_limited", "temporary_unavailable",
                "The provider's rate or quota limit was reached.",
                "Check rate/quota limits before another call; keep the draft and selected model.")
        elif error.code in {408, 504}:
            reason, category, message, correction = ("timeout", "temporary_unavailable",
                "The selected provider or its gateway timed out.",
                "Outcome unknown: check the service before another call; keep existing output and model choice.")
        elif error.code >= 500:
            reason, category, message, correction = ("service_unavailable", "temporary_unavailable",
                "The selected provider returned a server failure.",
                "Check the selected service's availability before another call; retain the draft and selected model.")
        elif 300 <= error.code < 400:
            reason, category, message, correction = ("endpoint_redirect_rejected", "tool_contract",
                "The configured endpoint redirected the request; credentials were not forwarded.",
                "Ask the configuration owner to verify the exact endpoint; do not send credentials to the redirect location.")
        elif error.code in {404, 405, 410, 415}:
            reason, category, message, correction = ("configuration_rejected", "tool_contract",
                "The endpoint, model, method or media type was rejected.",
                "Ask the configuration owner to fix the selected endpoint/model contract; changing messages cannot fix it.")
        elif error.code in {400, 413, 422}:
            reason, category, message, correction = ("request_rejected", "submission",
                "The selected provider rejected the content request.",
                "Check the submitted messages and the selected model's supported request contract; preserve the scientific evidence.")
        else:
            reason, category, message, correction = ("request_rejected", "provider_rejection",
                "The provider rejected the call; its HTTP status does not establish a repairable submission error.",
                "Check the selected provider's documented status and configuration before deciding whether the submission can be corrected; preserve the current draft.")
        raise ContentModelError(reason, category, message, correction, http_status=error.code) from None
    except (urllib.error.URLError, OSError) as error:
        timed_out = isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError)
        raise ContentModelError("timeout" if timed_out else "unreachable", "temporary_unavailable",
            "The selected provider timed out." if timed_out else "The selected provider connection failed.",
            "The remote generation outcome is unknown. Check the connection/service before another call; preserve existing draft output and model choice.",
            outcome="unknown") from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Never include provider bodies, submitted messages or credentials.
        raise ContentModelError("invalid_response", "tool_contract",
            "The selected provider did not return complete, nonempty text in the selected protocol (including refusal or truncated output).",
            "Ask the configuration owner to check the selected endpoint's response contract; keep existing draft output.") from None




def content_model_operation(agent_runtime, human_collaboration=None, credentials=None):
    """The daemon retains credentials; the native assistant receives only text."""
    from meta_research.semantic_mcp import SemanticMcpError, SemanticOperation
    def call(context, arguments):
        scope = agent_runtime.verify_root_agent_runtime_scope(root_kind=context.root_kind,
            run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        try:
            if "config_ref" in arguments and "model" in arguments:
                raise ValueError("content_model_selection_mismatch")
            selection = arguments.get("model", arguments.get("config_ref"))
            bound = (human_collaboration.query_companion_writing_model(context.run_ref)
                if context.root_kind == "companion" and human_collaboration is not None else None)
            if bound is not None:
                chosen = ContentModel.model_validate(bound)
                if chosen.kind == "current":
                    raise ValueError("content_model_selection_mismatch")
                expected = chosen.config_ref if chosen.kind == "configured_api" else chosen.model_dump(exclude_none=True)
                if selection is not None:
                    actual = ContentModel.model_validate(selection).model_dump(exclude_none=True) if isinstance(selection, dict) else selection
                    if actual != expected:
                        raise ValueError("content_model_selection_mismatch")
                selection = expected
            if selection is None:
                raise ValueError("content_model_configuration_invalid")
            result = generate(selection, {"messages": arguments["messages"]},
                credentials=credentials, quest_ref=scope.get("quest_ref"))
        except ValidationError:
            raise SemanticMcpError("content_model_configuration_invalid") from None
        except ValueError as error:
            raise SemanticMcpError(str(error), getattr(error, "details", None)) from None
        # Cancellation/recovery can rotate the fence while a provider responds.
        # Its external work may have happened; never deliver it under a stale task.
        agent_runtime.verify_root_agent_runtime_scope(root_kind=context.root_kind,
            run_ref=context.run_ref, attempt_ref=context.attempt_ref,
            root_session_ref=context.root_session_ref, fence_ref=context.fence_ref,
            runtime_binding_hash=context.capability_binding_hash)
        return {**({"config_ref": selection} if isinstance(selection, str) else {"model": selection}), "content": result}
    return SemanticOperation(semantic_operation_id="assistant.content.generate",
        owning_module="agent_runtime", access_mode="read",
        description="调用用户选择的写作内容模型。普通Writing只传messages，沿本原生会话持久绑定的模型；研究内传config_ref或task_api的model对象。支持OpenAI Chat Completions、Responses和Anthropic Messages文本协议，地址须为完整端点；instructions为非秘密内容要求，不执行自定义代码/headers。仅发送本次所需授权材料，不附加目录或历史。凭据只由受限引用解析。失败按details.reason_code/correction处理，超时或结果未知先查服务和已有输出，不能盲重试或换模型。文本仍须核对修订并制作文件。",
        input_schema={"type": "object", "properties": {
            "config_ref": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
            "model": {"type": "object", "properties": {
                "kind": {"type": "string", "enum": ["task_api"]},
                "endpoint": {"type": "string", "maxLength": 2048},
                "model": {"type": "string", "minLength": 1, "maxLength": 256},
                "protocol": {"type": "string", "enum": ["openai_chat_completions", "openai_responses", "anthropic_messages"]},
                "api_key_ref": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,64}$"},
                "instructions": {"type": ["string", "null"], "maxLength": 12000}},
                "required": ["kind", "endpoint", "model", "protocol", "api_key_ref"], "additionalProperties": False},
            "messages": {"type": "array", "minItems": 1, "maxItems": 100, "items": {
                "type": "object", "properties": {
                    "role": {"type": "string", "enum": ["system", "user", "assistant"]},
                    "content": {"type": "string", "maxLength": 1000000}},
                "required": ["role", "content"], "additionalProperties": False}}},
            "required": ["messages"], "additionalProperties": False},
        output_schema={"type": "object"}, handler=call,
        error_descriptions={
            "content_model_selection_mismatch": "The call does not match the user's original Writing selection. Keep the same model and task; do not substitute a model or credential. A new choice requires the user to start a Writing request with that choice.",
            "content_model_credential_unavailable": "No usable restricted credential is available. No provider call ran. Add or repair the credential through the Writing form or restricted environment, preserving task/model and draft.",
            "content_model_credential_scope_denied": "This credential does not authorize this Quest, exact endpoint or model. No provider call ran. Obtain an authorized reference for this selection; do not change destinations to bypass it.",
            "content_model_configuration_invalid": "config_ref must name the selected model using 1–64 letters, digits, underscores or hyphens; do not substitute another model.",
            "content_model_configuration_unavailable": "config_ref has no usable configuration; no provider call ran. Verify the reference or ask the configuration owner to restore META_RESEARCH_CONTENT_MODELS, endpoint/model and api_key_env; keep the draft.",
            "content_model_request_invalid": "messages must be nonempty, with only role (system/user/assistant) and text content. Correct the message shape, preserving evidence.",
            "content_model_request_too_large": "The model/messages JSON exceeds 8 MiB. Send smaller authorized excerpts or split the writing task; retain the draft and original evidence.",
        })
