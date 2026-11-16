from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from meta_research.bundle_skill import BundleDispatchRequest
from meta_research.owners.common import canonical_hash
from test_bundle_batch_access import _bind
from test_bundle_input_drift_recovery import _adapter, _output
from test_gemini_batch_delta import _source_readers


def capture_bundle_resume_inputs(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("dispatch"), durable=True)
    batch, _, _, run, _, _ = _bind(adapter)
    target = deepcopy(batch.current_targets[0])
    target["spec"]["candidate"]["routes"][0]["route_ref"] = ("ORIGINAL_CANDIDATE_DETAIL " * 100).strip()
    target["candidate"] = deepcopy(target["spec"]["candidate"])
    target["spec_hash"] = canonical_hash(target["spec"])
    target["risk_class"] = "normal"
    initial = deepcopy(batch.initial_target_plan)
    initial["initial_strategy_update"]["candidates"] = [deepcopy(target["spec"])]
    first_result = {**batch.target_commits[0], "scientific_result": "FIRST_EXACT_RESULT " * 180,
                    "lambda": ["0.001", "0.0010000000000000002"],
                    "invalid_rows": [42], "denominator": 600, "nominal_zeros": 40,
                    "failure": "exit zero but nonfinite factors"}
    batch = replace(batch, initial_target_plan=initial, current_targets=(target,), target_commits=(first_result,))
    adapter.bind_batch_source_readers(lambda request: {**_source_readers(request), "proposals": ()})
    scope = {name: getattr(batch, name) for name in (
        "stage_request_ref", "cycle_ref", "run_ref", "attempt_ref", "fence_ref", "root_session_ref",
        "graph_ref", "native_session_ref", "runtime_binding", "inbox_checkpoint", "cycle_position", "job_ref")}
    dispatch = BundleDispatchRequest(**scope, generation=1, frontier=(target,),
        state={"target_commit_refs": [], "running_targets": [], "blocked_targets": []})
    later_output = _output("batch")
    later_output["strategy_update"]["revision"] = 3
    runner._outputs = iter([_output("dispatch"), _output("batch"), _output("dispatch"),
                            later_output, later_output])
    adapter.schedule_targets(dispatch)
    adapter.propose_target_batch(batch)
    changed_target = {**target, "dispatch_allowed": False, "human_request_status": "not_open",
                      "human_request_ref": None,
                      "human_request_command": {"semantic_operation_id": "human_request.open",
                          "reconciliation_operation_id": "human_request.open.reconcile",
                          "arguments": {"effect_id": "exact-current-human-effect", "text": "New permission required"}}}
    continued_dispatch = replace(dispatch, generation=2, frontier=(changed_target,),
        state={"target_commit_refs": [first_result["commit_ref"]], "running_targets": [],
               "blocked_targets": [{"target_ref": target["target_ref"], "reason": "new permission required"}]})
    adapter.schedule_targets(continued_dispatch)
    fresh_result = {"commit_ref": "commit:new-arrival", "target_ref": target["target_ref"],
                    "scientific_result": "NEW_COUNTEREXAMPLE " * 100,
                    "raw": {"lambda": "0.0010000000000000002", "valid": False}}
    later_batch = replace(batch, base_generation=1, target_commits=(*batch.target_commits, fresh_result))
    adapter.propose_target_batch(later_batch)
    correction = replace(later_batch, job_ref="bundle-job:correction",
        provider_feedback={"detail_code": "fix_result_reference", "repair_hint": "THIS_CORRECTION_ONLY"})
    adapter.propose_target_batch(correction)
    names = ("first-dispatch", "first-batch", "continued-dispatch", "new-result-batch", "correction-batch")
    samples = [{"name": name, "argv": argv, "prompt": prompt, "schema": schema,
                "characters": len(prompt), "utf8_bytes": len(prompt.encode("utf-8"))}
               for name, (argv, prompt, schema) in zip(names, runner.calls, strict=True)]
    return adapter, runner, (dispatch, batch, continued_dispatch, later_batch, correction), samples


if __name__ == "__main__":
    import sys
    destination = Path(sys.argv[1])
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        _, _, _, samples = capture_bundle_resume_inputs(Path(temporary))
    for sample in samples:
        (destination / (sample["name"] + ".txt")).write_text(sample["prompt"], encoding="utf-8")
    (destination / "samples.json").write_text(json.dumps(samples, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps([{key: sample[key] for key in ("name", "characters", "utf8_bytes")}
                      for sample in samples], ensure_ascii=False))
