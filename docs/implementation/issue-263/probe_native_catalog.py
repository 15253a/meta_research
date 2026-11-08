"""Read ALL_TOOLS through a real model in an isolated role; no listed tool runs."""
from pathlib import Path
import argparse
import json
import os
import shutil
import subprocess
import sys

from collect_native_tools import adapter_arguments, make_server


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--executable", type=Path, required=True)
    p.add_argument("--auth", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--proxy", required=True)
    p.add_argument("--roles", nargs="+", required=True)
    a = p.parse_args()
    sys.path.insert(0, str(a.source / "src"))
    a.output.mkdir(exist_ok=False)
    server = make_server([])
    try:
        for role in a.roles:
            directory = a.output / role
            directory.mkdir()
            home = directory / "home"
            home.mkdir(mode=0o700)
            auth = home / "auth.json"
            try:
                shutil.copyfile(a.auth, auth)
                auth.chmod(0o600)
                argv, builder, sandbox = adapter_arguments(a.source, a.executable, directory, role,
                                                           f"http://127.0.0.1:{server.server_port}/mcp")
                env = dict(os.environ, CODEX_HOME=str(home), META_RESEARCH_MCP_TOKEN="fixture",
                           HTTP_PROXY=a.proxy, HTTPS_PROXY=a.proxy, NO_PROXY="127.0.0.1,localhost")
                prompt = """Bounded metadata-only software acceptance. Call functions.exec once with exactly
text(ALL_TOOLS.map(x => ({name:x.name,description:x.description}))) to observe the actual callable catalog
including deferred tools. Do not execute any listed tool. Do not use shell, web, child agents, files,
or research operations. Return the required result JSON stating the metadata was observed."""
                run = subprocess.run(argv, input=prompt, text=True, capture_output=True, env=env,
                                     cwd=directory, timeout=120)
                (directory / "stdout.jsonl").write_text(run.stdout)
                (directory / "stderr.txt").write_text(run.stderr)
                catalogs = []
                for path in (home / "sessions").rglob("*.jsonl"):
                    for line in path.read_text().splitlines():
                        row = json.loads(line)
                        item = row.get("payload", {})
                        if row.get("type") != "response_item" or item.get("type") != "custom_tool_call_output":
                            continue
                        for content in item.get("output", []):
                            try:
                                value = json.loads(content.get("text", ""))
                            except ValueError:
                                continue
                            if isinstance(value, list) and value and all(isinstance(t, dict) and "name" in t and "description" in t for t in value):
                                catalogs.append({"session_file": path.name, "call_id": item["call_id"], "tools": value})
                result = {"role": role, "builder": builder, "sandbox": sandbox, "exit": run.returncode,
                          "catalogs": catalogs, "scope": "Real-model ALL_TOOLS metadata; no listed tool invoked; empty semantic MCP fixture"}
                (directory / "runtime-catalog.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                print(json.dumps({"role": role, "exit": run.returncode, "catalogs": len(catalogs),
                                  "tool_names": [t["name"] for c in catalogs for t in c["tools"]]}), flush=True)
            finally:
                auth.unlink(missing_ok=True)
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
