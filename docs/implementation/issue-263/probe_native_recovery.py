"""Real installed CLI native failures and correction in the original session.

Run only in a new disposable directory. Auth is copied into that directory and
removed in finally; the caller retains its own source credential. This does not
use production research data or issue a semantic grant. The MCP fixture is empty.
"""
from pathlib import Path
import argparse
import hashlib
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
    a = p.parse_args()
    sys.path.insert(0, str(a.source / "src"))
    output = a.output.resolve()
    output.mkdir(exist_ok=False)
    home = output / "home"
    home.mkdir(mode=0o700)
    auth = home / "auth.json"
    server = None
    try:
        shutil.copyfile(a.auth, auth)
        auth.chmod(0o600)
        server = make_server([])
        (output / "adapter").mkdir()
        argv, builder, sandbox = adapter_arguments(a.source, a.executable, output / "adapter", "companion",
                                                   f"http://127.0.0.1:{server.server_port}/mcp")
        work = Path(argv[argv.index("--cd") + 1])
        (work / "retained-work.txt").write_text("EXISTING_VALID_WORK_263\n")
        (work / "correct-input.txt").write_text("NATIVE_FIXTURE_263\n")
        (work / "draft.txt").write_text("status=draft\nretained=KEEP_263\n")
        before = hashlib.sha256((work / "retained-work.txt").read_bytes()).hexdigest()
        env = dict(os.environ, CODEX_HOME=str(home), META_RESEARCH_MCP_TOKEN="fixture",
                   HTTP_PROXY=a.proxy, HTTPS_PROXY=a.proxy, NO_PROXY="127.0.0.1,localhost")
        prompt = """Perform this bounded software acceptance fixture using actual native tools.
    Work only in the current directory. No research operations, web, real data, or credentials.
    1. Make exactly one native shell call `cat absent-input.txt`. Observe the actual failure.
       Then list this directory and read the existing correct-input.txt. Do not create absent-input.txt.
    2. Make one apply_patch update of draft.txt expecting the intentionally wrong line status=stale.
       Observe the actual context failure, read draft.txt, then correct only status=draft to status=verified.
       Preserve retained=KEEP_263 and retained-work.txt bytes. Do not replace the whole directory.
    3. Send one native subagent message to the invalid identifier native-missing-agent-263.
       Observe the actual rejection. Then spawn a real child whose sole task is to read draft.txt and
       retained-work.txt and report their exact status/marker. Do not edit anything in the child.
       Wait for the real child, and retain its returned identity for the next turn. Do not invent results.
    4. Return the required JSON result string briefly stating actual failure reasons, corrections,
       preserved work, and child identifier. If a tool is unavailable, report that exact gap.
    """
        turns = []
        for index in range(2):
            if index:
                argv[-1:] = ["resume", turns[0]["native_session_ref"], "-"]
                prompt = """Continue this same native session. Read draft.txt and retained-work.txt with native tools.
Verify status=verified, retained=KEEP_263 and EXISTING_VALID_WORK_263 remain. Use the existing child
identity from the prior turn to ask it for a short second read verification, then wait for that same child.
Do not spawn another child or rewrite valid work. Return JSON result with actual evidence and same child id."""
            (output / f"turn-{index + 1}-prompt.txt").write_text(prompt)
            completed = subprocess.run(argv, input=prompt, text=True, capture_output=True, env=env, cwd=work, timeout=240)
            (output / f"turn-{index + 1}.jsonl").write_text(completed.stdout)
            (output / f"turn-{index + 1}-stderr.txt").write_text(completed.stderr)
            events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
            refs = [e["thread_id"] for e in events if e.get("type") == "thread.started"]
            turn = {"number": index + 1, "exit": completed.returncode, "native_session_ref": refs[0] if refs else None,
                    "turn_completed": any(e.get("type") == "turn.completed" for e in events), "event_count": len(events)}
            turns.append(turn)
            print(json.dumps(turn), flush=True)
            if completed.returncode or not turn["native_session_ref"]:
                break
        report = {"builder": builder, "sandbox": sandbox, "turns": turns,
                  "same_session": len(turns) == 2 and turns[0]["native_session_ref"] == turns[1]["native_session_ref"],
                  "retained_work_unchanged": before == hashlib.sha256((work / "retained-work.txt").read_bytes()).hexdigest(),
                  "draft": (work / "draft.txt").read_text(), "absent_input_not_created": not (work / "absent-input.txt").exists(),
                  "boundary": "Real model and installed CLI with exact Companion adapter argv; empty MCP fixture; no semantic/business authorization"}
        (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report), flush=True)
    finally:
        auth.unlink(missing_ok=True)
        if server is not None:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
