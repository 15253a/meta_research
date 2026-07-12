"""Bounded source projection tests for reviewed repository adapter generation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import conftest

from orchestrator import database as db
from orchestrator.artifact_capability import ArtifactCapabilityError
from orchestrator.cost_ledger import CostLedger
from orchestrator.interfaces import Artifact, CallUsage
from orchestrator.repository_adapter_generation import AdapterGenerationService
from orchestrator.repository_adapter_projection import (
    build_adapter_source_projection,
)
from orchestrator.repository_materialization_common import (
    RepositoryMaterializationError,
    _canonical,
    _git_blob_sha1,
    _sha256,
    _value_hash,
)
from orchestrator.runner import RunnerError
from orchestrator.schemas import SchemaSet
from orchestrator.writedaemon import WriteDaemon


_REPOSITORY = "acme/model"
_REVISION = "a" * 40
_ROOT_TREE_SHA = "b" * 40
_ADAPTER_PATH = ".meta-research/import-adapter.json"
_LOCK_NAMES = (
    "python-wheel-lock.json", "requirements.lock", "requirements.txt",
    "poetry.lock", "uv.lock", "conda-lock.yml", "package-lock.json",
    "pnpm-lock.yaml", "yarn.lock", "Cargo.lock",
)
_CONFIG = {
    "provider": "codex-reviewed-sidecar-v1",
    "prompt_version": 1,
    "max_inventory_paths": 3,
    "max_inventory_bytes": 2048,
    "max_preview_files": 2,
    "max_preview_file_bytes": 32,
    "max_preview_total_bytes": 20,
    "max_projection_bytes": 16384,
}
_SYSTEM_ROOT = Path(__file__).resolve().parent.parent
_POLICY = yaml.safe_load(
    (_SYSTEM_ROOT / "policies" / "policy.yaml").read_text(encoding="utf-8"))
_NO_BUDGET_POLICY = {
    **_POLICY,
    "budget": {**_POLICY["budget"], "session_max": None},
}
_SCHEMAS = SchemaSet(_SYSTEM_ROOT / "schemas")
_USAGE = CallUsage(
    tokens_total=1, wallclock_sec=0.01, tokens_known=True)
_GENERATOR_TRANSCRIPT = "generator-private-transcript-marker"


def _tree_and_ledger(tmp_path: Path, files: dict[str, bytes]):
    tree = tmp_path / "tree"
    tree.mkdir()
    ledger = []
    for relpath, payload in sorted(files.items()):
        path = tree / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        ledger.append({
            "path": relpath,
            "sha256": _sha256(payload),
            "bytes": len(payload),
            "git_blob_sha1": _git_blob_sha1(payload),
            "git_mode": "100644",
            "repository": _REPOSITORY,
            "revision": _REVISION,
        })
    return tree, ledger


def _project(tree: Path, ledger, *, owner_guard=lambda: None):
    return build_adapter_source_projection(
        tree_root=tree,
        ledger=ledger,
        repository=_REPOSITORY,
        revision=_REVISION,
        root_tree_sha=_ROOT_TREE_SHA,
        adapter_path=_ADAPTER_PATH,
        dependency_lock_names=_LOCK_NAMES,
        dependency_lock_basename="python-wheel-lock.json",
        config=_CONFIG,
        owner_guard=owner_guard,
    )


def test_projection_is_deterministic_and_preview_is_policy_bounded(tmp_path):
    tree, ledger = _tree_and_ledger(tmp_path, {
        "README.md": b"x" * 33,       # exceeds the per-preview bound
        "README.txt": b"short\n",
        "artifact.bin": b"model",
        "eval.py": b"print(1)\n",
        "notes.txt": b"third\n",     # excluded by max_preview_files
    })
    guard_calls = 0

    def guard():
        nonlocal guard_calls
        guard_calls += 1

    first = _project(tree, ledger, owner_guard=guard)
    second = _project(tree, ledger, owner_guard=guard)

    assert first == second
    assert first["inventory_truncated"] is True
    assert len(first["inventory"]) == _CONFIG["max_inventory_paths"]
    assert [item["path"] for item in first["previews"]] == [
        "README.txt", "eval.py"]
    assert len(first["previews"]) <= _CONFIG["max_preview_files"]
    assert all(item["bytes"] <= _CONFIG["max_preview_file_bytes"]
               for item in first["previews"])
    assert first["preview_total_bytes"] == sum(
        item["bytes"] for item in first["previews"])
    assert first["preview_total_bytes"] <= _CONFIG["max_preview_total_bytes"]
    assert len(_canonical(first)) <= _CONFIG["max_projection_bytes"]
    unhashed = dict(first)
    projection_hash = unhashed.pop("projection_hash")
    assert projection_hash == _value_hash(unhashed)
    assert guard_calls > 0


@pytest.mark.parametrize(("lock_path", "expected"), [
    (None, {
        "adapter_version": 2,
        "dependency_mode": "pinned_image_only",
        "dependency_locks": [],
    }),
    (".meta-research/python-wheel-lock.json", {
        "adapter_version": 3,
        "dependency_mode": "python_wheel_image_v1",
        "dependency_locks": [".meta-research/python-wheel-lock.json"],
    }),
])
def test_projection_freezes_v2_or_unique_python_wheel_v3_contract(
        tmp_path, lock_path, expected):
    files = {"README.md": b"usage\n", "artifact.bin": b"model"}
    if lock_path is not None:
        files[lock_path] = b"{}\n"
    tree, ledger = _tree_and_ledger(tmp_path, files)

    assert _project(tree, ledger)["dependency_contract"] == expected


def test_projection_marks_unavailable_lock_without_installing_it(tmp_path):
    tree, ledger = _tree_and_ledger(tmp_path, {
        "README.md": b"usage\n", "requirements.txt": b"package==1\n"})

    projection = _project(tree, ledger)

    assert projection["dependency_contract"] == {
        "adapter_version": 2,
        "dependency_mode": "pinned_image_only",
        "dependency_locks": [],
    }
    assert projection["unavailable_dependency_locks"] == ["requirements.txt"]


def test_projection_prefers_shallow_entrypoint_over_tests_and_nested_copies(
        tmp_path):
    tree, ledger = _tree_and_ledger(tmp_path, {
        "README.md": b"use\n",
        "main.py": b"print(1)\n",
        "examples/deep/main.py": b"print(2)\n",
        "test_unit.py": b"assert 1\n",
    })

    projection = _project(tree, ledger)

    assert [item["path"] for item in projection["previews"]] == [
        "README.md", "main.py"]


def test_projection_rejects_ambiguous_python_lock_before_reads(tmp_path):
    tree, ledger = _tree_and_ledger(tmp_path, {
        "README.md": b"usage\n",
        "a/python-wheel-lock.json": b"{}\n",
        "b/python-wheel-lock.json": b"{}\n",
    })

    def forbidden_read():
        pytest.fail("dependency lock rejection must happen before source reads")

    with pytest.raises(RepositoryMaterializationError, match="多份 python wheel lock"):
        _project(tree, ledger, owner_guard=forbidden_read)


@pytest.mark.parametrize(("drift", "match"), [
    ("sha256", "sha256"),
    ("bytes", "size"),
])
def test_projection_rejects_ledger_hash_or_size_drift(tmp_path, drift, match):
    tree, ledger = _tree_and_ledger(tmp_path, {"README.md": b"usage\n"})
    if drift == "sha256":
        ledger[0]["sha256"] = "sha256:" + "0" * 64
    else:
        ledger[0]["bytes"] += 1

    with pytest.raises(ArtifactCapabilityError, match=match):
        _project(tree, ledger)


def _adapter():
    return {
        "version": 2,
        "artifact_relpath": "artifact.bin",
        "artifact_type": "external_model",
        "smoke_argv": ["python", "-c", "print('loaded')"],
        "eval_argv": ["python", "eval.py", "{artifact}"],
        "dependency_mode": "pinned_image_only",
        "dependency_locks": [],
        "factory_protocol": {
            "name": "adapter-test", "version": 1,
            "scope_spec": {"split": "factory"},
            "metrics": [{
                "log_key": "score", "name": "score", "version": 1,
                "direction": "higher", "unit": None,
                "compute_spec": "Read one finite score from eval output.",
                "readout_rule": "Use the emitted score value.",
            }],
            "required": ["score"],
        },
    }


def _anchor_value(pack, key):
    prefix = key + "="
    matches = [
        line[len(prefix):] for line in pack.anchor_md.splitlines()
        if line.startswith(prefix)]
    assert len(matches) == 1
    return matches[0]


def _review(verdict):
    def produce(pack):
        issues = ([] if verdict == "pass" else [{
            "item": "eval_argv",
            "why": "bounded projection does not prove the evaluation contract",
            "fix_hint": "provide an explicit repository adapter",
        }])
        return {"import-adapter-review.json": {
            "verdict": verdict,
            "round_no": 1,
            "identity_hash": _anchor_value(pack, "identity_hash"),
            "projection_hash": _anchor_value(pack, "projection_hash"),
            "adapter_sha256": _anchor_value(pack, "adapter_sha256"),
            "issues": issues,
        }}
    return produce


class _ScriptedArtifactRunner:
    def __init__(self, scripted):
        self.scripted = list(scripted)
        self.factory_purposes = []
        self.calls = []
        self.current_purpose = None

    def factory(self, _transcripts_dir, purpose_tag):
        self.factory_purposes.append(purpose_tag)
        self.current_purpose = purpose_tag
        return self

    def run_task(self, *, system_prompt, skill, context_pack):
        purpose = self.current_purpose
        self.calls.append({
            "purpose": purpose,
            "system_prompt": system_prompt,
            "skill": skill,
            "pack": context_pack,
        })
        if not self.scripted:
            pytest.fail("unexpected adapter generation runner call")
        item = self.scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        files = item(context_pack) if callable(item) else item
        return Artifact(
            stage=context_pack.stage, files=files, usage=_USAGE,
            transcript_ref=(
                _GENERATOR_TRANSCRIPT
                if purpose == "adapter-generation" else "review-transcript"),
        )


@pytest.fixture()
def service_factory(tmp_path):
    connections = []

    def make(scripted):
        conn = db.connect(tmp_path / f"service-{len(connections)}.sqlite")
        conftest.seed_minimal(conn)
        connections.append(conn)
        daemon = WriteDaemon(conn)
        runner = _ScriptedArtifactRunner(scripted)
        service = AdapterGenerationService(
            runner_factory=runner.factory,
            schemas=_SCHEMAS,
            policy=_NO_BUDGET_POLICY,
            config=_CONFIG,
            system_prompt="[adapter system prompt]",
            generation_skill="[adapter generation skill]",
            review_skill="[adapter review skill]",
            daemon=daemon,
            work_root=str(tmp_path / "work"),
            cost_ledger=CostLedger(daemon, _NO_BUDGET_POLICY),
        )
        return service, runner, daemon

    yield make
    for conn in connections:
        conn.close()


def _service_projection(tmp_path):
    tree, ledger = _tree_and_ledger(tmp_path, {
        "README.md": b"load artifact.bin and run eval.py\n",
        "artifact.bin": b"model",
        "eval.py": b"print('metric_value: score=1.0')\n",
    })
    return _project(tree, ledger)


def _generation_context():
    return {
        "cycle_id": "c1", "external_import_id": 1,
        "question_id": 1, "candidate_id": 1,
    }


def test_service_generates_reviews_records_and_reuses_same_identity(
        tmp_path, service_factory):
    adapter = _adapter()
    service, runner, daemon = service_factory([
        {"import-adapter.json": adapter},
        _review("pass"),
    ])
    projection = _service_projection(tmp_path)

    first = service.generate(
        projection=projection,
        generation_context=_generation_context())

    assert runner.factory_purposes == ["adapter-generation", "adapter-review"]
    assert [call["purpose"] for call in runner.calls] == runner.factory_purposes
    assert _GENERATOR_TRANSCRIPT not in runner.calls[1]["pack"].anchor_md
    assert first["adapter"] == adapter
    assert first["raw"] == _canonical(adapter)
    provenance = first["provenance"]
    assert provenance["adapter_sha256"] == _sha256(_canonical(adapter))
    assert provenance["projection_hash"] == projection["projection_hash"]
    decisions = daemon.query(
        "SELECT id,actor,type,payload_json FROM decision "
        "WHERE type IN ('adapter_generation_candidate','adapter_generation_review') "
        "ORDER BY id")
    assert [(row[1], row[2]) for row in decisions] == [
        ("agent", "adapter_generation_candidate"),
        ("judge", "adapter_generation_review"),
    ]
    assert provenance["generation_decision_id"] == decisions[0][0]
    assert provenance["review_decision_id"] == decisions[1][0]
    assert all(
        json.loads(row[3])["identity_hash"] == provenance["identity_hash"]
        for row in decisions)

    second = service.generate(
        projection=projection,
        generation_context=_generation_context())

    assert second == first
    assert runner.factory_purposes == ["adapter-generation", "adapter-review"]
    assert len(runner.calls) == 2
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type IN "
        "('adapter_generation_candidate','adapter_generation_review')")[0] == 2


def test_service_review_fail_is_candidate_failure(tmp_path, service_factory):
    service, runner, daemon = service_factory([
        {"import-adapter.json": _adapter()},
        _review("fail"),
    ])

    with pytest.raises(
            RepositoryMaterializationError, match="未通过独立复核"):
        service.generate(
            projection=_service_projection(tmp_path),
            generation_context=_generation_context())

    assert runner.factory_purposes == ["adapter-generation", "adapter-review"]
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE actor='agent' "
        "AND type='adapter_generation_candidate'")[0] == 1
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE actor='judge' "
        "AND type='adapter_generation_review'")[0] == 1


def test_service_generation_failure_is_candidate_failure(
        tmp_path, service_factory):
    service, runner, daemon = service_factory([{
        "adapter-generation-failure.json": {
            "reason_code": "ambiguous_entrypoint",
            "details_md": "bounded projection has two plausible entrypoints",
        },
    }])

    with pytest.raises(
            RepositoryMaterializationError, match="无法安全推导"):
        service.generate(
            projection=_service_projection(tmp_path),
            generation_context=_generation_context())

    assert runner.factory_purposes == ["adapter-generation"]
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE actor='agent' "
        "AND type='adapter_generation_candidate'")[0] == 1
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE actor='judge' "
        "AND type='adapter_generation_review'")[0] == 0


def test_service_runner_error_propagates_without_candidate_decision(
        tmp_path, service_factory):
    failure = RunnerError(
        "provider unavailable", usage=_USAGE,
        transcript_ref="failed-generation-transcript",
        failure_kind="provider_unavailable")
    service, runner, daemon = service_factory([failure])

    with pytest.raises(RunnerError) as caught:
        service.generate(
            projection=_service_projection(tmp_path),
            generation_context=_generation_context())

    assert caught.value is failure
    assert runner.factory_purposes == ["adapter-generation"]
    assert daemon.query_one(
        "SELECT count(*) FROM decision WHERE type IN "
        "('adapter_generation_candidate','adapter_generation_review')")[0] == 0
    assert daemon.query_one(
        "SELECT status,failure_kind FROM runner_call "
        "WHERE purpose='adapter_generation'") == (
            "failed", "provider_unavailable")
