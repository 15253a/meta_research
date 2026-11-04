from dataclasses import replace
import json

from meta_research.owners.common import canonical_hash
from test_bundle_batch_access import _bind
from test_bundle_delivery_sequence import _proposal
from test_bundle_input_drift_recovery import _adapter, _output
from test_gemini_batch_delta import _source_readers


def test_large_durable_input_survives_while_unchanged_model_body_is_removed(tmp_path):
    adapter, runner = _adapter(tmp_path, _output("batch"), durable=True)
    first, _, *_ = _bind(adapter)
    commit = {**first.target_commits[0], "accepted_scientific_body": "x" * (17 * 1024 * 1024)}
    first = replace(first, target_commits=(commit,))
    adapter.bind_batch_source_readers(lambda request: {
        **_source_readers(request), "proposals": () if request.base_generation == 0 else (_proposal(first),),
        "proposal_checkpoints": {"proposal:0": first.inbox_checkpoint}})
    later = _output("batch")
    later["strategy_update"]["revision"] = 3
    runner._outputs = iter([_output("batch"), later])
    adapter.propose_target_batch(first)
    second = replace(first, base_generation=1)
    result = adapter.propose_target_batch(second)
    directory = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": first.job_ref}) / "target-batch-2"
    business = (directory / "business-input.json").read_bytes()
    prompt = (directory / "prompt.txt").read_bytes()
    assert len(business) > 16 * 1024 * 1024
    assert json.loads(business)["request"]["target_commits"] == [commit]
    assert len(prompt) < 100_000
    view = json.loads(next(line.partition("=")[2] for line in prompt.decode().splitlines()
                           if line.startswith("batch_context=")))
    assert view["target_commits"] == []
    assert adapter.propose_target_batch(second) == result
    assert len(runner.calls) == 2
    assert (directory / "business-input.json").read_bytes() == business
    assert (directory / "prompt.txt").read_bytes() == prompt
