import hashlib
import json
import shutil
import time
from pathlib import Path

import pytest

from orchestrator.interfaces import CallUsage, ContextPack
from orchestrator.process_supervisor import ExecutionSupervisor, atomic_write_receipt
from orchestrator.provider_invocation import write_provider_invocation_receipt
from orchestrator.wildidea_adapter import (
    ADAPTER_VERSION,
    PINNED_COMMIT,
    PINNED_ENGINE_VERSION,
    WildIdeaAdapter,
    WildIdeaAdapterError,
)


SYSTEM_ROOT = Path(__file__).resolve().parents[1]
HONEST_STATUS = "联网查重未启用·文献级待验证"


def _policy():
    return {
        "idea": {
            "engine": {
                "name": "wildidea",
                "version": PINNED_ENGINE_VERSION,
                "adapter_version": ADAPTER_VERSION,
                "profile": "research",
            },
            "problem_type": "research",
            "slot_count": 9,
            "candidate_top_k": 3,
            "max_attempts": 3,
            "sd_threshold": 6,
            "thresholds": {
                "research": {
                    "structural_depth": 6,
                    "domain_distance": 7,
                    "applicability": 6,
                    "novelty": 8,
                }
            },
            "novelty_check": {
                "enabled": False, "status": "pending_controlled_backend"},
        }
    }


def _enabled_policy():
    policy = _policy()
    policy["idea"]["dedup_budget"] = 10
    policy["idea"]["novelty_check"] = {
        "enabled": True,
        "status": "controlled_backend_enabled",
        "provider": "arxiv_api_v1",
        "endpoint": "https://export.arxiv.org/api/query",
        "queries_per_candidate": 1,
        "max_results_per_query": 10,
        "timeout_s": 20,
        "max_response_bytes": 4194304,
        "min_interval_s": 3,
    }
    return policy


class _FakeNoveltyProvider:
    name = "arxiv_api_v1"

    def __init__(self):
        self.calls = []

    def search(self, query, *, policy_hash):
        self.calls.append((query, policy_hash))
        result_hash = "sha256:" + hashlib.sha256(query.encode()).hexdigest()
        snapshot_hash = "sha256:" + hashlib.sha256(
            ("snapshot\x00" + query).encode()).hexdigest()
        return {
            "final_ref": {
                "query": query,
                "provider": self.name,
                "snapshot_hash": snapshot_hash,
                "snapshot_ref": (
                    "state/novelty/snapshots/sha256/"
                    + snapshot_hash.removeprefix("sha256:") + ".json"),
                "raw_content_hash": "sha256:" + "a" * 64,
                "result_content_hashes": [result_hash],
                "ranking": [result_hash],
                "policy_hash": policy_hash,
            },
            "results": [{
                "rank": 1,
                "result_content_hash": result_hash,
                "id": "https://arxiv.org/abs/fixture",
                "title": "Frozen nearest-neighbor fixture",
            }],
        }


def _pack():
    pack = ContextPack(
        cycle_id="c17",
        stage="idea",
        target_id=None,
        anchor_md="## 用户研究问题\n如何稳健迁移跨受试者 EEG 表征？",
        neighborhood_md="必要约束：LODO；不得窥视 sealed holdout。",
        retrieval_md="已知参考资料只读摘要。",
        refs=["paper:1"],
        sources=["db:question:17", "db:goal:1:v1"],
    )
    material = "\x00".join((
        pack.anchor_md,
        pack.neighborhood_md,
        pack.retrieval_md,
        json.dumps(pack.refs, ensure_ascii=False),
    ))
    pack.pack_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return pack


def _candidate(candidate_id, *, path="wildidea", core=None):
    candidate = {
        "candidate_id": candidate_id,
        "generation_path": path,
        "audit_mapping": {
            "source_domain": "断裂力学-" + candidate_id,
            "target_domain": "跨受试者 EEG",
            "object_mapping": "裂纹尖端→域偏移边界",
            "shared_relations": "局部应力跨阈值后触发全局失稳",
        },
        "core_claim": core or ("SECRET-CORE-" + candidate_id),
        "mechanism": "按局部域偏移强度触发分层校准",
        "assumptions": ["训练域可估计局部偏移"],
        "min_falsifiable_experiment": "与统一校准对照；LODO 均值不升即失败",
        "novelty_type": "校准",
        "novelty_status": HONEST_STATUS,
    }
    if path == "wildidea":
        candidate["wildidea_extra"] = {
            "source_isof": "输入=载荷；状态=裂纹；输出=扩展；反馈=检测",
            "source_prototype": "D2-15 断裂阈值",
            "deanchor_level": "成立",
            "degenerate_form": "只按均值做静态阈值",
            "nearest_neighbor_diff": "不同于普通域判别器，显式建局部临界关系",
            "strongest_rebuttal": "局部偏移强度未必对应泛化失稳",
        }
    return candidate


def _draft(*, bypass=False):
    if bypass:
        return {
            "need_innovation": False,
            "candidates": [_candidate("b1", path="bypass")],
            "novelty_refs": [],
        }
    return {
        "need_innovation": True,
        "candidates": [_candidate("c2"), _candidate("c0"), _candidate("c1")],
        "novelty_refs": [],
    }


def _score(candidate_id, values, decision="pass"):
    names = (
        "structural_depth", "domain_distance", "applicability", "novelty",
        "unexpectedness", "non_obviousness",
    )
    return {
        "candidate_id": candidate_id,
        "scores": dict(zip(names, values)),
        "decision": decision,
        "rationale": "独立映射评分",
    }


def test_startup_verifies_exact_upstream_identity_and_policy():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    assert adapter.metadata["commit"] == PINNED_COMMIT
    assert adapter.metadata["engine_version"] == PINNED_ENGINE_VERSION
    assert len(adapter.metadata["manifest_sha256"]) == 64

    bad = _policy()
    bad["idea"]["engine"]["version"] = "wildidea@main"
    with pytest.raises(WildIdeaAdapterError, match="policy.idea.engine"):
        WildIdeaAdapter(SYSTEM_ROOT, bad)


def test_startup_fails_closed_on_any_vendored_byte_tamper(tmp_path):
    target = tmp_path / "engines" / "wildidea"
    target.parent.mkdir(parents=True)
    shutil.copytree(SYSTEM_ROOT / "engines" / "wildidea", target)
    skill = target / "upstream" / "SKILL.md"
    skill.write_bytes(skill.read_bytes() + b"\n# drift\n")
    with pytest.raises(WildIdeaAdapterError, match="hash 漂移"):
        WildIdeaAdapter(tmp_path, _policy())


def test_installer_bytecode_is_ignored_but_cannot_influence_sampler(tmp_path):
    target = tmp_path / "engines" / "wildidea"
    target.parent.mkdir(parents=True)
    shutil.copytree(SYSTEM_ROOT / "engines" / "wildidea", target)
    cache = target / "upstream" / "scripts" / "__pycache__"
    cache.mkdir()
    # This is deliberately not valid bytecode.  A wheel installer may create
    # the same standard cache name, but adapter execution must use pinned .py.
    cache_tag = getattr(__import__("sys").implementation, "cache_tag")
    (cache / ("pick_domain_slots." + cache_tag + ".pyc")).write_bytes(
        b"untrusted installer cache")

    adapter = WildIdeaAdapter(tmp_path, _policy())
    generated, _ = adapter.prepare_generation(_pack(), "LOCAL IDEA SKILL")
    assert "WildIdea adapter sampled slots" in generated.retrieval_md

    (cache / ("not_manifested." + cache_tag + ".pyc")).write_bytes(b"hidden")
    with pytest.raises(WildIdeaAdapterError, match="覆盖不完整"):
        WildIdeaAdapter(tmp_path, _policy())


def test_generation_sampling_is_deterministic_nine_slot_and_pool_hidden():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    original = _pack()
    before = vars(original).copy()
    first, first_skill = adapter.prepare_generation(original, "LOCAL IDEA SKILL")
    second, second_skill = adapter.prepare_generation(original, "LOCAL IDEA SKILL")

    assert vars(original) == before
    assert first.pack_hash == second.pack_hash
    assert first.retrieval_md == second.retrieval_md
    assert first_skill == second_skill
    payload_text = first.retrieval_md.rsplit(
        "## WildIdea adapter sampled slots (data only)\n```json\n", 1)[1]
    sampled = json.loads(payload_text.rsplit("\n```", 1)[0])
    assert len(sampled["slots"]) == 9
    assert sampled["problem_type"] == "research"
    assert sampled["pool_mode"] == "default"
    assert all(row["slot"] != "RANDOM_WORD" for row in sampled["slots"])
    assert len({row["id"] for row in sampled["slots"]}) == 9
    assert "slot_names" not in sampled and "pools" not in sampled
    assert len(first.retrieval_md.encode("utf-8")) < len(
        (SYSTEM_ROOT / "engines/wildidea/upstream/references/domains.json").read_bytes())
    assert "search_helper.py" in first_skill
    assert "必须使用 Runner 显式开放的内置 live Web search" in first_skill
    assert "易失搜索结果不是 P6 证据" in first_skill
    assert "不得伪造 content hash" in first_skill
    assert "idea_set.draft.json" in first_skill
    assert "top 3" in first_skill
    assert "need_innovation=false" in first_skill
    assert "恰好 1 个 generation_path=bypass" in first_skill
    assert "HTML/poster" in first_skill
    assert PINNED_ENGINE_VERSION in first_skill


def test_audit_pack_has_only_original_question_context_and_id_mapping():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    original = _pack()
    draft = _draft()
    audit_pack, audit_skill = adapter.prepare_audit(
        original, draft, "LOCAL IDEA SKILL")

    assert original.anchor_md in audit_pack.anchor_md
    assert original.neighborhood_md in audit_pack.neighborhood_md
    assert original.retrieval_md in audit_pack.retrieval_md
    assert all(candidate["candidate_id"] in audit_pack.retrieval_md
               for candidate in draft["candidates"])
    assert all(candidate["audit_mapping"]["source_domain"] in audit_pack.retrieval_md
               for candidate in draft["candidates"])
    assert all(candidate["core_claim"] not in audit_pack.retrieval_md
               for candidate in draft["candidates"])
    assert "wildidea:sample" not in "\n".join(audit_pack.sources)
    assert "生成/修复/重抽" in audit_skill
    assert "idea_audit.json" in audit_skill


def test_draft_and_audit_identity_validation_are_fail_closed():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    draft = _draft()
    assert adapter.validate_draft(draft) == []

    forged = _draft()
    forged["provenance"] = {"engine_version": "model-says-latest"}
    assert any("provenance" in error for error in adapter.validate_draft(forged))

    audit = {
        "audit_scores": [
            _score("c2", (8, 8, 8, 8, 8, 8)),
            _score("c0", (8, 8, 8, 8, 8, 8)),
            _score("invented", (8, 8, 8, 8, 8, 8)),
        ],
        "selected_id": "invented",
    }
    errors = adapter.validate_audit(draft, audit)
    assert any("未覆盖" in error and "c1" in error for error in errors)
    assert any("发明" in error and "invented" in error for error in errors)
    assert any("selected_id" in error for error in errors)


def test_merge_corrects_research_gate_selects_by_six_mean_and_stamps_provenance():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    original = _pack()
    generation_pack, generation_skill = adapter.prepare_generation(
        original, "LOCAL IDEA SKILL")
    draft = _draft()
    audit = {
        "audit_scores": [
            # Model says pass, but NV misses the research gate.
            _score("c2", (10, 10, 10, 7.9, 10, 10), decision="pass"),
            # c0/c1 have the same six-dimensional mean; lexical id wins.
            _score("c1", (8, 8, 8, 8, 8, 8), decision="fail"),
            _score("c0", (8, 8, 8, 8, 8, 8), decision="fail"),
        ],
        "selected_id": "c2",
    }
    final = adapter.merge(
        draft, audit, generation_pack=generation_pack,
        base_skill="LOCAL IDEA SKILL")

    decisions = {row["candidate_id"]: row["decision"]
                 for row in final["audit_scores"]}
    assert decisions == {"c0": "pass", "c1": "pass", "c2": "fail"}
    assert final["selected_id"] == "c0"
    assert final["novelty_refs"] == []
    assert all(candidate["novelty_status"] == HONEST_STATUS
               for candidate in final["candidates"])
    assert final["provenance"]["engine_version"] == PINNED_ENGINE_VERSION
    assert final["provenance"]["adapter_version"] == ADAPTER_VERSION
    assert final["provenance"]["model"] == "gpt-5.6-sol"
    assert final["provenance"]["dependency_lock_hash"] == hashlib.sha256(
        (SYSTEM_ROOT / "engines/wildidea/DEPENDENCY_LOCK.json").read_bytes()
    ).hexdigest()
    # anchor_pack = sampled source-card pack; input_card = original question pack.
    assert final["provenance"]["input_card_hash"] == original.pack_hash
    sampled_text = generation_pack.retrieval_md.rsplit(
        "## WildIdea adapter sampled slots (data only)\n```json\n", 1)[1]
    sampled = json.loads(sampled_text.rsplit("\n```", 1)[0])
    expected_anchor_hash = hashlib.sha256(json.dumps(
        sampled, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode("utf-8")).hexdigest()
    assert final["provenance"]["anchor_pack_hash"] == expected_anchor_hash
    assert final["provenance"]["sampling"]["seed"] is not None
    assert final["provenance"]["sampling"]["temperature"] is None
    assert len(final["provenance"]["anchor_pack_hash"]) == 64
    assert len(final["provenance"]["prompt_hash"]) == 64
    assert final["provenance"]["prompt_hash"] == hashlib.sha256(
        generation_skill.encode("utf-8")).hexdigest()
    assert len(final["provenance"]["judge_prompt_hash"]) == 64
    # Inputs remain untouched.
    assert audit["selected_id"] == "c2"
    assert "provenance" not in draft


def test_bypass_uses_only_common_sd_floor():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    original = _pack()
    generation_pack, _ = adapter.prepare_generation(original, "LOCAL IDEA SKILL")
    draft = _draft(bypass=True)
    audit = {
        "audit_scores": [
            _score("b1", (6, 0, 1, 0, 0, 0), decision="fail"),
        ],
        "selected_id": None,
    }
    final = adapter.merge(
        draft, audit, generation_pack=generation_pack,
        base_skill="LOCAL IDEA SKILL")
    assert final["audit_scores"][0]["decision"] == "pass"
    assert final["selected_id"] == "b1"


def test_final_provenance_binds_exact_accepted_runner_invocations(tmp_path):
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    generation_pack, _ = adapter.prepare_generation(_pack(), "LOCAL IDEA SKILL")
    supervisor = ExecutionSupervisor.standalone(tmp_path / "receipts")

    expected_receipt_hashes = {}
    for role, runner_call_id, phase, purpose, marker in (
            ("generation", 41, "idea", "idea-generate-n1-a2", "a"),
            ("judge", 42, "audit", "idea-audit-n2-a1", "b")):
        prompt_sha256 = "sha256:" + marker * 64
        operation_id = "exec-" + marker * 32
        execution_path = supervisor.receipt_dir / (
            "execution-" + operation_id + ".json")
        execution = supervisor._prepared_receipt(  # noqa: SLF001 - receipt fixture
            operation_id=operation_id, kind="codex-query",
            spec_sha256="sha256:" + "c" * 64, timeout_s=10,
            operation_context={
                "reconcile_protocol": "runner-call-v1",
                "db_owner_kind": "runner_call",
                "db_owner_id": runner_call_id,
                "cycle_id": generation_pack.cycle_id,
                "db_phase": phase,
                "db_purpose": purpose,
            })
        execution.update({
            "state": "terminal", "outcome": "exit", "returncode": 0,
            "started_at_unix": time.time() - 0.1,
            "finished_at_unix": time.time(), "group_drained": True,
            "term_sent": False, "kill_sent": False,
        })
        atomic_write_receipt(execution_path, execution)
        receipt_path = Path(write_provider_invocation_receipt(
            receipt_dir=supervisor.receipt_dir,
            runner_call_id=runner_call_id,
            cycle_id=generation_pack.cycle_id,
            phase=phase, purpose=purpose, provider="codex-cli",
            model="gpt-5.6", effort="medium",
            prompt_sha256=prompt_sha256,
            usage=CallUsage(tokens_known=True),
            usage_source="stderr_tokens_used",
            execution_receipt_ref=str(execution_path)))
        receipt_bytes = receipt_path.read_bytes()
        expected_receipt_hashes[role] = "sha256:" + hashlib.sha256(
            receipt_bytes).hexdigest()
        adapter.bind_accepted_invocation(
            generation_pack, role=role, runner_call_id=runner_call_id,
            prompt_sha256=prompt_sha256,
            provider_receipt_ref=str(receipt_path),
            execution_receipt_ref=str(execution_path))

    draft = _draft()
    audit = {
        "audit_scores": [
            _score(candidate_id, (8, 8, 8, 8, 8, 8))
            for candidate_id in ("c0", "c1", "c2")
        ],
        "selected_id": "c0",
    }
    final = adapter.merge(
        draft, audit, generation_pack=generation_pack,
        base_skill="LOCAL IDEA SKILL")
    provenance = final["provenance"]
    assert provenance["prompt_hash"] == "a" * 64
    assert provenance["judge_prompt_hash"] == "b" * 64
    assert provenance["generation_runner_call_id"] == 41
    assert provenance["judge_runner_call_id"] == 42
    assert provenance["generation_provider_receipt_hash"] == expected_receipt_hashes["generation"]
    assert provenance["judge_provider_receipt_hash"] == expected_receipt_hashes["judge"]


def test_production_merge_requires_both_provider_bindings():
    adapter = WildIdeaAdapter(SYSTEM_ROOT, _policy())
    generation_pack, _ = adapter.prepare_generation(_pack(), "LOCAL IDEA SKILL")
    draft = _draft(bypass=True)
    audit = {
        "audit_scores": [_score("b1", (6, 0, 1, 0, 0, 0))],
        "selected_id": "b1",
    }
    with pytest.raises(WildIdeaAdapterError, match="provider binding"):
        adapter.merge(
            draft, audit, generation_pack=generation_pack,
            base_skill="LOCAL IDEA SKILL", require_invocation_binding=True)


def test_controlled_novelty_freezes_before_blind_audit_and_stamps_final_refs():
    novelty = _FakeNoveltyProvider()
    adapter = WildIdeaAdapter(
        SYSTEM_ROOT, _enabled_policy(), novelty_provider=novelty)
    original = _pack()
    generation_pack, generation_skill = adapter.prepare_generation(
        original, "LOCAL IDEA SKILL")
    draft = _draft()
    for candidate in draft["candidates"]:
        candidate["novelty_queries"] = [
            "EEG domain generalization " + candidate["candidate_id"]]

    audit_pack, audit_skill = adapter.prepare_audit(
        original, draft, "LOCAL IDEA SKILL",
        generation_pack=generation_pack)

    assert len(novelty.calls) == 3
    assert all(call[1].startswith("sha256:") for call in novelty.calls)
    assert "Controlled novelty snapshots" in audit_pack.retrieval_md
    assert "Frozen nearest-neighbor fixture" in audit_pack.retrieval_md
    assert all(candidate["core_claim"] not in audit_pack.retrieval_md
               for candidate in draft["candidates"])
    assert "不得再联网补搜" in audit_skill
    assert "novelty_queries" in generation_skill

    audit = {
        "audit_scores": [
            _score(candidate_id, (8, 8, 8, 8, 8, 8))
            for candidate_id in ("c0", "c1", "c2")
        ],
        "selected_id": "c0",
    }
    final = adapter.merge(
        draft, audit, generation_pack=generation_pack,
        base_skill="LOCAL IDEA SKILL")

    assert len(final["novelty_refs"]) == 3
    assert {row["candidate_id"] for row in final["novelty_refs"]} == {
        "c0", "c1", "c2"}
    assert all(row["provider"] == "arxiv_api_v1"
               for row in final["novelty_refs"])
    assert all(candidate["novelty_status"] ==
               "联网粗查已启用·文献级待人工验证"
               for candidate in final["candidates"])


@pytest.mark.parametrize("bad_query", [
    " leading EEG query",
    "EEG query ",
    "EEG\tquery",
    'EEG "query"',
    "EEG \\query",
    "EEG cafe\u0301 query",
])
def test_controlled_novelty_draft_rejects_queries_provider_would_reject(bad_query):
    adapter = WildIdeaAdapter(
        SYSTEM_ROOT, _enabled_policy(),
        novelty_provider=_FakeNoveltyProvider())
    draft = _draft()
    for candidate in draft["candidates"]:
        candidate["novelty_queries"] = ["valid EEG novelty query"]
    draft["candidates"][0]["novelty_queries"] = [bad_query]

    assert any("有界普通文本" in error
               for error in adapter.validate_draft(draft))
