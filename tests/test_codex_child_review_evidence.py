"""Check the public review boundary after the sealed-ledger gate was retired.

The standalone codex_child_review verifier was removed in 7729571. Native
child traces remain diagnostic evidence; current Idea acceptance validates the
draft, final outcome and advisory review hashes without claiming a trusted
child identity. These tests exercise that live acceptance boundary.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.idea_contract import IDEA_REVIEW_SCHEMA_REF
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillContractError,
    review_record,
    validate_idea_skill_result,
)
from meta_research.owners.common import canonical_hash
from test_idea_skill_contract import (
    _SequenceRunner,
    _fake_codex_executable,
    _idea_set,
    _request,
    _result,
    _review_turn_output,
)


def test_review_acceptance_binds_both_material_hashes_without_child_evidence() -> None:
    draft = _idea_set()
    final = _idea_set("Compare robustness under a changed intervention")
    result = _result(draft=draft, final=final)

    draft_hash, outcome_hash, review_hash = validate_idea_skill_result(
        _request(), result
    )

    assert draft_hash == canonical_hash(draft)
    assert outcome_hash == canonical_hash(final)
    assert draft_hash != outcome_hash
    record = review_record(result, draft_hash=draft_hash, outcome_hash=outcome_hash)
    assert record == {
        "schema_ref": IDEA_REVIEW_SCHEMA_REF,
        "reviewed_draft_hash": draft_hash,
        "final_outcome_hash": outcome_hash,
    }
    assert review_hash == canonical_hash(record)


@pytest.mark.parametrize(
    ("review_mode", "reviewer_agent_ref"),
    (
        ("trusted_child", "codex-child:1"),
        ("codex_child", "codex-child:1"),
        ("external_session", "codex-child:1"),
        ("advisory_unobserved", "codex-child:1"),
        ("advisory_unobserved", "run-session:1"),
    ),
)
def test_review_acceptance_rejects_claims_of_verified_child_identity(
    review_mode: str, reviewer_agent_ref: str
) -> None:
    result = _result(review_mode=review_mode, reviewer_agent_ref=reviewer_agent_ref)

    with pytest.raises(IdeaSkillContractError, match="idea_review_mode_invalid"):
        validate_idea_skill_result(_request(), result)


def test_advisory_review_still_rejects_changed_native_root_session() -> None:
    request = _request(native_session_ref="codex-primary:1")
    result = replace(_result(), primary_session_ref="different-root:1")

    with pytest.raises(IdeaSkillContractError, match="root_native_session_changed"):
        validate_idea_skill_result(request, result)


@pytest.mark.parametrize("field", ("reviewed_draft", "final_outcome"))
def test_advisory_review_still_validates_evidence_in_both_materials(field: str) -> None:
    result = _result()
    altered = deepcopy(getattr(result, field))
    altered["candidates"][0]["evidence_boundary"]["accepted_evidence_refs"] = [
        "asset:unaccepted"
    ]

    with pytest.raises(IdeaSkillContractError, match="accepted_evidence_ref_unbound"):
        validate_idea_skill_result(_request(), replace(result, **{field: altered}))


def test_production_review_without_spawn_or_wait_still_validates_public_result(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner(
        [{"outcome": _idea_set()}, _review_turn_output()],
        emit_review_spawn=False,
        emit_review_wait=False,
    )
    executable = _fake_codex_executable(
        tmp_path / "fake-codex-review", read_all_input=True
    )
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "advisory-review",
        executable=str(executable),
        process_runner=runner,
    )
    request = _request(runtime_binding=adapter.runtime_binding())

    result = adapter.execute(request)
    draft_hash, outcome_hash, review_hash = validate_idea_skill_result(request, result)

    assert result.review_mode == "advisory_unobserved"
    assert result.reviewer_agent_ref is None
    assert draft_hash == canonical_hash(result.reviewed_draft)
    assert outcome_hash == canonical_hash(result.final_outcome)
    assert review_hash == canonical_hash(
        review_record(result, draft_hash=draft_hash, outcome_hash=outcome_hash)
    )
