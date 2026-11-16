"""Scientific integers and the retained formal-work inventory boundaries."""
from copy import deepcopy
import json
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from meta_research.formal_entities import (
    _artifact_paths, _checkpoint_paths, _declared_work, primary_work_metrics, verify_retained_products,
    explicit_run_only, verify_report_only_assessments,
    primary_reused_references, primary_evaluation_status,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json
from meta_research.owners.research_graph import (
    _decode_target_result_content, _target_metric_values,
    _target_jsonschema_integer, _target_jsonschema_number,
    _target_formal_measurement_receipt_bindings, _receipt_hash,
    FORMAL_MEASUREMENT_RECEIPT_KIND, SQLiteResearchGraph,
)


def _run(index=0, evaluations=0):
    return {"run_key": f"run-{index}", "evaluations": [
        {"attempt_key": f"evaluation-{i}", "metrics": {"quality": i}}
        for i in range(evaluations)
    ]}


@pytest.mark.parametrize("value", [-(2**63), 2**64 - 1, 2**100])
def test_rg_scientific_integers_remain_exact_in_tree_and_measurements(value):
    document = {"study": {"bootstrap_seed": value}, "metrics": {"quality": value}}
    assert _decode_target_result_content(json.dumps(document).encode()) == document
    metrics = _target_metric_values(result_content=document,
        required_metric_keys=("quality",), optional_metric_keys=())
    assert metrics["quality"] == value and type(metrics["quality"]) is int
    assert _target_jsonschema_integer(None, value)
    assert _target_jsonschema_number(None, value)


def test_rg_small_integer_keeps_original_canonical_type():
    assert type(_target_metric_values(result_content={"metrics": {"quality": 3}},
        required_metric_keys=("quality",), optional_metric_keys=())["quality"]) is int


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_rg_non_finite_numbers_still_rejected(bad):
    with pytest.raises(OwnerConflict):
        _decode_target_result_content(json.dumps({"study": bad}).encode())
    with pytest.raises(OwnerConflict):
        _target_metric_values(result_content={"metrics": {"quality": bad}},
            required_metric_keys=("quality",), optional_metric_keys=())


def test_one_hundred_runs_pass_and_one_hundred_one_reject_without_truncation():
    document = {"formal_runs": [_run(i) for i in range(100)]}
    assert len(_declared_work(document)) == 100
    document["formal_runs"].append(_run(100))
    original = deepcopy(document)
    with pytest.raises(OwnerConflict) as caught:
        _declared_work(document)
    feedback = caught.value.feedback
    assert "$.formal_runs" in feedback and "101" in feedback and "100" in feedback
    assert "merge" in feedback and "evidence" in feedback and "source" in feedback
    assert document == original


@pytest.mark.parametrize("count", [101, 107])
def test_evaluation_limit_reports_actual_count_and_keeps_original(count):
    document = {"formal_runs": [_run(evaluations=count)]}
    original = deepcopy(document)
    with pytest.raises(OwnerConflict) as caught:
        _declared_work(document)
    feedback = caught.value.feedback
    assert "$.formal_runs[0].evaluations" in feedback
    assert str(count) in feedback and "100" in feedback and "merge" in feedback
    assert document == original


def test_evaluation_count_is_per_processed_run_and_skip_semantics_stay():
    assert len(_declared_work({"formal_runs": [_run(evaluations=100)]})) == 100
    assert len(_declared_work({"formal_runs": [_run(0, 60), _run(1, 60)]})) == 120
    skipped = {"status": "blocked", "evaluations": [None] * 107}
    assert len(_declared_work({"formal_runs": [_run(), skipped]})) == 1


def test_independent_inventory_errors_are_reported_together():
    document = {"formal_runs": [_run(0, 101), _run(1, 107)]}
    with pytest.raises(OwnerConflict) as caught:
        _declared_work(document)
    assert "$.formal_runs[0].evaluations" in caught.value.feedback
    assert "$.formal_runs[1].evaluations" in caught.value.feedback
    assert "101" in caught.value.feedback and "107" in caught.value.feedback


def test_artifact_and_checkpoint_lists_have_no_hundred_item_limit():
    paths = [f"outputs/data/file-{i}.json" for i in range(107)]
    assert _artifact_paths(paths) == paths
    assert _checkpoint_paths(paths) == paths
    with pytest.raises(OwnerConflict):
        _artifact_paths(["../escape"])
    with pytest.raises(OwnerConflict):
        _artifact_paths([paths[0], paths[0]])


@pytest.mark.parametrize("field", ["dataset_candidates", "environment_candidates"])
def test_all_107_reusable_candidates_are_validated_and_last_is_not_skipped(field):
    paths = [f"outputs/data/observation-{i}.json" for i in range(107)]
    entries = [{"role": "data", "declared_relative_path": path} for path in paths]
    document = {"formal_runs": [{"run_key": "observed", "artifact_paths": paths,
        "evaluations": []}], field: [{"artifact_path": path, "name": "Observation",
        "purpose": "Reuse the observed results"} for path in paths]}
    verify_retained_products(document, entries)
    document[field][-1]["purpose"] = ""
    with pytest.raises(OwnerConflict) as caught:
        verify_retained_products(document, entries)
    assert f"$.{field}[106]" in caught.value.feedback


def test_feedback_retains_the_existing_channel_budget():
    with pytest.raises(OwnerConflict) as caught:
        _declared_work({"formal_runs": [{"run_key": "", "evaluations": [None] * 100}] * 100})
    assert len(caught.value.feedback) <= 4096
    assert "additional declaration errors omitted" in caught.value.feedback


def test_formal_metrics_do_not_require_repeated_summary():
    document = {"formal_runs": [_run(evaluations=1)]}
    assert primary_work_metrics(document) == {"quality": 0}
    document["metrics"] = {}
    assert primary_work_metrics(document) == {"quality": 0}
    with pytest.raises(OwnerConflict):
        _declared_work({"formal_runs": []})
    assert primary_work_metrics({"formal_runs": None, "metrics": {"quality": 3}}) == {"quality": 3}


def test_same_entry_can_reuse_its_parsed_work_without_reparsing(monkeypatch):
    document = {"formal_runs": [_run(evaluations=1)]}
    work = _declared_work(document)
    def unexpected_reparse(_):
        pytest.fail("the local parsed declaration should be reused")
    monkeypatch.setattr("meta_research.formal_entities._declared_work", unexpected_reparse)
    assert primary_work_metrics(document, work=work) == {"quality": 0}
    assert not explicit_run_only(document, work=work)
    assert primary_evaluation_status(document, work=work) == "completed"
    assert primary_reused_references(document, work=work) == {
        "variant_run_ref": None, "evaluation_attempt_ref": None, "metric_result_ref": None}
    result_document = SimpleNamespace(as_dict=lambda: document)
    assert SQLiteResearchGraph._root_reused_primary_binding(None, result_document, work=work) is None
    assert SQLiteResearchGraph._root_reused_primary_binding(None, result_document, "evaluation_attempt", work=work) is None
    verify_report_only_assessments(document, [], work=work)
    verify_retained_products(document, [], work=work)


def test_duplicate_identifiers_are_still_rejected():
    with pytest.raises(OwnerConflict):
        _declared_work({"formal_runs": [_run(), _run()]})
    run = _run(evaluations=1)
    run["evaluations"].append(deepcopy(run["evaluations"][0]))
    with pytest.raises(OwnerConflict):
        _declared_work({"formal_runs": [run]})


@pytest.mark.parametrize("source,stored", [(1, 1.0), (2**64 - 1, 2**64 - 1), (2**100, 2**100)])
def test_persisted_measurement_preserves_its_exact_value_type_and_hash(source, stored, monkeypatch):
    """Read an old float record and new integers through the production SQL reader."""
    receipt = SimpleNamespace(receipt_ref="source-receipt", payload_hash="source-hash",
        as_public_dict=lambda: {"source": "accepted"})
    protocol = SimpleNamespace(required_metric_keys=("quality",), optional_metric_keys=())
    authority = SimpleNamespace(authority_ref="authority", authority_hash="authority-hash", receipt=receipt,
        measurement_contract=SimpleNamespace(protocol_version=protocol, result_schema_ref="schema",
            result_schema=SimpleNamespace(as_dict=lambda: {})),
        experiment_keys=("experiment",), measurement_unit_key="unit", protocol_parts=(),
        protocol_aggregation_proof=None, identities=SimpleNamespace(protocol_version_ref="protocol"))
    attempt = SimpleNamespace(target_ref="target", target_run_ref="run", target_attempt_ref="execution",
        target_fence_ref="fence", attempt_binding_ref="attempt-binding", payload_hash="attempt-hash",
        receipt=receipt, evaluation_attempt_ref="evaluation")
    binding = SimpleNamespace(binding_ref="binding", request_hash="request-hash", exit_receipt_hash="exit-hash",
        receipt=receipt, target_run_ref="run", target_attempt_ref="execution", target_fence_ref="fence")
    terminal = SimpleNamespace(exit_receipt=None)
    manifest = SimpleNamespace(manifest_ref="manifest", payload_hash="manifest-hash", receipt=receipt)
    role = SimpleNamespace(role_ref="result-role", binding=SimpleNamespace(as_dict=lambda: {"version": "result-v1"}))
    expected = _target_metric_values(result_content={"metrics": {"quality": source}},
        required_metric_keys=("quality",), optional_metric_keys=())
    metrics = {"quality": stored}
    metrics_hash = canonical_hash(metrics)
    bindings = _target_formal_measurement_receipt_bindings(authority=authority, accepted_attempt=attempt,
        generic_binding=binding, terminal=terminal, manifest=manifest, result_role=role,
        result_schema_hash=canonical_hash({}), result_disposition=None, metrics_hash=metrics_hash)
    values = dict(metric_result_ref="metric", evaluation_attempt_ref="evaluation", result_role_ref="result-role",
        metrics_json=canonical_json(metrics), metrics_hash=metrics_hash,
        required_metrics_hash=canonical_hash(["quality"]), run_ref="run", execution_attempt_ref="execution",
        fence_ref="fence", execution_result_hash="exit-hash", execution_receipt_ref="source-receipt",
        execution_receipt_hash="source-hash", receipt_ref="metric-receipt",
        receipt_hash=_receipt_hash(FORMAL_MEASUREMENT_RECEIPT_KIND, "evaluation", bindings))
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE rg_metric_results (" + ",".join(key + " TEXT" for key in values) + ")"))
            connection.execute(text("INSERT INTO rg_metric_results VALUES (" + ",".join(":" + key for key in values) + ")"), values)
            connection.execute(text("CREATE TABLE rg_evaluation_attempts (evaluation_attempt_ref TEXT, status TEXT)"))
            connection.execute(text("INSERT INTO rg_evaluation_attempts VALUES ('evaluation', 'measurement_accepted')"))
        owner = SimpleNamespace(_database=SimpleNamespace(read=engine.connect, fenced_write=engine.begin),
            _target_formal_measurement_facts=lambda _: (authority, attempt, binding, SimpleNamespace(handle=object()), terminal,
                manifest, role, {"metrics": {"quality": source}}, expected, None))
        owner.query_target_formal_metric_result = lambda ref: SQLiteResearchGraph.query_target_formal_metric_result(owner, ref)
        accepted = SQLiteResearchGraph.query_target_formal_metric_result(owner, "evaluation")
        assert accepted.metrics == metrics and type(accepted.metrics["quality"]) is type(stored)
        assert accepted.metrics_hash == metrics_hash
        if sys.platform != "win32":
            # The native runtime imports POSIX locks; its frontier is tested
            # separately. Here exercise the persisted measurement retry branch.
            monkeypatch.setattr("meta_research.owners.agent_runtime.verify_current_target_run_frontier_in_transaction",
                lambda connection, handle: None)
            retried = SQLiteResearchGraph.accept_target_formal_measurement(owner, target_ref="target",
                evaluation_attempt_ref="evaluation", idempotency_key="same-attempt")
            assert retried == accepted
        with engine.begin() as connection:
            connection.execute(text("UPDATE rg_metric_results SET metrics_json=:content"),
                {"content": canonical_json({"quality": stored + 1})})
        with pytest.raises(OwnerConflict, match="target_formal_measurement_integrity_invalid"):
            SQLiteResearchGraph.query_target_formal_metric_result(owner, "evaluation")
    finally:
        engine.dispose()


@pytest.mark.skipif(sys.platform == "win32", reason="native Target runtime requires POSIX file descriptors")
def test_root_closure_capacity_error_retains_details_through_owner_intake(tmp_path, monkeypatch):
    from meta_research.bundle_protocol import BundleProtocolError
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture

    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        details = {"category": "technical_capacity", "rule": "serialized_utf8_bytes", "path": "$",
            "actual_is_lower_bound": True,
            "actual_bytes": 8 * 1024 * 1024 + 1, "limit": 8 * 1024 * 1024}
        failure = BundleProtocolError("AcceptedMeasurementClosure exceeds the root projection byte budget")
        failure.details = details
        def exceed_budget(*args, **kwargs):
            raise failure
        monkeypatch.setattr("meta_research.owners.research_graph.verify_accepted_closure", exceed_budget)
        owner = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        with pytest.raises(OwnerConflict) as caught:
            owner.finalize(handle=handle, evidence=evidence)
        assert caught.value.code == "target_root_measurement_closure_invalid"
        assert caught.value.details == details
        assert "technical capacity" in caught.value.feedback
        assert str(details["actual_bytes"]) in caught.value.feedback
        assert str(details["limit"]) in caught.value.feedback
        assert "at least" in caught.value.feedback
        assert "Do not truncate" in caught.value.feedback
        assert "rerun" in caught.value.feedback
        assert runtime.owners.research_graph.query_target_formal_results(handle.target_ref) == ()
    finally:
        runtime.close()


@pytest.mark.skipif(sys.platform == "win32", reason="native Target runtime requires POSIX file descriptors")
def test_rg_build_parses_once_but_rechecks_work_after_issuer_boundary(tmp_path, monkeypatch):
    import inspect
    import meta_research.formal_entities as formal
    from meta_research.target_run_finalizer import TargetRunFinalizer
    from test_target_root_finalizer import _EvidenceReader, _root_finalizer_fixture

    runtime, lifecycle, memory, authority, handle, workspace, evidence = _root_finalizer_fixture(tmp_path)
    try:
        parse = formal._declared_work
        callers = []
        def counted(document):
            callers.append(inspect.currentframe().f_back.f_code.co_name)
            return parse(document)
        monkeypatch.setattr(formal, "_declared_work", counted)
        owner = TargetRunFinalizer(lifecycle=lifecycle, memory=memory,
            workspace_resolver=runtime.target_run_authorities.agent_runtime,
            evidence_reader=_EvidenceReader(evidence), measurement_authority=runtime.owners.research_graph,
            graph_authority=runtime.owners.research_graph)
        accepted = owner.finalize(handle=handle, evidence=evidence)
        assert accepted.status == "completed"
        assert callers.count("accept_target_commit_from_root_completion") == 1
        assert not set(callers) & {"_root_reused_primary_binding", "primary_reused_references",
            "primary_evaluation_status", "_target_root_commit_material"}
        # Fresh issuer verification before the write still performs a fresh
        # domain validation instead of trusting the earlier mutable projection.
        assert callers.count("_target_root_domain_context_from_current") >= 2
    finally:
        runtime.close()
