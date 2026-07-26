import json
import os
from pathlib import Path

import pytest
import yaml

from orchestrator import database as db
from orchestrator.bundle_graph import BundleGraph, VerifiedPublication
from orchestrator.bundle_sources import (
    BundleSources,
    SourceBindingConflict,
    SourceBindingError,
    SourceMaterializationError,
)
from orchestrator.compiler_sqlite import SqliteCompiler


SOURCE_HASH = "afba9f33a217a18d3fe3b79e94095b2f4b8ff5c99f71e3d71c186d45a6e6c6b5"
MANIFEST_HASH = "a" * 64
SOURCE_REF = "pool/baselines/a/source"
SYSTEM_ROOT = Path(__file__).resolve().parent.parent
POLICY = yaml.safe_load(
    (SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))


class _PublicationVerifier:
    def __init__(self):
        self.publications = {}

    def verify(self, manifest_ref, expected_hash):
        publication = self.publications[manifest_ref]
        assert publication.manifest_hash == expected_hash
        return publication


def _registered_abc(tmp_path, *, admit=True, include_source=True):
    work_root = tmp_path / "work"
    source = work_root / SOURCE_REF
    source.mkdir(parents=True)
    (source / "model.py").write_bytes(b"VALUE = 1\n")

    conn = db.connect(":memory:")
    conn.execute(
        "INSERT INTO goal(id,version,text,predicate_json) VALUES (1,1,'g','{}')"
    )
    conn.execute(
        "INSERT INTO cycle(id,goal_id,goal_ver,status,policy_version) "
        "VALUES (1,1,1,'bundle','v-test')"
    )
    baseline_ids = {}
    for seq, key in enumerate(("A", "B", "C"), start=1):
        baseline_ids[key] = seq
        conn.execute(
            "INSERT INTO baseline("
            "id,slug,canonical_key,parent_id,born_cycle,status"
            ") VALUES (?,?,?,?,1,'planned')",
            (
                seq,
                key.lower(),
                key.lower(),
                None if key == "A" else baseline_ids["A"],
            ),
        )

    target_ids = {}
    for seq, key in enumerate(("A", "B", "C"), start=1):
        target_ids[key] = int(
            conn.execute(
                "INSERT INTO build_target("
                "cycle_id,target_kind,seq,status,baseline_id"
                ") VALUES (1,'build',?,'pending',?)",
                (seq, baseline_ids[key]),
            ).lastrowid
        )

    verifier = _PublicationVerifier()
    graph = BundleGraph(conn, publication_verifier=verifier)
    graph.register_plan(
        cycle_id=1,
        target_ids=target_ids,
        plan_targets=[
            {"target_key": "A", "depends_on": [], "gpu_required": False},
            {
                "target_key": "B",
                "depends_on": ["A"],
                "parent_baseline": {"target_key": "A"},
                "published_source_inputs": [
                    {"input_key": "base", "target_key": "A"}
                ],
                "gpu_required": False,
            },
            {
                "target_key": "C",
                "depends_on": ["A"],
                "parent_baseline": {"target_key": "A"},
                "published_source_inputs": [
                    {"input_key": "base", "target_key": "A"}
                ],
                "gpu_required": False,
            },
        ],
    )
    requests = {
        key: int(request_id)
        for key, request_id in conn.execute(
            "SELECT n.target_key,r.id FROM bundle_source_request r "
            "JOIN bundle_target_node n "
            "ON n.target_id=r.downstream_target_id "
            "ORDER BY n.target_key"
        )
    }

    if admit:
        conn.execute(
            "UPDATE baseline SET status='legal' WHERE id=?",
            (baseline_ids["A"],),
        )
        conn.execute(
            "INSERT INTO variant("
            "id,baseline_id,variant_key,config_json,status"
            ") VALUES (1,1,'default','{}','legal')"
        )
        conn.execute(
            "INSERT INTO protocol(id,version,name,scope_spec_json) "
            "VALUES (1,1,'p','{}')"
        )
        conn.execute(
            "INSERT INTO evaluation("
            "id,variant_id,protocol_id,protocol_ver,eval_key,source,status,"
            "created_cycle,build_target_id,target_set_hash"
            ") VALUES (1,1,1,1,'factory','factory','created',1,?,'set')",
            (target_ids["A"],),
        )
        conn.execute(
            "INSERT INTO evaluation_attempt("
            "id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,status"
            ") VALUES (1,1,1,?,1,'factory','success')",
            (target_ids["A"],),
        )
        conn.execute(
            "UPDATE evaluation "
            "SET status='success',canonical_attempt_id=1 WHERE id=1"
        )
        conn.execute(
            "UPDATE build_target "
            "SET status='complete',baseline_id=1,variant_id=1,evaluation_id=1 "
            "WHERE id=?",
            (target_ids["A"],),
        )
        conn.execute(
            "INSERT INTO phase_commit("
            "cycle_id,stage,target_id,artifact_hash"
            ") VALUES (1,'bundle',?,'phase-a')",
            (target_ids["A"],),
        )
        manifest_ref = f"pool/manifests/{MANIFEST_HASH}.json"
        decision_payload = {
            "schema": "meta-research-pool-db-binding/v1",
            "manifest_ref": manifest_ref,
            "manifest_hash": MANIFEST_HASH,
            "baseline_id": 1,
            "variant_id": 1,
            "evaluation_id": 1,
            "attempt_id": 1,
        }
        decision_id = int(
            conn.execute(
                "INSERT INTO decision(cycle_id,actor,type,payload_json) "
                "VALUES (1,'gate','pool_publication',?)",
                (
                    json.dumps(
                        decision_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            ).lastrowid
        )
        verifier.publications[manifest_ref] = VerifiedPublication(
            manifest_ref=manifest_ref,
            manifest_hash=MANIFEST_HASH,
            baseline_id=1,
            variant_id=1,
            evaluation_id=1,
            attempt_id=1,
            source_ref=SOURCE_REF if include_source else None,
            source_hash=SOURCE_HASH if include_source else None,
            source_hash_alg="sha256-tree-v1" if include_source else None,
        )
        graph.admit(
            target_id=target_ids["A"],
            publication_decision_id=decision_id,
        )

    return conn, work_root, target_ids, requests


def test_bind_records_the_exact_upstream_admission(tmp_path):
    conn, work_root, target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)

    binding = sources.bind(requests["B"])

    assert binding.request_id == requests["B"]
    assert binding.downstream_target_id == target_ids["B"]
    assert binding.upstream_target_id == target_ids["A"]
    assert binding.input_key == "base"
    assert binding.manifest_hash == MANIFEST_HASH
    assert binding.source_ref == SOURCE_REF
    assert binding.source_hash == SOURCE_HASH
    assert binding.source_hash_alg == "sha256-tree-v1"


def test_unbound_target_context_does_not_expose_published_input(tmp_path):
    conn, work_root, target_ids, _requests = _registered_abc(tmp_path)
    compiler = SqliteCompiler(conn, POLICY, work_root=work_root)

    pack = compiler.render(
        cycle_id="c1", stage="bundle", target_id=str(target_ids["B"]))

    assert "published-inputs" not in pack.anchor_md
    assert not any(
        item.get("kind") == "published_source_input"
        for item in pack.artifact_refs)


def test_bound_target_context_exposes_exact_materialized_input_only(tmp_path):
    conn, work_root, target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    binding = sources.bind(requests["B"])
    published_root = (
        work_root / "c1" / f"t{target_ids['B']}" / "published-inputs")
    published_root.mkdir(parents=True)
    with sources.materialize(
        requests["B"], target_directory=published_root, input_key="base",
    ):
        pass
    compiler = SqliteCompiler(conn, POLICY, work_root=work_root)

    pack = compiler.render(
        cycle_id="c1", stage="bundle", target_id=str(target_ids["B"]))

    refs = [
        item for item in pack.artifact_refs
        if item.get("kind") == "published_source_input"]
    assert refs == [{
        "kind": "published_source_input",
        "ref": str(published_root / "base"),
        "source": f"bundle_source_binding:{binding.binding_id}",
        "content_hash": "sha256-tree-v1:" + SOURCE_HASH,
    }]
    assert '"input_key": "base"' in pack.anchor_md
    assert f'"upstream_target_id": {target_ids["A"]}' in pack.anchor_md
    assert f'"upstream_admission_id": {binding.upstream_admission_id}' in (
        pack.anchor_md)
    assert f'"publication_decision_id": {binding.publication_decision_id}' in (
        pack.anchor_md)
    assert f'"manifest_ref": "{binding.manifest_ref}"' in pack.anchor_md
    assert f'"manifest_hash": "{MANIFEST_HASH}"' in pack.anchor_md
    assert '"worker_path": "published-inputs/base"' in pack.anchor_md
    assert SOURCE_REF not in pack.anchor_md
    assert all(SOURCE_REF not in item["ref"] for item in refs)


def test_bind_exact_replay_is_idempotent(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)

    first = sources.bind(requests["B"])
    replay = sources.bind(requests["B"])

    assert replay == first


def test_bind_fails_closed_if_durable_hash_conflicts(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    conn.execute("DROP TRIGGER trg_bundle_source_binding_noupd")
    conn.execute(
        "UPDATE bundle_source_binding SET source_hash=? WHERE request_id=?",
        ("c" * 64, requests["B"]),
    )

    with pytest.raises(SourceBindingConflict, match="conflicts"):
        sources.bind(requests["B"])


def test_bind_rejects_status_complete_without_exact_admission(tmp_path):
    conn, work_root, target_ids, requests = _registered_abc(
        tmp_path,
        admit=False,
    )
    conn.execute(
        "UPDATE build_target SET status='complete' WHERE id=?",
        (target_ids["A"],),
    )
    sources = BundleSources(conn, work_root=work_root)

    with pytest.raises(SourceBindingError, match="not admitted"):
        sources.bind(requests["B"])


def test_bind_rejects_admission_without_complete_source_identity(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(
        tmp_path,
        include_source=False,
    )
    sources = BundleSources(conn, work_root=work_root)

    with pytest.raises(SourceBindingError, match="incomplete source"):
        sources.bind(requests["B"])


def test_materialize_gives_b_and_c_independent_writable_capabilities(tmp_path):
    conn, work_root, target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    sources.bind(requests["C"])
    target_b = tmp_path / "targets" / "B"
    target_c = tmp_path / "targets" / "C"
    target_b.mkdir(parents=True)
    target_c.mkdir(parents=True)

    with sources.materialize(
        requests["B"],
        target_directory=target_b,
        input_key="base",
    ) as b_source, sources.materialize(
        requests["C"],
        target_directory=target_c,
        input_key="base",
    ) as c_source:
        b_file = Path(b_source.ref) / "model.py"
        c_file = Path(c_source.ref) / "model.py"
        upstream_file = work_root / SOURCE_REF / "model.py"

        assert b_source.downstream_target_id == target_ids["B"]
        assert c_source.downstream_target_id == target_ids["C"]
        assert b_source.source_hash == c_source.source_hash == SOURCE_HASH
        assert b_file.read_bytes() == c_file.read_bytes() == b"VALUE = 1\n"
        assert len(
            {
                os.stat(upstream_file).st_ino,
                os.stat(b_file).st_ino,
                os.stat(c_file).st_ino,
            }
        ) == 3

        b_file.write_bytes(b"VALUE = 2\n")
        assert upstream_file.read_bytes() == b"VALUE = 1\n"
        assert c_file.read_bytes() == b"VALUE = 1\n"


def test_materialize_reopens_exact_existing_destination_after_crash(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)

    with sources.materialize(
        requests["B"], target_directory=target, input_key="base",
    ) as first:
        first_path = Path(first.ref) / "model.py"
        first_inode = first_path.stat().st_ino

    with sources.materialize(
        requests["B"], target_directory=target, input_key="base",
    ) as recovered:
        recovered_path = Path(recovered.ref) / "model.py"
        assert recovered_path.read_bytes() == b"VALUE = 1\n"
        assert recovered_path.stat().st_ino == first_inode


@pytest.mark.parametrize("corruption", ["hash-drift", "symlink"])
def test_materialize_recovery_rejects_conflicting_existing_destination(
        tmp_path, corruption):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)
    with sources.materialize(
        requests["B"], target_directory=target, input_key="base",
    ):
        pass

    destination = target / "base"
    if corruption == "hash-drift":
        (destination / "model.py").write_bytes(b"VALUE = 9\n")
    else:
        real = target / "real-base"
        destination.rename(real)
        destination.symlink_to(real, target_is_directory=True)

    with pytest.raises(SourceMaterializationError):
        sources.materialize(
            requests["B"], target_directory=target, input_key="base")


def test_materialize_rejects_source_hash_drift(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    (work_root / SOURCE_REF / "model.py").write_bytes(b"VALUE = 9\n")
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)

    with pytest.raises(SourceMaterializationError, match="hash drifted"):
        sources.materialize(
            requests["B"],
            target_directory=target,
            input_key="base",
        )

    assert not (target / "base").exists()


def test_materialize_rejects_symlink_in_published_source(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    source = work_root / SOURCE_REF
    (source / "alias.py").symlink_to(source / "model.py")
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)

    with pytest.raises(SourceMaterializationError, match="symlink"):
        sources.materialize(
            requests["B"],
            target_directory=target,
            input_key="base",
        )

    assert not (target / "base").exists()


def test_materialize_rejects_missing_published_source(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    source = work_root / SOURCE_REF
    (source / "model.py").unlink()
    source.rmdir()
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)

    with pytest.raises(SourceMaterializationError, match="missing"):
        sources.materialize(
            requests["B"],
            target_directory=target,
            input_key="base",
        )

    assert not (target / "base").exists()


def test_materialize_rejects_input_key_path_traversal(tmp_path):
    conn, work_root, _target_ids, requests = _registered_abc(tmp_path)
    sources = BundleSources(conn, work_root=work_root)
    sources.bind(requests["B"])
    target = tmp_path / "targets" / "B"
    target.mkdir(parents=True)

    with pytest.raises(SourceMaterializationError, match="path-safe"):
        sources.materialize(
            requests["B"],
            target_directory=target,
            input_key="../escape",
        )

    assert not (target.parent / "escape").exists()
