from dataclasses import replace
from meta_research.owners.common import canonical_hash
from test_bundle_delivery_sequence import _sequence, _basis


def test_missing_newest_spool_cannot_hide_reverted_fact(tmp_path, monkeypatch):
    adapter, runner, first, shown = _sequence(tmp_path, monkeypatch)
    original = {**first.target_commits[0], "exact_result": "A"}
    revised = {**original, "exact_result": "B"}
    first = replace(first, target_commits=(original,))
    adapter.propose_target_batch(first)
    second = replace(first, base_generation=1, target_commits=(revised,))
    adapter.propose_target_batch(second)
    operation = adapter._workspace / "provider-operations" / canonical_hash({"job_ref": first.job_ref}) / "target-batch-2"
    (operation / "invocation.json").rename(operation / "invocation.unavailable")
    current = replace(first, base_generation=2)
    assert _basis(adapter, current, (first, second)) is None
    assert len(runner.calls) == 2
