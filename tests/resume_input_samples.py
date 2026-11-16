"""Capture application inputs without launching a model or a research service."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from resume_stage_capture import capture_all
from bundle_resume_samples import capture_bundle_resume_inputs
from test_native_codex_guidance import capture_native_guidance_input
from resume_target_capture import capture_target_inputs


def capture_inputs(workspace: Path, output: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=True)
    samples = capture_all(workspace / "stages")
    samples["target"] = capture_target_inputs(workspace / "target")
    bundle_workspace = workspace / "bundle"
    bundle_workspace.mkdir()
    _, _, _, bundle = capture_bundle_resume_inputs(bundle_workspace)
    samples["bundle"] = {sample["name"]: sample for sample in bundle}
    human = {}
    for resume in (False, True):
        for unknown in (False, True):
            name = ("resumed-root" if resume else "initial-root") + ("-unknown-steer" if unknown else "-steer")
            scenario_workspace = workspace / "native-human" / name
            scenario_workspace.mkdir(parents=True)
            human[name] = capture_native_guidance_input(scenario_workspace, unknown=unknown, resume=resume)
    samples["native-human"] = human
    rows = []
    for family, scenarios in samples.items():
        destination = output / family
        destination.mkdir(exist_ok=True)
        for name, sample in scenarios.items():
            prompt = sample["prompt"]
            normalized = prompt.replace(str(workspace.resolve()), "<fixture>").replace(
                str(Path(__file__).resolve().parents[1]), "<source>")
            (destination / (name + ".txt")).write_text(prompt, encoding="utf-8")
            rows.append({"family": family, "scenario": name,
                         "characters": len(prompt), "utf8_bytes": len(prompt.encode("utf-8")),
                         "normalized_characters": len(normalized),
                         "normalized_utf8_bytes": len(normalized.encode("utf-8"))})
    (output / "raw.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "sizes.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rows, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument("output", type=Path)
    arguments = parser.parse_args()
    capture_inputs(arguments.workspace, arguments.output)
