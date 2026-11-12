"""Controlled role results with a real supervised provider process lifetime.

The observer controls model content and reads the real packaged references.
The executable consumes stdin and writes that content; only the production
supervisor creates the process-tree proof and signed exit receipt.
"""
import json
from pathlib import Path
import sys


def controlled_codex(path: Path) -> Path:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json,sys\nfrom pathlib import Path\n"
        "if '--version' in sys.argv:\n print('codex-role-delivery-test 1');raise SystemExit(0)\n"
        "args=sys.argv\nresult=Path(args[args.index('--output-last-message')+1])\n"
        "schema=Path(args[args.index('--output-schema')+1])\n"
        "response=json.loads((schema.parent/'controlled-provider-response.json').read_text())\n"
        "prompt=sys.stdin.read()\nassert prompt\n"
        "result.write_text(response['result'],encoding='utf-8')\n"
        "sys.stdout.write(response['stdout'])\n"
        "sys.stderr.write(response['stderr'])\n"
        "raise SystemExit(response['returncode'])\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def run_observed_durable_result(observer, job_ref, argv, prompt, timeout,
                                stdout_path, pid_path, request_path, environment=None):
    from meta_research.quest_drafting import _CancellableProcessRunner

    completed = observer(argv, prompt, timeout, environment)
    result_path = Path(argv[argv.index("--output-last-message") + 1])
    response = {"result": result_path.read_text(encoding="utf-8"),
                "stdout": completed.stdout, "stderr": completed.stderr,
                "returncode": completed.returncode}
    (result_path.parent / "controlled-provider-response.json").write_text(
        json.dumps(response, ensure_ascii=False), encoding="utf-8")
    result_path.unlink()
    return _CancellableProcessRunner().run_durable_job(
        job_ref, argv, prompt, timeout, stdout_path, pid_path, request_path, environment)
