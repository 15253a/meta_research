"""Formal pool publication: staging -> atomic directories -> DB bindings/cards/views inputs."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

import conftest
from orchestrator import database as db
from orchestrator import pool_publication as pool_publication_module
from orchestrator.pool_publication import (
    BaselinePublication,
    CheckpointPublication,
    EvaluationPublicationSpec,
    ImplementationRevisionPublication,
    PoolPublicationError,
    PoolPublisher,
    ProtocolPublication,
    TrainingPublicationSpec,
    VariantPublication,
    bind_database,
    bind_training_database,
    is_formally_published,
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write_canonical_manifest(work: Path, payload):
    raw = (json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    digest = _sha(raw)
    reference = f"pool/manifests/{digest}.json"
    (work / reference).write_bytes(raw)
    return reference, digest


def _sources(work: Path):
    cycle = work / "questions" / "q2" / "cycles" / "c2" / "artifacts"
    code = cycle / "src"
    code.mkdir(parents=True)
    (code / "model.py").write_text("def forward(x):\n    return x + 1\n", encoding="utf-8")
    (code / "layers").mkdir()
    (code / "layers" / "block.py").write_text("WIDTH = 8\n", encoding="utf-8")
    identity = cycle / "identity.md"
    identity.write_text("# Evidence model", encoding="utf-8")
    checkpoint = cycle / "fold0.pt"
    checkpoint.write_bytes(b"checkpoint-fold-0\x00")
    results = cycle / "evaluation"
    (results / "folds").mkdir(parents=True)
    (results / "eval.log").write_text("metric_value accuracy=0.91\n", encoding="utf-8")
    (results / "folds" / "fold0.json").write_text('{"accuracy":0.9}\n', encoding="utf-8")
    (results / "aggregate.json").write_text('{"accuracy":0.91}\n', encoding="utf-8")
    transcript = cycle / "process-receipt.json"
    transcript.write_text('{"exit_code":0}\n', encoding="utf-8")
    return identity, code, checkpoint, results, transcript


def _publish(work: Path):
    identity, code, checkpoint, results, transcript = _sources(work)
    publisher = PoolPublisher(work)
    training = publisher.publish_training(TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="evidence-model", canonical_key="arch:evidence-v1",
            identity_source=identity, code_source=code,
            repro_cmd_md="python -m train --config config.json"),
        variant=VariantPublication(
            variant_id=2, variant_key="base", config={"lr": 0.001, "seed": 7}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=None, ckpt_key="fold0", source=checkpoint,
            expected_sha256="sha256:" + _sha(checkpoint.read_bytes()))],
    ))
    publication = publisher.publish_evaluation(EvaluationPublicationSpec(
        training=training, evaluation_id=2, eval_key="lodo-v1", attempt_id=2,
        attempt_no=1, results_source=results, primary_artifact="eval.log",
        transcript_source=transcript,
        metrics=[
            {"metric_id": 1, "metric_ver": 1, "value": 0.90,
             "scope": "fold", "checkpoint_id": 2},
            {"metric_id": 1, "metric_ver": 1, "value": 0.91,
             "scope": "aggregate"},
        ],
        protocol=ProtocolPublication(
            protocol_id=2, version=1, name="LODO protocol 中文",
            scope_spec={"split": "leave-one-dataset-out", "datasets": ["A", "B"]}),
        checkpoint_ids={"fold0": 2},
    ))
    return publisher, training, publication, (identity, code, checkpoint, results, transcript)


def test_training_publication_copies_not_moves_and_checkpoint_binding_is_formal(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    identity, code, checkpoint, _results, _transcript = _sources(work)
    checkpoint_1 = checkpoint.with_name("fold1.pt")
    checkpoint_1.write_bytes(b"checkpoint-fold-1\x00")
    publisher = PoolPublisher(work)
    spec = TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="模型 family", canonical_key="canonical:model",
            identity_source=identity, code_source=code, repro_cmd_md="python train.py"),
        variant=VariantPublication(variant_id=2, variant_key="base", config={"seed": 3}),
        checkpoints=[
            CheckpointPublication(
                checkpoint_id=2, ckpt_key="fold0", source=checkpoint,
                expected_sha256=_sha(checkpoint.read_bytes())),
            CheckpointPublication(
                checkpoint_id=3, ckpt_key="fold1", source=checkpoint_1,
                expected_sha256=_sha(checkpoint_1.read_bytes())),
        ],
    )
    first = publisher.publish_training(spec)
    second = publisher.publish_training(spec)

    assert first.manifest_ref == second.manifest_ref
    assert first.manifest_hash == second.manifest_hash
    assert [item["ckpt_key"] for item in first.checkpoint_bindings] == ["fold0", "fold1"]
    binding = first.checkpoint_bindings[0]
    assert binding["path"].startswith("baselines/baseline-")
    assert "/variants/base/checkpoints/fold0/" in binding["path"]
    assert (work / binding["path"]).read_bytes() == checkpoint.read_bytes()
    assert binding["content_hash"] == _sha(checkpoint.read_bytes())
    assert identity.exists() and code.exists() and checkpoint.exists() and checkpoint_1.exists()  # 复制非剪切
    baseline = first.payload["objects"]["baseline"]
    assert (work / baseline["identity"]["path"]).read_text(encoding="utf-8") == (
        "# Evidence model\n\n## 复现命令\npython train.py")
    assert baseline["code"]["hash_alg"] == "sha256-tree-v1"
    assert list((work / ".pool-staging").iterdir()) == []


def test_training_revision_is_run_bound_append_only_and_replay_safe(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    identity, code, checkpoint, _results, _transcript = _sources(work)
    publisher = PoolPublisher(work)
    initial = publisher.publish_training(TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="evidence-model",
            canonical_key="arch:evidence-v1",
            identity_source=identity, code_source=code,
            repro_cmd_md="python train.py"),
        variant=VariantPublication(
            variant_id=2, variant_key="base", config={"seed": 7}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=2, ckpt_key="fold0", source=checkpoint)],
    ))
    initial_code = initial.payload["objects"]["baseline"]["code"]
    initial_checkpoint = initial.checkpoint_bindings[0]
    initial_code_bytes = (work / initial_code["path"] / "model.py").read_bytes()
    initial_checkpoint_bytes = (work / initial_checkpoint["path"]).read_bytes()

    revised_code = work / "questions" / "q2" / "cycles" / "c2" / "revised-src"
    revised_code.mkdir()
    (revised_code / "model.py").write_text(
        "def forward(x):\n    return x + 2\n", encoding="utf-8")
    revised_checkpoint = revised_code.parent / "fold0-v2.pt"
    revised_checkpoint.write_bytes(b"checkpoint-fold-0-v2\x00")
    revision_spec = TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="evidence-model",
            canonical_key="arch:evidence-v1"),
        variant=VariantPublication(
            variant_id=2, variant_key="base", config={"seed": 7}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=3, ckpt_key="fold0-r7",
            source=revised_checkpoint)],
        implementation_revision=ImplementationRevisionPublication(
            run_id=7, code_source=revised_code),
    )
    first = publisher.publish_training(revision_spec)
    second = publisher.publish_training(revision_spec)

    assert first.manifest_ref == second.manifest_ref
    assert first.payload["mode"] == "revision"
    objects = first.payload["objects"]
    revision = objects["implementation_revision"]
    variant_root = objects["variant"]["root"]
    assert revision["run_id"] == 7
    assert revision["root"] == f"{variant_root}/revisions/run-7"
    assert revision["code"]["path"] == f"{revision['root']}/src"
    assert (work / revision["code"]["path"] / "model.py").read_text(
        encoding="utf-8").endswith("return x + 2\n")
    assert first.checkpoint_bindings[0]["path"].startswith(
        f"{revision['root']}/checkpoints/fold0-r7/")
    assert objects["baseline"]["code"] == initial_code
    assert (work / initial_code["path"] / "model.py").read_bytes() == initial_code_bytes
    assert (work / initial_checkpoint["path"]).read_bytes() == initial_checkpoint_bytes
    assert publisher.verify_training(
        initial.manifest_ref, expected_hash=initial.manifest_hash).payload == initial.payload

    (revised_code / "model.py").write_text(
        "def forward(x):\n    return x - 999\n", encoding="utf-8")
    with pytest.raises(PoolPublicationError, match="不同内容占用"):
        publisher.publish_training(revision_spec)
    assert (work / revision["code"]["path"] / "model.py").read_text(
        encoding="utf-8").endswith("return x + 2\n")
    assert list((work / ".pool-staging").iterdir()) == []


def test_complete_publication_is_content_addressed_multi_checkpoint_ready_and_replay_safe(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    publisher, training, first, sources = _publish(work)
    second = publisher.publish_evaluation(EvaluationPublicationSpec(
        training=training, evaluation_id=2, eval_key="lodo-v1", attempt_id=2,
        attempt_no=1, results_source=sources[3], primary_artifact="eval.log",
        transcript_source=sources[4],
        metrics=[
            {"metric_id": 1, "metric_ver": 1, "value": 0.90,
             "scope": "fold", "checkpoint_id": 2},
            {"metric_id": 1, "metric_ver": 1, "value": 0.91,
             "scope": "aggregate"},
        ],
        protocol=ProtocolPublication(
            protocol_id=2, version=1, name="LODO protocol 中文",
            scope_spec={"datasets": ["A", "B"], "split": "leave-one-dataset-out"}),
        checkpoint_ids={"fold0": 2},
    ))

    assert first.manifest_ref == second.manifest_ref
    assert first.manifest_hash == second.manifest_hash
    manifest = work / first.manifest_ref
    assert manifest.name == f"{_sha(manifest.read_bytes())}.json"
    evaluation = first.payload["objects"]["evaluation"]
    assert (work / evaluation["primary_artifact"]["path"]).read_text(
        encoding="utf-8").startswith("metric_value")
    assert (work / evaluation["attempt"]["path"] / "folds" / "fold0.json").exists()
    assert (work / evaluation["attempt"]["path"] / "metric_results.json").exists()
    protocol = first.payload["objects"]["protocol"]
    assert (work / protocol["spec"]["path"]).read_text(encoding="utf-8").startswith(
        "# LODO protocol 中文 @ 1")
    assert protocol["root"] == "protocols/p2@1"
    assert first.database_bindings["evaluation_attempt"]["artifact_ref"].startswith("sha256:")
    retry = publisher.publish_evaluation(EvaluationPublicationSpec(
        training=training, evaluation_id=2, eval_key="lodo-v1", attempt_id=4,
        attempt_no=2, results_source=sources[3], primary_artifact="eval.log",
        metrics=[{"metric_id": 1, "metric_ver": 1, "value": 0.92,
                  "scope": "aggregate"}],
        protocol=ProtocolPublication(
            protocol_id=2, version=1, name="LODO protocol 中文",
            scope_spec={"datasets": ["A", "B"], "split": "leave-one-dataset-out"}),
        checkpoint_ids={"fold0": 2},
    ))
    retry_root = work / retry.payload["objects"]["evaluation"]["root"] / "attempts"
    assert (retry_root / "1").is_dir() and (retry_root / "2").is_dir()
    assert list((work / ".pool-staging").iterdir()) == []


def test_exec_variant_extends_only_an_existing_formal_baseline(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    publisher, initial_training, _initial, sources = _publish(work)
    next_checkpoint = sources[2].with_name("ablation-fold0.pt")
    next_checkpoint.write_bytes(b"ablation-checkpoint\x00")
    training = publisher.publish_training(TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="evidence-model", canonical_key="arch:evidence-v1"),
        variant=VariantPublication(
            variant_id=3, variant_key="no-residual", config={"lr": 0.001, "residual": False}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=None, ckpt_key="fold0", source=next_checkpoint)],
    ))
    assert training.payload["mode"] == "variant"
    assert (training.payload["objects"]["baseline"]["code"]["sha256"]
            == initial_training.payload["objects"]["baseline"]["code"]["sha256"])
    assert (work / training.payload["objects"]["variant"]["config_asset"]["path"]).exists()
    published = publisher.publish_evaluation(EvaluationPublicationSpec(
        training=training, evaluation_id=3, eval_key="lodo-ablation", attempt_id=3,
        attempt_no=1, results_source=sources[3], primary_artifact="eval.log",
        metrics=[{"metric_id": 1, "metric_ver": 1, "value": 0.89,
                  "scope": "aggregate"}],
        protocol=ProtocolPublication(
            protocol_id=2, version=1, name="LODO protocol 中文",
            scope_spec={"split": "leave-one-dataset-out", "datasets": ["A", "B"]}),
        checkpoint_ids={"fold0": 3},
    ))
    assert published.payload["objects"]["variant"]["variant_id"] == 3
    assert "/variants/no-residual/evaluations/lodo-ablation/" in (
        published.payload["objects"]["evaluation"]["attempt"]["path"] + "/")


def test_hash_mismatch_and_destination_collision_fail_without_overwrite(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    identity, code, checkpoint, _results, _transcript = _sources(work)
    publisher = PoolPublisher(work)
    bad = TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="bad", canonical_key="bad-key",
            identity_source=identity, code_source=code, repro_cmd_md="python train.py"),
        variant=VariantPublication(variant_id=2, variant_key="base", config={"seed": 1}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=2, ckpt_key="fold0", source=checkpoint,
            expected_sha256="0" * 64)],
    )
    with pytest.raises(PoolPublicationError, match="hash 不符"):
        publisher.publish_training(bad)
    assert not any((work / "baselines").iterdir())
    assert list((work / ".pool-staging").iterdir()) == []

    good = TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="model", canonical_key="same-key",
            identity_source=identity, code_source=code, repro_cmd_md="python train.py"),
        variant=VariantPublication(variant_id=2, variant_key="base", config={"seed": 1}),
        checkpoints=[CheckpointPublication(
            checkpoint_id=2, ckpt_key="fold0", source=checkpoint)],
    )
    receipt = publisher.publish_training(good)
    formal_checkpoint = work / receipt.checkpoint_bindings[0]["path"]
    original = formal_checkpoint.read_bytes()
    (code / "model.py").write_text("def forward(x): return x - 1\n", encoding="utf-8")
    with pytest.raises(PoolPublicationError, match="不同内容占用"):
        publisher.publish_training(good)
    assert formal_checkpoint.read_bytes() == original
    assert list((work / ".pool-staging").iterdir()) == []


def test_manifest_publish_race_is_create_if_absent_not_overwrite(tmp_path, monkeypatch):
    """A winner created after the precheck remains immutable."""
    work = tmp_path / "work"
    work.mkdir()
    PoolPublisher(work)
    target = work / "pool" / "manifests" / ("f" * 64 + ".json")
    real_link = pool_publication_module.os.link

    def racing_link(source, destination, *, follow_symlinks=False):
        Path(destination).write_bytes(b"other")
        return real_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(pool_publication_module.os, "link", racing_link)
    with pytest.raises(PoolPublicationError, match="竞态碰撞"):
        pool_publication_module._atomic_publish_file(target, b"loser")
    assert target.read_bytes() == b"other"
    assert not list(target.parent.glob(f".{target.name}.tmp-*"))


def test_symlink_source_rejected_and_published_tamper_detected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    identity, code, checkpoint, _results, _transcript = _sources(work)
    (code / "escape.py").symlink_to("/etc/passwd")
    publisher = PoolPublisher(work)
    spec = TrainingPublicationSpec(
        baseline=BaselinePublication(
            baseline_id=2, slug="model", canonical_key="symlink-key",
            identity_source=identity, code_source=code, repro_cmd_md="python train.py"),
        variant=VariantPublication(variant_id=2, variant_key="base", config={"seed": 1}),
        checkpoints=[CheckpointPublication(checkpoint_id=2, ckpt_key="fold0", source=checkpoint)],
    )
    with pytest.raises(PoolPublicationError, match="symlink"):
        publisher.publish_training(spec)
    assert list((work / ".pool-staging").iterdir()) == []
    (code / "escape.py").unlink()
    receipt = publisher.publish_training(spec)
    formal = work / receipt.checkpoint_bindings[0]["path"]
    formal.write_bytes(b"tampered")
    with pytest.raises(PoolPublicationError, match="checkpoint hash 失配"):
        publisher.verify_training(receipt.manifest_ref, expected_hash=receipt.manifest_hash)


def test_content_addressed_manifest_outside_formal_namespace_is_rejected(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    publisher, training, publication, _sources_ = _publish(work)
    impostor_dir = work / "questions" / "q2" / "cycles" / "c2" / "artifacts"
    training_impostor = impostor_dir / Path(training.manifest_ref).name
    training_impostor.write_bytes((work / training.manifest_ref).read_bytes())
    publication_impostor = impostor_dir / Path(publication.manifest_ref).name
    publication_impostor.write_bytes((work / publication.manifest_ref).read_bytes())

    with pytest.raises(PoolPublicationError, match="正式 pool/manifests"):
        publisher.verify_training(
            training_impostor.relative_to(work).as_posix(),
            expected_hash=training.manifest_hash,
        )
    with pytest.raises(PoolPublicationError, match="正式 pool/manifests"):
        publisher.verify_publication(
            publication_impostor.relative_to(work).as_posix(),
            expected_hash=publication.manifest_hash,
        )


def test_canonical_training_manifest_cannot_bless_cycle_staging_assets(tmp_path):
    """Correct namespace for the JSON never confers authority on arbitrary refs."""
    work = tmp_path / "work"
    work.mkdir()
    publisher, training, _publication, _sources_ = _publish(work)
    formal = training.payload["objects"]
    forged = work / "questions" / "q9" / "cycles" / "c9" / "artifacts" / "forged"
    forged.mkdir(parents=True)
    shutil.copyfile(
        work / formal["baseline"]["identity"]["path"], forged / "identity.md")
    shutil.copytree(work / formal["baseline"]["code"]["path"], forged / "src")
    shutil.copyfile(
        work / formal["variant"]["config_asset"]["path"], forged / "config.json")
    shutil.copyfile(
        work / formal["checkpoints"][0]["path"], forged / "checkpoint.bin")

    mutations = [
        ("baseline identity", ("baseline", "identity", "path"), forged / "identity.md"),
        ("baseline code", ("baseline", "code", "path"), forged / "src"),
        ("variant config", ("variant", "config_asset", "path"), forged / "config.json"),
        ("checkpoint", ("checkpoints", 0, "path"), forged / "checkpoint.bin"),
    ]
    for label, keys, replacement in mutations:
        payload = copy.deepcopy(training.payload)
        target = payload["objects"]
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = replacement.relative_to(work).as_posix()
        reference, digest = _write_canonical_manifest(work, payload)
        with pytest.raises(PoolPublicationError, match="formal namespace"):
            publisher.verify_training(reference, expected_hash=digest)


def test_complete_manifest_cannot_detach_from_training_or_other_formal_namespaces(tmp_path):
    """Complete manifests extend, rather than redefine, their training identity."""
    work = tmp_path / "work"
    work.mkdir()
    publisher, _training, publication, _sources_ = _publish(work)
    objects = publication.payload["objects"]
    forged = work / "questions" / "q8" / "cycles" / "c8" / "artifacts" / "forged"
    forged.mkdir(parents=True)
    shutil.copyfile(work / objects["checkpoints"][0]["path"], forged / "checkpoint.bin")
    shutil.copyfile(work / objects["protocol"]["spec"]["path"], forged / "spec.md")
    shutil.copyfile(
        work / objects["evaluation"]["primary_artifact"]["path"], forged / "eval.log")
    shutil.copytree(
        work / objects["evaluation"]["attempt"]["path"], forged / "attempt")

    # Old verification never compared complete checkpoint objects with the
    # referenced training manifest, so a DB checkpoint row could be redirected
    # to staging while retaining the same bytes/hash.
    payload = copy.deepcopy(publication.payload)
    checkpoint_ref = (forged / "checkpoint.bin").relative_to(work).as_posix()
    payload["objects"]["checkpoints"][0]["path"] = checkpoint_ref
    payload["db_bindings"]["checkpoints"][0]["path"] = checkpoint_ref
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="training_manifest objects"):
        publisher.verify_publication(reference, expected_hash=digest)

    # Byte-identical protocol and evaluation files outside their exact derived
    # namespace are likewise not formal assets.
    payload = copy.deepcopy(publication.payload)
    payload["objects"]["protocol"]["spec"]["path"] = (
        forged / "spec.md").relative_to(work).as_posix()
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="formal namespace"):
        publisher.verify_publication(reference, expected_hash=digest)

    payload = copy.deepcopy(publication.payload)
    primary_ref = (forged / "eval.log").relative_to(work).as_posix()
    payload["objects"]["evaluation"]["primary_artifact"]["path"] = primary_ref
    payload["db_bindings"]["evaluation_attempt"]["execution_log_ref"] = primary_ref
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="formal attempt namespace"):
        publisher.verify_publication(reference, expected_hash=digest)

    payload = copy.deepcopy(publication.payload)
    attempt_ref = (forged / "attempt").relative_to(work).as_posix()
    payload["objects"]["evaluation"]["attempt"]["path"] = attempt_ref
    payload["units"][1]["path"] = attempt_ref
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="formal namespace"):
        publisher.verify_publication(reference, expected_hash=digest)


def test_manifest_identity_fields_are_linked_to_their_formal_bytes(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    publisher, training, publication, _sources_ = _publish(work)

    payload = copy.deepcopy(training.payload)
    payload["objects"]["variant"]["config"] = {"lr": 99, "seed": 7}
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="config.json.*脱钩"):
        publisher.verify_training(reference, expected_hash=digest)

    payload = copy.deepcopy(publication.payload)
    payload["db_bindings"]["evaluation_attempt"]["artifact_ref"] = "sha256:" + "0" * 64
    reference, digest = _write_canonical_manifest(work, payload)
    with pytest.raises(PoolPublicationError, match="db_bindings.*脱钩"):
        publisher.verify_publication(reference, expected_hash=digest)


def _seed_publication_db(conn, publication):
    conftest.seed_minimal(conn)
    objects = publication.payload["objects"]
    baseline, variant = objects["baseline"], objects["variant"]
    protocol, evaluation = objects["protocol"], objects["evaluation"]
    checkpoint = objects["checkpoints"][0]
    binding = publication.database_bindings["evaluation_attempt"]
    conn.execute(
        "INSERT INTO baseline(id,slug,canonical_key,identity_doc,status) VALUES (?,?,?,?, 'planned')",
        (baseline["baseline_id"], baseline["slug"], baseline["canonical_key"],
         baseline["identity_doc"]))
    conn.execute(
        "INSERT INTO variant(id,baseline_id,variant_key,config_json,status) VALUES (?,?,?,?, 'planned')",
        (variant["variant_id"], baseline["baseline_id"], variant["variant_key"],
         json.dumps(variant["config"], sort_keys=True, separators=(",", ":"))))
    conn.execute(
        "INSERT INTO checkpoint(id,variant_id,ckpt_key,path,content_hash,hash_alg,"
        "artifact_type,origin) VALUES (?,?,?,?,?,?,'algorithm','none')",
        (checkpoint["checkpoint_id"], variant["variant_id"], checkpoint["ckpt_key"],
         checkpoint["path"], checkpoint["content_hash"], checkpoint["hash_alg"]))
    conn.execute(
        "INSERT INTO protocol(id,version,name,scope_spec_json) VALUES (?,?,?,?)",
        (protocol["protocol_id"], protocol["version"], protocol["name"],
         json.dumps(protocol["scope_spec"], sort_keys=True, separators=(",", ":"))))
    conn.execute(
        "INSERT INTO protocol_metric(protocol_id,protocol_ver,metric_id,metric_ver) VALUES (?, ?, 1, 1)",
        (protocol["protocol_id"], protocol["version"]))
    conn.execute(
        "INSERT INTO build_target(id,cycle_id,question_id,target_kind,seq,status,variant_id,"
        "eval_action,eval_key,evaluation_source) "
        "VALUES (3,1,1,'eval',3,'complete',2,'create_evaluation',?,'factory')",
        (evaluation["eval_key"],))
    conn.execute(
        "INSERT INTO evaluation(id,variant_id,protocol_id,protocol_ver,eval_key,source,status,"
        "created_cycle,build_target_id,target_set_hash) VALUES (?,?,?,?,?,'factory','created',1,3,'set')",
        (evaluation["evaluation_id"], variant["variant_id"], protocol["protocol_id"],
         protocol["version"], evaluation["eval_key"]))
    conn.execute(
        "INSERT INTO evaluation_attempt(id,evaluation_id,cycle_id,build_target_id,attempt_no,purpose,"
        "status,artifact_ref,transcript_ref) VALUES (?,?,1,3,?,'factory','success',?,?)",
        (evaluation["attempt_id"], evaluation["evaluation_id"], evaluation["attempt_no"],
         binding["artifact_ref"], publication.manifest_ref))
    for metric in evaluation["metrics"]:
        conn.execute(
            "INSERT INTO metric_result(evaluation_id,evaluation_attempt_id,checkpoint_id,metric_id,"
            "metric_ver,value,scope) VALUES (?,?,?,?,?,?,?)",
            (evaluation["evaluation_id"], evaluation["attempt_id"], metric.get("checkpoint_id"),
             metric["metric_id"], metric["metric_ver"], metric["value"], metric["scope"]))
    conn.execute("UPDATE evaluation SET status='success',canonical_attempt_id=? WHERE id=?",
                 (evaluation["attempt_id"], evaluation["evaluation_id"]))
    conn.commit()


def test_database_binding_requires_formal_log_then_atomically_materializes_cards_and_event(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _publisher, _training, publication, _sources_ = _publish(work)
    conn = db.connect(":memory:")
    _seed_publication_db(conn, publication)
    conn.execute("UPDATE baseline SET status='legal' WHERE id=1")
    conn.execute("UPDATE variant SET status='legal' WHERE id=1")
    assert is_formally_published(conn, variant_id=1) is False  # status-only legacy row
    bind_training_database(
        conn, publication.training, updated_cycle=1,
        checkpoint_ids={"fold0": 2})
    bind_training_database(
        conn, publication.training, updated_cycle=1,
        checkpoint_ids={"fold0": 2})
    conn.commit()

    with pytest.raises(PoolPublicationError, match="formal evaluation execution_log"):
        bind_database(conn, publication, updated_cycle=1)
    assert conn.execute("SELECT code_ref FROM baseline WHERE id=2").fetchone()[0] is not None
    assert conn.execute("SELECT count(*) FROM card").fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM decision WHERE type='pool_training_publication'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM decision WHERE type='pool_publication'").fetchone()[0] == 0

    binding = publication.database_bindings["evaluation_attempt"]
    conn.execute(
        "INSERT INTO execution_log(evaluation_attempt_id,cycle_id,log_kind,ref,content_hash) "
        "VALUES (2,1,'eval',?,?)",
        (binding["execution_log_ref"], binding["execution_log_hash"]))
    bind_database(conn, publication, updated_cycle=1)
    bind_database(conn, publication, updated_cycle=1)  # exact replay: no duplicate event/card
    conn.commit()

    baseline = publication.payload["objects"]["baseline"]
    assert conn.execute("SELECT code_ref,commit_hash,status FROM baseline WHERE id=2").fetchone() == (
        baseline["code"]["path"],
        "sha256-tree-v1:" + baseline["code"]["sha256"], "planned")
    assert conn.execute("SELECT count(*) FROM decision WHERE type='pool_publication'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM card WHERE card_type IN ('baseline','variant','protocol')").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM card WHERE src_hash='' OR stale<>0").fetchone()[0] == 0
    assert is_formally_published(conn, variant_id=2) is False  # binding alone never changes status
    conn.execute("UPDATE baseline SET status='legal' WHERE id=2")
    conn.execute("UPDATE variant SET status='legal' WHERE id=2")
    assert is_formally_published(conn, variant_id=2) is True
    conn.close()
