"""Observe installed CLI native tools using candidate production adapter argv.

Linux only. An empty MCP fixture and a local Responses sink isolate assembly:
the sink records only model/tools and rejects without forwarding to a model.
Pass a catalog with only the `models` array from an authenticated CLI cache;
fallback metadata silently changes tools. This is not model acceptance.
"""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import threading

SCHEMA = {"type": "object", "properties": {"result": {"type": "string"}},
          "required": ["result"], "additionalProperties": False}
PROFILES = ["idea", "plan", "bundle", "target", "target-workspace-write",
            "reasoning", "companion", "writing", "deepfetch", "deepfetch-provided-only",
            "acquisition", "acquisition-provided-only", "recorder"]


def flatten_tools(tools, namespace="", pointer="tools"):
    result = []
    for index, tool in enumerate(tools):
        source = f"{pointer}[{index}]"
        name = ".".join(filter(None, [namespace, tool.get("name", tool["type"])]))
        if tool["type"] == "namespace":
            result.extend(flatten_tools(tool["tools"], name, source + ".tools"))
        else:
            result.append({"identity": name, "type": tool["type"], "source_pointer": source, "schema": tool})
            if name == "functions.exec":
                # These are the actual executable tools declarations, not runtime
                # helpers such as text/image/store or the orchestration API.
                description = tool["description"]
                headings = list(re.finditer(r"(?m)^### `([^`]+)`\n", description))
                for position, heading in enumerate(headings):
                    end = headings[position + 1].start() if position + 1 < len(headings) else len(description)
                    section = description[heading.start():end]
                    declaration = re.search(r"```ts\n(declare const tools: \{[\s\S]*?)\n```", section)
                    method = heading.group(1)
                    if declaration and re.search(r"\b" + re.escape(method) + r"\s*\(", declaration.group(1)):
                        result.append({"identity": "tools." + method, "type": "code_mode_nested_tool",
                                       "source_pointer": source + ".description", "description_offset": heading.start(),
                                       "declaration": declaration.group(1), "documentation": section.strip()})
    return result


def adapter_arguments(source, executable, directory, role, mcp_url):
    """Reach real adapter argument boundaries without executing business code."""
    from meta_research.harness_adapters import CodexHarnessAdapter, HarnessInvocation
    from meta_research.idea_skill import CodexIdeaSkillAdapter, DEFAULT_PROVIDER_TRANSPORT_LIMITS
    from meta_research.companion import CodexCompanionAdapter
    from meta_research.plan_skill import CodexPlanSkillAdapter
    from meta_research.bundle_skill import CodexBundleSkillAdapter
    from meta_research.reasoning_skill import CodexReasoningSkillAdapter
    from meta_research.acquisition_root import _AcquisitionCodexAdapter
    from meta_research.deepfetch import CodexDeepFetchAdapter
    from meta_research.recorder_sources import RecorderChannels
    from meta_research.timeline_summary_provider import CodexTimelineSummaryAdapter
    from meta_research.root_capabilities import provided_only_cli_arguments
    from meta_research.quest_drafting import protected_codex_shell_arguments
    schema = directory / "output-schema.json"
    schema.write_text(json.dumps(SCHEMA))
    result = directory / "last-message.json"
    provided_only = role.endswith("-provided-only")
    base_role = role.removesuffix("-provided-only")
    if role.startswith("target"):
        policy = "workspace-write" if role.endswith("workspace-write") else "danger-full-access"
        previous = os.environ.get("META_RESEARCH_TARGET_SANDBOX_MODE")
        os.environ["META_RESEARCH_TARGET_SANDBOX_MODE"] = policy
        try:
            adapter = CodexHarnessAdapter(directory, executable=str(executable))
            invocation = HarnessInvocation("codex", "inventory", "inventory", "inventory", 1,
                "inventory", "inventory", "gpt-6.1-sol", "Inventory", mcp_url, "fixture",
                root_kind="target", target_workspace_ref="inventory", working_directory=str(directory))
            return adapter._argv(invocation), "CodexHarnessAdapter._argv", policy
        finally:
            if previous is None:
                os.environ.pop("META_RESEARCH_TARGET_SANDBOX_MODE", None)
            else:
                os.environ["META_RESEARCH_TARGET_SANDBOX_MODE"] = previous
    if role == "recorder":
        channels = RecorderChannels()
        channels.url = mcp_url
        adapter = CodexTimelineSummaryAdapter(directory, executable=str(executable), channels=channels)
        argv = adapter._provider_argv(schema, result, native_session_ref=None, ephemeral=False)
        return argv, "CodexTimelineSummaryAdapter._provider_argv", adapter._sandbox_mode
    if base_role == "deepfetch":
        adapter = CodexDeepFetchAdapter(directory, executable=str(executable))
        argv = adapter._durable_argv(job_ref="inventory", runtime_binding_hash="0" * 64,
            schema_path=schema, result_path=result, native_session_ref=None, mcp_url=mcp_url)
        if provided_only:
            argv = provided_only_cli_arguments(argv, writable_root=directory, resident_mcp=True)
        argv = protected_codex_shell_arguments(argv)
        return argv, "CodexDeepFetchAdapter._durable_argv + production access overlays", argv[argv.index("--sandbox") + 1]

    class Captured(BaseException):
        def __init__(self, argv):
            self.argv = argv

    def capture(argv, *unused):
        raise Captured(argv)

    classes = {"idea": CodexIdeaSkillAdapter, "plan": CodexPlanSkillAdapter,
               "bundle": CodexBundleSkillAdapter, "reasoning": CodexReasoningSkillAdapter,
               "companion": CodexCompanionAdapter, "writing": CodexCompanionAdapter,
               "acquisition": _AcquisitionCodexAdapter}
    adapter = classes[base_role](directory, executable=str(executable), process_runner=capture)
    try:
        adapter._invoke_once(model_ref="gpt-6.1-sol", directory=directory / "operation",
            prompt="Inventory", schema=SCHEMA, native_session_ref=None, job_ref=None,
            stdout_path=None, invocation_hash=None, transport_limits=DEFAULT_PROVIDER_TRANSPORT_LIMITS,
            mcp_url=mcp_url, mcp_token="fixture", mcp_scope_binding_hash="0" * 64,
            semantic_mcp_protected_environment=True,
            execution_access_mode="provided_only" if provided_only else None,
            execution_write_root=directory)
    except Captured as captured:
        return captured.argv, type(adapter).__name__ + "._invoke_once", captured.argv[captured.argv.index("--sandbox") + 1]
    raise AssertionError("adapter did not reach CLI boundary")


def make_server(captures):
    class Sink(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_error(405)

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            if self.path == "/mcp":
                if "id" not in value:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = ({"protocolVersion": value["params"]["protocolVersion"], "capabilities": {"tools": {}},
                           "serverInfo": {"name": "empty-native-inventory-fixture", "version": "1"}}
                          if value["method"] == "initialize" else {"tools": []})
                payload, status = {"jsonrpc": "2.0", "id": value["id"], "result": result}, 200
            else:
                captures.append({"model": value.get("model"), "tools": value.get("tools", []),
                                 "wire_keys": list(value), "wire_path": self.path,
                                 "additional_tools": [{"pointer": f"input[{i}].tools", "tools": item["tools"]}
                                                      for i, item in enumerate(value.get("input", [])) if item.get("type") == "additional_tools"],
                                 "tool_choice": value.get("tool_choice"), "parallel_tool_calls": value.get("parallel_tool_calls")})
                payload, status = {"error": {"type": "invalid_request_error", "code": "inventory_sink_complete",
                                             "message": "Local inventory captured; no model request forwarded."}}, 400
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-head", required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--model-catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roles", nargs="+", default=PROFILES)
    options = parser.parse_args()
    sys.path.insert(0, str(options.source / "src"))
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    captures, results = [], []
    server = make_server(captures)
    try:
        for role in options.roles:
            directory = output / role
            directory.mkdir(exist_ok=True)
            home = directory / "codex-home"
            home.mkdir(exist_ok=True)
            argv, builder, sandbox = adapter_arguments(options.source, options.executable, directory, role,
                                                       f"http://127.0.0.1:{server.server_port}/mcp")
            configs = ['model_provider="inventory"', 'model_providers.inventory.name="Local inventory sink"',
                "model_providers.inventory.base_url=" + json.dumps(f"http://127.0.0.1:{server.server_port}/v1"),
                'model_providers.inventory.wire_api="responses"', 'model_providers.inventory.env_key="INVENTORY_TEST_KEY"',
                "model_providers.inventory.request_max_retries=0", "model_providers.inventory.stream_max_retries=0"]
            if role != "recorder":
                configs.append("model_catalog_json=" + json.dumps(str(options.model_catalog)))
            argv[-1:-1] = [part for value in configs for part in ("--config", value)]
            before = len(captures)
            env = dict(os.environ, CODEX_HOME=str(home), INVENTORY_TEST_KEY="nonsecret-local-inventory",
                       META_RESEARCH_MCP_TOKEN="fixture", NO_PROXY="127.0.0.1,localhost")
            process = subprocess.run(argv, input="Inventory probe: do not use tools. Return inventory complete.",
                                     env=env, cwd=directory, capture_output=True, text=True, timeout=60)
            emitted = captures[before:]
            flattened = flatten_tools(emitted[0]["tools"]) if emitted else []
            if emitted:
                for extra in emitted[0]["additional_tools"]:
                    flattened.extend(flatten_tools(extra["tools"], pointer=extra["pointer"]))
            result = {"role": role, "builder": builder, "sandbox": sandbox, "cli_exit": process.returncode,
                      "captured_requests": len(emitted), "wire_keys": emitted[0]["wire_keys"] if emitted else [], "wire_path": emitted[0]["wire_path"] if emitted else None,
                      "top_level_tool_count": len(emitted[0]["tools"]) if emitted else None,
                      "native_tool_count": len(flattened), "native_tools": flattened,
                      "catalog": "packaged restricted recorder catalog" if role == "recorder" else "authenticated CLI model catalog",
                      "stderr_tail": process.stderr[-2000:] if not emitted else None}
            results.append(result)
            print(json.dumps({k: v for k, v in result.items() if k != "native_tools"}), flush=True)
    finally:
        server.shutdown()
        server.server_close()
    report = {"cli_version": subprocess.check_output([str(options.executable), "--version"], text=True).strip(),
              "source_head": options.source_head,
              "model_catalog_sha256": hashlib.sha256(options.model_catalog.read_bytes()).hexdigest(),
              "scope": "Actual installed CLI tool assembly using candidate adapter argv and authenticated model metadata; local empty MCP and Responses rejection; no real model acceptance",
              "differences_from_live": ["isolated CODEX_HOME/workspace", "empty MCP fixture replaces semantic grants",
                                        "local custom Responses provider", "authenticated model catalog pinned offline", "no external MCP configured"],
              "roles": results}
    (output / "native-tools.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
