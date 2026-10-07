from __future__ import annotations

from pathlib import Path

import pytest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from meta_research.composition import build_production_runtime
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root


def intake(memory, content, key, **values):
    result = memory.submit_asset_intake(
        AssetIntakeRequest(
            source_kind="text",
            custody_mode="managed",
            display_name="observation.txt",
            content=content,
            **values,
        ),
        idempotency_key=key,
    )
    assert result.status == "accepted", result.failure_code
    assert result.asset is not None
    return result.asset


def test_supplement_selects_current_without_rewriting_exact_history(tmp_path: Path):
    data_root = prepare_data_root(tmp_path / "lifecycle")
    runtime = build_production_runtime(data_root)
    try:
        memory = runtime.owners.research_memory
        first = intake(memory, b"first observation\n", "first")
        state = memory.query_asset_lifecycle(first.asset_ref)
        assert state["current_version_ref"] == first.version_ref
        assert state["revision"] == 1
        second = intake(
            memory,
            b"first observation\nsecond observation\n",
            "second",
            asset_ref=first.asset_ref,
            change={
                "kind": "supplement",
                "predecessor_version_ref": first.version_ref,
                "expected_revision": 1,
                "explanation": "Adds the second independent observation.",
            },
        )
        assert (
            memory.query_current_asset(first.asset_ref).version_ref
            == second.version_ref
        )
        assert (
            memory.materialize_asset(first.version_ref).content
            == b"first observation\n"
        )
        state = memory.query_asset_lifecycle(first.asset_ref)
        assert [(v["version_ref"], v["state"]) for v in state["versions"]] == [
            (first.version_ref, "superseded"),
            (second.version_ref, "current"),
        ]
        assert (
            state["changes"][-1]["explanation"]
            == "Adds the second independent observation."
        )
        assert memory.query_asset_version(first.version_ref).receipt == first.receipt
    finally:
        runtime.close()
    reopened = build_production_runtime(data_root)
    try:
        assert (
            reopened.owners.research_memory.query_current_asset(
                first.asset_ref
            ).version_ref
            == second.version_ref
        )
        assert (
            reopened.owners.research_memory.materialize_asset(first.version_ref).content
            == b"first observation\n"
        )
    finally:
        reopened.close()


def retire(memory, graph, asset, key, **values):
    return memory.retire_asset_version(
        asset.version_ref,
        expected_revision=memory.query_asset_lifecycle(asset.asset_ref)["revision"],
        expected_reference_revision=graph.query_asset_reference_revision(),
        explanation="The draft contains a verified unit error, is obsolete and has no remaining research or explanatory value.",
        low_value=True,
        obsolete=True,
        incorrect=True,
        impact_understood=True,
        has_explanation_value=False,
        idempotency_key=key,
        **values,
    )


def test_retirement_is_a_persisted_state_and_retains_shared_and_linked_bytes(
    tmp_path: Path,
):
    root = prepare_data_root(tmp_path / "retirement")
    runtime = build_production_runtime(root)
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        first = intake(memory, b"obsolete unit error\n", "obsolete")
        shared = intake(memory, b"obsolete unit error\n", "independent")
        request = dict(
            expected_revision=1,
            expected_reference_revision=graph.query_asset_reference_revision(),
            explanation="Verified obsolete unit error. No research depends on this disposable draft.",
            low_value=True,
            obsolete=True,
            incorrect=True,
            impact_understood=True,
            has_explanation_value=False,
            idempotency_key="retire",
        )
        fact = memory.retire_asset_version(first.version_ref, **request)
        assert memory.retire_asset_version(first.version_ref, **request) == fact
        assert fact["kind"] == "retirement"
        assert memory.query_current_asset(first.asset_ref) is None
        assert (
            memory.query_asset_lifecycle(first.asset_ref)["versions"][0]["state"]
            == "retired"
        )
        assert (
            memory.materialize_asset(first.version_ref).content
            == b"obsolete unit error\n"
        )
        assert (
            memory.materialize_asset(shared.version_ref).content
            == b"obsolete unit error\n"
        )
        with pytest.raises(
            OwnerConflict, match="asset_retirement_idempotency_conflict"
        ):
            memory.retire_asset_version(
                first.version_ref, **{**request, "explanation": "Different reason"}
            )
        source = tmp_path / "external.txt"
        source.write_bytes(b"external original\n")
        linked = memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="local_path",
                custody_mode="linked_local",
                source_locator=str(source),
                display_name="external.txt",
            ),
            idempotency_key="linked",
        ).asset
        retire(memory, graph, linked, "retire-linked")
        assert source.read_bytes() == b"external original\n"
        assert (
            memory.materialize_asset(linked.version_ref).content
            == b"external original\n"
        )
    finally:
        runtime.close()
    reopened = build_production_runtime(root)
    try:
        assert (
            reopened.owners.research_memory.query_current_asset(first.asset_ref) is None
        )
        assert (
            reopened.owners.research_memory.query_asset_lifecycle(first.asset_ref)[
                "changes"
            ][-1]
            == fact
        )
    finally:
        reopened.close()


def test_fresh_hold_and_unknown_impact_block_retirement(tmp_path: Path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "holds"))
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        asset = intake(memory, b"draft\n", "draft")
        assessment = memory.assess_release_eligibility(
            asset.version_ref,
            expected_reference_revision=graph.query_asset_reference_revision(),
            idempotency_key="old-assessment",
        )
        assert assessment.eligible is True
        hold = memory.place_asset_hold(
            asset.version_ref,
            reason="Retain until unit investigation finishes.",
            idempotency_key="fresh-hold",
        )
        with pytest.raises(OwnerConflict) as error:
            retire(memory, graph, asset, "blocked")
        assert error.value.details["reasons"] == ["active_holds"]
        assert error.value.details["active_hold_refs"] == [hold.hold_ref]
        assert (
            memory.query_current_asset(asset.asset_ref).version_ref == asset.version_ref
        )
        memory.release_asset_hold(hold.hold_ref, idempotency_key="release")
        with pytest.raises(OwnerConflict) as error:
            memory.retire_asset_version(
                asset.version_ref,
                expected_revision=1,
                expected_reference_revision=graph.query_asset_reference_revision(),
                explanation="Impact remains unknown.",
                low_value=True,
                obsolete=True,
                incorrect=True,
                impact_understood=False,
                has_explanation_value=True,
                idempotency_key="unknown",
            )
        assert error.value.details["reasons"] == [
            "impact_uncertain",
            "explanation_value_retained",
        ]
        assert len(memory.query_asset_lifecycle(asset.asset_ref)["changes"]) == 1
    finally:
        runtime.close()


@pytest.mark.parametrize("impact", [None, []], ids=["omitted", "empty"])
def test_correction_requires_explicit_assessment_when_no_work_is_listed(
    tmp_path: Path, impact
):
    from test_public_research_asset_roles import _runtime, _accepted_quest

    runtime = _runtime(tmp_path / "missing-assessment")
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        quest = _accepted_quest(runtime)
        first = intake(memory, b"density: 1000 g\n", "wrong")
        basis = intake(memory, b"instrument sheet: 1000 mg\n", "basis")
        role = graph.accept_asset_role(
            binding=first.as_binding(),
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="existing-density-use",
        )
        change = {
            "kind": "correction",
            "predecessor_version_ref": first.version_ref,
            "expected_revision": 1,
            "explanation": "Corrects the transcribed unit.",
            "error": "Milligrams were transcribed as grams.",
            "scope": "Density observation.",
            "evidence_bindings": [basis.as_binding().as_dict()],
        }
        if impact is not None:
            change["impact"] = impact
        with pytest.raises(
            OwnerConflict, match="asset_change_no_affected_work_explanation_required"
        ):
            intake(
                memory, b"density: 1000 mg\n", "missing-assessment",
                asset_ref=first.asset_ref, change=change,
            )
        assert memory.query_current_asset(first.asset_ref) == first
        assert len(memory.query_asset_lifecycle(first.asset_ref)["changes"]) == 1
        assert (
            graph.accept_asset_role(
                binding=first.as_binding(), role="evidence", quest_ref=quest.quest_ref,
                idempotency_key="existing-density-use",
            ) == role
        )
    finally:
        runtime.close()


def test_correction_records_a_checked_no_affected_work_assessment(tmp_path: Path):
    root = prepare_data_root(tmp_path / "no-affected-work")
    runtime = build_production_runtime(root)
    try:
        memory = runtime.owners.research_memory
        first = intake(memory, b"draft: 1000 g\n", "wrong")
        basis = intake(memory, b"instrument sheet: 1000 mg\n", "basis")
        explanation = "Checked this unused draft and its origin Quest; no accepted analysis uses this observation."
        change = {
            "kind": "correction",
            "predecessor_version_ref": first.version_ref,
            "expected_revision": 1,
            "explanation": "Corrects the draft transcription.",
            "error": "Milligrams were transcribed as grams.",
            "scope": "Unused density draft.",
            "evidence_bindings": [basis.as_binding().as_dict()],
        }
        for index, value in enumerate((None, "", " \n ", 7)):
            with pytest.raises(
                OwnerConflict,
                match="asset_change_no_affected_work_explanation_required",
            ):
                intake(
                    memory, b"draft: 1000 mg\n", "invalid-" + str(index),
                    asset_ref=first.asset_ref,
                    change={**change, "no_affected_work_explanation": value},
                )
        change["no_affected_work_explanation"] = "  " + explanation + "  "
        corrected = intake(
            memory, b"draft: 1000 mg\n", "correct",
            asset_ref=first.asset_ref, change=change,
        )
        fact = memory.query_asset_lifecycle(first.asset_ref)["changes"][-1]
        assert fact["impact"] == []
        assert fact["no_affected_work_explanation"] == explanation
        assert (
            intake(memory, b"draft: 1000 mg\n", "correct",
                   asset_ref=first.asset_ref, change=change) == corrected
        )
        assert memory.materialize_asset(first.version_ref).content == b"draft: 1000 g\n"
        assert memory.query_asset_version(first.version_ref).receipt == first.receipt
        with pytest.raises(OwnerConflict, match="asset_intake_idempotency_conflict"):
            intake(
                memory, b"draft: 1000 mg\n", "correct", asset_ref=first.asset_ref,
                change={**change, "no_affected_work_explanation": "Different assessment."},
            )
    finally:
        runtime.close()
    reopened = build_production_runtime(root)
    try:
        memory = reopened.owners.research_memory
        assert memory.query_asset_lifecycle(first.asset_ref)["changes"][-1] == fact
        assert memory.query_current_asset(first.asset_ref) == corrected
    finally:
        reopened.close()


def test_correction_keeps_exact_evidence_scope_and_work_judgments(tmp_path: Path):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "correction"))
    try:
        memory = runtime.owners.research_memory
        first = intake(memory, b"density: 1000 mg\n", "wrong")
        evidence = intake(memory, b"instrument sheet: 1 mg\n", "evidence")
        change = {
            "kind": "correction",
            "predecessor_version_ref": first.version_ref,
            "expected_revision": 1,
            "explanation": "Only the density unit transcription is corrected.",
            "error": "The milligram value was transcribed as grams.",
            "scope": "Density observation only.",
            "evidence_bindings": [evidence.as_binding().as_dict()],
            "impact": [
                {
                    "work_ref": "density-analysis",
                    "judgment": "recheck",
                    "explanation": "Recompute derived density.",
                },
                {
                    "work_ref": "unreviewed-comparison",
                    "judgment": "unknown",
                    "explanation": "Dependency still needs investigation.",
                },
            ],
        }
        corrected = intake(
            memory,
            b"density: 1 mg\n",
            "correct",
            asset_ref=first.asset_ref,
            change=change,
        )
        fact = memory.query_asset_lifecycle(first.asset_ref)["changes"][-1]
        assert fact["error"] == "The milligram value was transcribed as grams."
        assert fact["scope"] == "Density observation only."
        assert fact["evidence_bindings"][0]["version_ref"] == evidence.version_ref
        assert [row["judgment"] for row in fact["impact"]] == ["recheck", "unknown"]
        assert "no_affected_work_explanation" not in fact
        assert (
            memory.materialize_asset(first.version_ref).content == b"density: 1000 mg\n"
        )
        assert (
            memory.materialize_asset(corrected.version_ref).content
            == b"density: 1 mg\n"
        )
        with pytest.raises(OwnerConflict, match="asset_correction_evidence_required"):
            memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name="bad.txt",
                    content=b"unsubstantiated",
                    asset_ref=first.asset_ref,
                    change={**change, "evidence_bindings": []},
                ),
                idempotency_key="missing-evidence",
            )
        stale = memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="stale.txt",
                content=b"stale change",
                asset_ref=first.asset_ref,
                change={
                    key: value
                    for key, value in {**change, "kind": "substantive_change"}.items()
                    if key
                    in {
                        "kind",
                        "predecessor_version_ref",
                        "expected_revision",
                        "explanation",
                    }
                },
            ),
            idempotency_key="stale",
        )
        assert stale.status == "failed"
        assert stale.failure_code == "asset_revision_stale"
        assert (
            memory.query_current_asset(first.asset_ref).version_ref
            == corrected.version_ref
        )
        with pytest.raises(OwnerConflict) as error:
            retire(
                memory,
                runtime.owners.research_graph,
                evidence,
                "retire-correction-basis",
            )
        assert error.value.details["reasons"] == ["active_references"]
        disposable_basis = intake(memory, b"discarded basis", "discarded-basis")
        retire(memory, runtime.owners.research_graph, disposable_basis, "discard-basis")
        result = memory.submit_asset_intake(
            AssetIntakeRequest(
                source_kind="text",
                custody_mode="managed",
                display_name="retired-evidence.txt",
                content=b"unsupported replacement",
                asset_ref=first.asset_ref,
                change={
                    **change,
                    "predecessor_version_ref": corrected.version_ref,
                    "expected_revision": 2,
                    "evidence_bindings": [disposable_basis.as_binding().as_dict()],
                },
            ),
            idempotency_key="retired-basis",
        )
        assert result.status == "failed"
        assert result.failure_code == "asset_version_retired"
    finally:
        runtime.close()


def test_two_connections_serialize_same_revision_and_retirement_new_use(tmp_path: Path):
    from test_public_research_asset_roles import _runtime, _accepted_quest

    path = tmp_path / "concurrent"
    first_runtime = _runtime(path)
    quest = _accepted_quest(first_runtime)
    second_runtime = _runtime(path)
    try:
        memory = first_runtime.owners.research_memory
        first = intake(memory, b"base\n", "base")
        barrier = Barrier(2)

        def revise(pair):
            index, runtime = pair
            barrier.wait(timeout=10)
            return runtime.owners.research_memory.submit_asset_intake(
                AssetIntakeRequest(
                    source_kind="text",
                    custody_mode="managed",
                    display_name="revision.txt",
                    content=f"revision {index}\n".encode(),
                    asset_ref=first.asset_ref,
                    change={
                        "kind": "substantive_change",
                        "expected_revision": 1,
                        "predecessor_version_ref": first.version_ref,
                        "explanation": "Changes the measurement conditions.",
                    },
                ),
                idempotency_key=f"concurrent-{index}",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(revise, enumerate((first_runtime, second_runtime))))
        assert sorted(result.status for result in results) == ["accepted", "failed"]
        assert (
            next(result.failure_code for result in results if result.status == "failed")
            == "asset_revision_stale"
        )
        winner = next(result.asset for result in results if result.status == "accepted")
        assert (
            memory.query_current_asset(first.asset_ref).version_ref
            == winner.version_ref
        )
        assert memory.materialize_asset(first.version_ref).content == b"base\n"
        asset = intake(memory, b"disposable concurrent draft\n", "race-draft")
        barrier = Barrier(2)

        def competing(action):
            barrier.wait(timeout=10)
            try:
                if action == "retire":
                    return (
                        action,
                        retire(
                            memory,
                            first_runtime.owners.research_graph,
                            asset,
                            "race-retire",
                        ),
                    )
                return (
                    action,
                    second_runtime.owners.research_graph.accept_asset_role(
                        binding=asset.as_binding(),
                        role="evidence",
                        quest_ref=quest.quest_ref,
                        idempotency_key="race-use",
                        verify_content=False,
                    ),
                )
            except OwnerConflict as error:
                return action, error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = dict(pool.map(competing, ("retire", "use")))
        if isinstance(outcomes["retire"], dict):
            assert outcomes["use"] == "asset_version_retired"
            assert memory.query_current_asset(asset.asset_ref) is None
        else:
            assert outcomes["retire"] == "asset_retirement_blocked"
            assert outcomes["use"].version_ref == asset.version_ref
            assert (
                memory.query_current_asset(asset.asset_ref).version_ref
                == asset.version_ref
            )
        assert (
            memory.materialize_asset(asset.version_ref).content
            == b"disposable concurrent draft\n"
        )
    finally:
        second_runtime.close()
        first_runtime.close()


def test_legacy_migration_preserves_receipts_and_requires_explicit_current_selection(
    tmp_path: Path,
):
    root = prepare_data_root(tmp_path / "legacy")
    runtime = build_production_runtime(root)
    asset = intake(runtime.owners.research_memory, b"legacy literal\n", "legacy")
    runtime.close()
    with sqlite3.connect(root.database) as connection:
        before = connection.execute("SELECT * FROM rm_asset_versions").fetchall()
        connection.execute("DROP TABLE rm_asset_changes")
        connection.execute("DROP TABLE rm_asset_version_lifecycle")
        connection.execute("DROP TABLE rm_asset_lifecycle")
        connection.execute(
            "UPDATE alembic_version SET version_num='0059_research_environments'"
        )
    migrated = build_production_runtime(root)
    try:
        memory = migrated.owners.research_memory
        assert memory.query_current_asset(asset.asset_ref) is None
        assert memory.query_asset_lifecycle(asset.asset_ref)["versions"] == [
            {
                "version_ref": asset.version_ref,
                "state": "unselected",
                "predecessor_version_ref": None,
                "successor_version_refs": [],
                "changes": [],
            }
        ]
        assert (
            memory.materialize_asset(asset.version_ref).content == b"legacy literal\n"
        )
        assert memory.query_asset_version(asset.version_ref).receipt == asset.receipt
        current = intake(
            memory,
            b"explicit replacement\n",
            "explicit",
            asset_ref=asset.asset_ref,
            change={
                "kind": "substantive_change",
                "expected_revision": 0,
                "predecessor_version_ref": asset.version_ref,
                "explanation": "An explicit scientific selection after migration.",
            },
        )
        assert (
            memory.query_current_asset(asset.asset_ref).version_ref
            == current.version_ref
        )
        with sqlite3.connect(root.database) as connection:
            assert (
                connection.execute(
                    "SELECT * FROM rm_asset_versions WHERE version_ref=?",
                    (asset.version_ref,),
                ).fetchall()
                == before
            )
    finally:
        migrated.close()


@pytest.mark.parametrize("tamper", ["head", "state", "fact", "hold"])
def test_corrupted_lifecycle_or_hold_never_authorizes_retirement(
    tmp_path: Path, tamper
):
    root = prepare_data_root(tmp_path / tamper)
    runtime = build_production_runtime(root)
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        asset = intake(memory, b"retain on uncertain metadata\n", "retained")
        if tamper == "hold":
            memory.place_asset_hold(
                asset.version_ref,
                reason="Keep pending investigation",
                idempotency_key="keep",
            )
        with sqlite3.connect(root.database) as connection:
            sql = {
                "head": "UPDATE rm_asset_lifecycle SET current_version_ref=NULL",
                "state": "UPDATE rm_asset_version_lifecycle SET state='retired'",
                "fact": "UPDATE rm_asset_changes SET kind='retirement'",
                "hold": "UPDATE rm_asset_holds SET receipt_hash='" + "a" * 64 + "'",
            }[tamper]
            connection.execute(sql)
        with pytest.raises(OwnerConflict) as error:
            retire(memory, graph, asset, "reject-corruption")
        assert error.value.code in {
            "asset_lifecycle_state_invalid",
            "asset_change_receipt_invalid",
            "asset_hold_receipt_invalid",
        }
        assert (
            memory.materialize_asset(asset.version_ref).content
            == b"retain on uncertain metadata\n"
        )
    finally:
        runtime.close()


def test_retired_version_rejects_new_role_and_live_role_blocks_retirement(
    tmp_path: Path,
):
    from test_public_research_asset_roles import _runtime, _accepted_quest

    runtime = _runtime(tmp_path / "roles")
    try:
        memory, graph = runtime.owners.research_memory, runtime.owners.research_graph
        quest = _accepted_quest(runtime)
        retired = intake(memory, b"disposable\n", "disposable")
        retire(memory, graph, retired, "retire-disposable")
        with pytest.raises(OwnerConflict, match="asset_version_retired"):
            graph.accept_asset_role(
                binding=retired.as_binding(),
                role="evidence",
                quest_ref=quest.quest_ref,
                idempotency_key="new-use",
                verify_content=False,
            )
        protected = intake(memory, b"negative result remains evidence\n", "protected")
        role = graph.accept_asset_role(
            binding=protected.as_binding(),
            role="evidence",
            quest_ref=quest.quest_ref,
            idempotency_key="existing-use",
        )
        with pytest.raises(OwnerConflict) as error:
            retire(memory, graph, protected, "retire-protected")
        assert error.value.details["reasons"] == ["active_references"]
        assert error.value.details["active_reference_refs"] == [
            "asset-role:" + role.role_ref
        ]
        assert (
            graph.accept_asset_role(
                binding=protected.as_binding(),
                role="evidence",
                quest_ref=quest.quest_ref,
                idempotency_key="existing-use",
            )
            == role
        )
        assert (
            memory.materialize_asset(protected.version_ref).content
            == b"negative result remains evidence\n"
        )
    finally:
        runtime.close()
