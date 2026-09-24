"""The exhaustion blocker predicate must see the public waiter shapes.

The old inline predicate looked for nonexistent top-level ``run_ref`` /
``waiting`` keys on public waiter dicts, so a Bundle root's own open
HumanRequest never became a direct ``needs_input`` blocker. The public
waiter carries the asserted run inside ``target_assertion`` and blocks
while ``status == "blocked"``.
"""

from meta_research.bundle_exhaustion import _waiter_asserts_run


def test_top_level_run_assertion_matches_exact_run() -> None:
    assert _waiter_asserts_run({"run_ref": "bundle_run_a"}, "bundle_run_a")
    assert not _waiter_asserts_run({"run_ref": "bundle_run_b"}, "bundle_run_a")


def test_root_nested_run_assertion_matches_exact_run() -> None:
    assertion = {
        "root": {
            "run_ref": "bundle_run_a",
            "attempt_ref": "attempt_1",
            "fence_ref": "fence_1",
            "root_session_ref": "session_1",
        }
    }
    assert _waiter_asserts_run(assertion, "bundle_run_a")
    assert not _waiter_asserts_run(assertion, "bundle_run_b")


def test_non_dict_or_runless_assertions_never_match() -> None:
    assert not _waiter_asserts_run(None, "bundle_run_a")
    assert not _waiter_asserts_run("bundle_run_a", "bundle_run_a")
    assert not _waiter_asserts_run({"target_ref": "target_1"}, "bundle_run_a")
    assert not _waiter_asserts_run({"root": "bundle_run_a"}, "bundle_run_a")
