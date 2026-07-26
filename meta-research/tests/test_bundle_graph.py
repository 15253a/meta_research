"""Bundle DAG public seam tests.

These tests deliberately use a real SQLite database and observe graph behaviour
only through :class:`BundleGraph`.  SQL is used solely to arrange pre-existing
domain facts that belong to the frozen v1 schema.
"""
from __future__ import annotations

import json

import pytest

from orchestrator import database as db
from orchestrator.bundle_graph import (
    AdmissionError,
    BundleGraph,
    GraphValidationError,
    VerifiedPublication,
)


class _UnusedPublicationVerifier:
    def verify(self, manifest_ref: str, expected_hash: str):
        raise AssertionError("this test must not verify a publication")


class _PublicationVerifier:
    def __init__(self):
        self.publications = {}

    def verify(self, manifest_ref: str, expected_hash: str):
        value = self.publications[manifest_ref]
        if isinstance(value, Exception):
            raise value
        assert expected_hash == value.manifest_hash
        return value


def _cycle_with_targets(*keys: str, parents=None):
    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (1,1,1,'bundle','v-test')")
    target_ids = {}
    baseline_ids = {}
    parents = dict(parents or {})
    for seq, key in enumerate(keys, 1):
        parent_key = parents.get(key)
        if parent_key is not None and parent_key not in baseline_ids:
            raise ValueError("test parent must precede its child")
        baseline_id = 1000 + seq
        conn.execute(
            "INSERT INTO baseline("
            "id,slug,canonical_key,parent_id,born_cycle,status) "
            "VALUES (?,?,?,?,1,'planned')",
            (
                baseline_id,
                f"graph-{key.lower()}",
                f"graph-{key.lower()}",
                None if parent_key is None else baseline_ids[parent_key],
            ),
        )
        baseline_ids[key] = baseline_id
        cursor = conn.execute(
            "INSERT INTO build_target("
            "cycle_id,target_kind,seq,status,baseline_id) "
            "VALUES (1,'build',?,'pending',?)",
            (seq, baseline_id),
        )
        target_ids[key] = int(cursor.lastrowid)
    return conn, target_ids


def test_register_plan_exposes_only_dependency_roots_as_ready():
    conn, target_ids = _cycle_with_targets(
        "A", "B", "C", parents={"B": "A", "C": "A"})
    graph = BundleGraph(conn, publication_verifier=_UnusedPublicationVerifier())

    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[
            {
                "target_key": "A",
                "depends_on": [],
                "resources": {"gpu_count": 0},
            },
            {
                "target_key": "B",
                "depends_on": ["A"],
                "parent_baseline": {"target_key": "A"},
                "published_source_inputs": [
                    {"input_key": "base", "target_key": "A"},
                ],
                "resources": {"gpu_count": 1},
            },
            {
                "target_key": "C",
                "depends_on": ["A"],
                "parent_baseline": {"target_key": "A"},
                "published_source_inputs": [
                    {"input_key": "base", "target_key": "A"},
                ],
                "resources": {"gpu_count": 2},
            },
        ],
    )

    assert [target.target_key for target in graph.ready_frontier(1)] == ["A"]
    overview = graph.overview(1)
    assert overview.ready == ("A",)
    assert {
        target.target_key: (target.depends_on, target.blocked_by, target.gpu_count)
        for target in overview.targets
    } == {
        "A": ((), (), 0),
        "B": (("A",), ("A",), 1),
        "C": (("A",), ("A",), 2),
    }


def test_plan_declaration_can_be_validated_before_domain_claims():
    conn = db.connect(":memory:")
    graph = BundleGraph(
        conn, publication_verifier=_UnusedPublicationVerifier())

    graph.validate_plan_declaration([
        {"target_key": "A", "depends_on": [], "gpu_required": False},
        {"target_key": "B", "depends_on": ["A"], "gpu_required": False},
    ])
    with pytest.raises(GraphValidationError, match="dependency cycle"):
        graph.validate_plan_declaration([
            {"target_key": "A", "depends_on": ["B"], "gpu_required": False},
            {"target_key": "B", "depends_on": ["A"], "gpu_required": False},
        ])

    assert conn.execute(
        "SELECT count(*) FROM bundle_target_node").fetchone() == (0,)


@pytest.mark.parametrize(
    "dependencies,match",
    [
        ({"A": ["missing"], "B": []}, "missing target"),
        ({"A": ["A"], "B": []}, "depends on itself"),
        ({"A": ["B"], "B": ["A"]}, "dependency cycle"),
    ],
)
def test_register_plan_rejects_invalid_graph_without_partial_registration(
        dependencies, match):
    conn, target_ids = _cycle_with_targets("A", "B")
    graph = BundleGraph(conn, publication_verifier=_UnusedPublicationVerifier())
    plan_targets = [
        {
            "target_key": key,
            "depends_on": dependencies[key],
            "resources": {"gpu_count": 0},
        }
        for key in ("A", "B")
    ]

    with pytest.raises(GraphValidationError, match=match):
        graph.register_plan(
            cycle_id=1, target_ids=target_ids, plan_targets=plan_targets)

    assert graph.overview(1).targets == ()


def test_register_plan_rejects_cross_cycle_target_identity():
    conn, target_ids = _cycle_with_targets("A")
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (2,1,1,'bundle','v-test')")
    other = conn.execute(
        "INSERT INTO build_target(cycle_id,target_kind,seq,status) "
        "VALUES (2,'build',1,'pending')").lastrowid
    target_ids["A"] = int(other)
    graph = BundleGraph(conn, publication_verifier=_UnusedPublicationVerifier())

    with pytest.raises(GraphValidationError, match="belongs to cycle 2"):
        graph.register_plan(
            cycle_id=1,
            target_ids=target_ids,
            plan_targets=[{
                "target_key": "A",
                "depends_on": [],
                "resources": {"gpu_count": 0},
            }],
        )


def test_identical_registration_replays_but_conflicting_registration_fails_closed():
    conn, target_ids = _cycle_with_targets("A")
    graph = BundleGraph(conn, publication_verifier=_UnusedPublicationVerifier())
    target = {
        "target_key": "A",
        "depends_on": [],
        "gpu_required": True,
        "resources": {"gpu_count": 2},
    }
    graph.register_plan(
        cycle_id=1, target_ids=target_ids, plan_targets=[target])
    graph.register_plan(
        cycle_id=1, target_ids=target_ids, plan_targets=[target])
    assert graph.overview(1).targets[0].gpu_count == 2

    conflicting = {**target, "resources": {"gpu_count": 1}}
    with pytest.raises(GraphValidationError, match="conflicts"):
        graph.register_plan(
            cycle_id=1,
            target_ids=target_ids,
            plan_targets=[conflicting],
        )
    assert graph.overview(1).targets[0].gpu_count == 2


def test_runnable_frontier_recovers_an_interrupted_nonterminal_target():
    conn, target_ids = _cycle_with_targets("A")
    graph = BundleGraph(
        conn, publication_verifier=_UnusedPublicationVerifier())
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[{
            "target_key": "A",
            "depends_on": [],
            "resources": {"gpu_count": 0},
        }],
    )
    conn.execute(
        "UPDATE build_target SET status='running' WHERE id=?",
        (target_ids["A"],),
    )

    assert graph.ready_frontier(1) == ()
    assert [target.target_id for target in graph.runnable_frontier(1)] == [
        target_ids["A"]]


def test_descendants_are_transitive_unique_and_deterministically_ordered():
    conn, target_ids = _cycle_with_targets("A", "B", "C", "D")
    graph = BundleGraph(conn, publication_verifier=_UnusedPublicationVerifier())
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[
            {"target_key": "A", "depends_on": [], "gpu_required": False},
            {"target_key": "B", "depends_on": ["A"], "gpu_required": False},
            {"target_key": "C", "depends_on": ["A"], "gpu_required": False},
            {
                "target_key": "D",
                "depends_on": ["B", "C"],
                "gpu_required": False,
            },
        ],
    )

    assert [target.target_key for target in graph.descendants(
        target_ids["A"])] == ["B", "C", "D"]


def _register_ab(conn, target_ids, verifier):
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[
            {"target_key": "A", "depends_on": [], "gpu_required": False},
            {"target_key": "B", "depends_on": ["A"], "gpu_required": False},
        ],
    )
    return graph


def _formal_target_facts(
        conn,
        target_id,
        verifier,
        *,
        phase_commit=True,
        formal_decision=True,
        legal=True,
):
    conn.execute(
        "INSERT INTO baseline(id,slug,canonical_key,status) VALUES (1,'a','a',?)",
        ("legal" if legal else "building",),
    )
    conn.execute(
        "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) "
        "VALUES (1,1,'default','{}',?)",
        ("legal" if legal else "building",),
    )
    conn.execute(
        "INSERT INTO protocol(id,version,name,scope_spec_json) "
        "VALUES (1,1,'p','{}')")
    conn.execute(
        "INSERT INTO evaluation("
        "id,variant_id,protocol_id,protocol_ver,eval_key,source,status,"
        "created_cycle,build_target_id,target_set_hash"
        ") VALUES (1,1,1,1,'factory','factory','created',1,?,'set')",
        (target_id,),
    )
    conn.execute(
        "INSERT INTO evaluation_attempt("
        "id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status"
        ") VALUES (1,1,1,?,1,'factory','success')",
        (target_id,),
    )
    conn.execute(
        "UPDATE evaluation SET status='success',canonical_attempt_id=1 WHERE id=1")
    conn.execute(
        "UPDATE build_target SET status='complete',baseline_id=1,variant_id=1,"
        "evaluation_id=1 WHERE id=?",
        (target_id,),
    )
    phase_id = None
    if phase_commit:
        phase_id = conn.execute(
            "INSERT INTO phase_commit(cycle_id,stage,target_id,artifact_hash) "
            "VALUES (1,'bundle',?,'phase-a')",
            (target_id,),
        ).lastrowid

    manifest_hash = "a" * 64
    manifest_ref = f"pool/manifests/{manifest_hash}.json"
    decision_id = None
    if formal_decision:
        payload = {
            "schema": "meta-research-pool-db-binding/v1",
            "manifest_ref": manifest_ref,
            "manifest_hash": manifest_hash,
            "baseline_id": 1,
            "variant_id": 1,
            "evaluation_id": 1,
            "attempt_id": 1,
        }
        decision_id = conn.execute(
            "INSERT INTO decision(cycle_id,actor,type,payload_json) "
            "VALUES (1,'gate','pool_publication',?)",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")),),
        ).lastrowid
    verifier.publications[manifest_ref] = VerifiedPublication(
        manifest_ref=manifest_ref,
        manifest_hash=manifest_hash,
        baseline_id=1,
        variant_id=1,
        evaluation_id=1,
        attempt_id=1,
        source_ref="pool/baselines/a/source",
        source_hash="b" * 64,
        source_hash_alg="sha256-tree-v1",
    )
    return int(decision_id or 999_999), phase_id, manifest_ref


def test_complete_target_does_not_unlock_dependent_before_exact_admission():
    conn, target_ids = _cycle_with_targets("A", "B")
    verifier = _PublicationVerifier()
    graph = _register_ab(conn, target_ids, verifier)
    decision_id, _phase_id, _manifest_ref = _formal_target_facts(
        conn, target_ids["A"], verifier)

    assert graph.ready_frontier(1) == ()
    assert graph.overview(1).targets[1].blocked_by == ("A",)

    admission = graph.admit(
        target_id=target_ids["A"],
        publication_decision_id=decision_id,
    )

    assert admission.target_id == target_ids["A"]
    assert [target.target_key for target in graph.ready_frontier(1)] == ["B"]
    assert graph.overview(1).targets[0].admitted is True


def test_admission_requires_exact_bundle_phase_commit():
    conn, target_ids = _cycle_with_targets("A")
    verifier = _PublicationVerifier()
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[{
            "target_key": "A", "depends_on": [], "gpu_required": False}],
    )
    decision_id, _phase_id, _manifest_ref = _formal_target_facts(
        conn, target_ids["A"], verifier, phase_commit=False)

    with pytest.raises(AdmissionError, match="phase commit"):
        graph.admit(
            target_id=target_ids["A"],
            publication_decision_id=decision_id,
        )


def test_admission_requires_legal_domain_objects():
    conn, target_ids = _cycle_with_targets("A")
    verifier = _PublicationVerifier()
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[{
            "target_key": "A", "depends_on": [], "gpu_required": False}],
    )
    decision_id, _phase_id, _manifest_ref = _formal_target_facts(
        conn, target_ids["A"], verifier, legal=False)

    with pytest.raises(AdmissionError, match="legal"):
        graph.admit(
            target_id=target_ids["A"],
            publication_decision_id=decision_id,
        )


def test_admission_reverifies_publication_and_rejects_hash_drift():
    conn, target_ids = _cycle_with_targets("A")
    verifier = _PublicationVerifier()
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[{
            "target_key": "A", "depends_on": [], "gpu_required": False}],
    )
    decision_id, _phase_id, manifest_ref = _formal_target_facts(
        conn, target_ids["A"], verifier)
    verifier.publications[manifest_ref] = ValueError("manifest hash drift")

    with pytest.raises(AdmissionError, match="verification failed"):
        graph.admit(
            target_id=target_ids["A"],
            publication_decision_id=decision_id,
        )


def test_identical_admission_replays_and_conflicting_verified_identity_fails_closed():
    conn, target_ids = _cycle_with_targets("A")
    verifier = _PublicationVerifier()
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[{
            "target_key": "A", "depends_on": [], "gpu_required": False}],
    )
    decision_id, _phase_id, manifest_ref = _formal_target_facts(
        conn, target_ids["A"], verifier)

    first = graph.admit(
        target_id=target_ids["A"],
        publication_decision_id=decision_id,
    )
    replay = graph.admit(
        target_id=target_ids["A"],
        publication_decision_id=decision_id,
    )
    assert replay == first

    verifier.publications[manifest_ref] = VerifiedPublication(
        manifest_ref=manifest_ref,
        manifest_hash="a" * 64,
        baseline_id=1,
        variant_id=1,
        evaluation_id=1,
        attempt_id=1,
        source_ref="pool/baselines/a/source",
        source_hash="c" * 64,
        source_hash_alg="sha256-tree-v1",
    )
    with pytest.raises(AdmissionError, match="conflicts"):
        graph.admit(
            target_id=target_ids["A"],
            publication_decision_id=decision_id,
        )
