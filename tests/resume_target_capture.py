"""Capture Target inputs at the actual Codex adapter process-runner boundary."""
import json
from pathlib import Path

from test_target_root_runtime_context import capture_target_context, capture_target_rejection


def capture_target_inputs(workspace: Path):
    captures = {}
    for retained, name in ((True, "same-session"), (False, "replacement-session")):
        directory = workspace / name
        directory.mkdir(parents=True)
        for index, (argv, prompt) in enumerate(capture_target_context(directory, retained)):
            captures[name + ("-initial" if index == 0 else "-continue")] = _sample(argv, prompt)
    directory = workspace / "owner-rejection"
    directory.mkdir(parents=True)
    (argv, prompt), = capture_target_rejection(directory)
    captures["owner-rejection"] = _sample(argv, prompt)
    return captures


def _sample(argv, prompt):
    schema = (json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text(encoding="utf-8"))
              if "--output-schema" in argv else None)
    return {"argv": argv, "prompt": prompt, "schema": schema}
