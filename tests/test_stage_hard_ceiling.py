from __future__ import annotations

from pathlib import Path

from meta_research.composition import build_production_runtime
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillRequest,
    IdeaSkillUnavailable,
)
from meta_research.owners.common import canonical_hash
from meta_research.paths import prepare_data_root
from meta_research.runtime_protection import InhibitorLease, RuntimeProtectionUnavailable
from test_idea_stage_recovery import (
    _ComputeProbe,
    _DraftingProvider,
    _IdeaProvider,
    _confirm_question,
    _runtime,
)
from test_public_bundle_stage import (
    _DeterministicBundleSkill,
    _bundle_runtime,
    _prepare_bundle_request,
)
from test_public_plan_stage import (
    _DeterministicIdeaSkill,
    _DeterministicPlanSkill,
    _confirm_direct_quest,
    _finish_idea_stage,
    _runtime as _plan_runtime,
)
from test_public_advancement_runtime_control import (
    _confirmed_control,
    _execute_control,
)


def _signed_ceiling_evidence(reason: str) -> dict[str, object]:
    return {
        "schema_ref": "meta-research/provider-hard-ceiling/v1",
        "termination_reason": reason,
        "invocation_hash": canonical_hash({"invocation": reason}),
        "prompt_hash": canonical_hash({"prompt": reason}),
        "output_schema_hash": canonical_hash({"schema": reason}),
        "stdout_hash": canonical_hash({"stdout": reason}),
        "result_file_hash": canonical_hash({"result": reason}),
        "supervisor_receipt_hash": canonical_hash({"receipt": reason}),
    }


class _CeilingIdeaProvider(_IdeaProvider):
    def __init__(self) -> None:
        super().__init__()
        self.generate_calls = 0

    def generate_draft(self, request: IdeaSkillRequest):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


class _CeilingPlanProvider(_DeterministicPlanSkill):
    def __init__(self) -> None:
        super().__init__(no_gap=False)
        self.generate_calls = 0

    def generate_draft(self, request):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_output_limit",
            recovery_checkpoint=_signed_ceiling_evidence("output_limit"),
        )


class _CeilingBundleProvider(_DeterministicBundleSkill):
    def __init__(self) -> None:
        self.generate_calls = 0

    def generate_draft(self, request):
        self.generate_calls += 1
        raise IdeaSkillUnavailable(
            "codex_operation_timeout",
            recovery_checkpoint=_signed_ceiling_evidence("timeout"),
        )


class _UnavailableInhibitor:
    kind = "test_unavailable_inhibitor"

    def __init__(self) -> None:
        self.available = True
        self.active: set[str] = set()

    def acquire(self, *, holder_ref: str, reason: str):
        del reason
        if not self.available:
            raise RuntimeProtectionUnavailable("power_inhibitor_acquisition_failed")
        self.active.add(holder_ref)
        return InhibitorLease(
            holder_ref=holder_ref,
            backend=self.kind,
            scope="sleep",
            acquired_at=1.0,
            native_holder_ref="test-native:" + holder_ref,
        )

    def is_confirmed(self, lease) -> bool:
        return lease.holder_ref in self.active

    def query_hold(self, lease) -> str:
        return "confirmed" if lease.holder_ref in self.active else "absent"

    def release(self, lease) -> None:
        self.active.discard(lease.holder_ref)


def test_idea_signed_hard_ceiling_durably_fences_attempt_without_terminal_run(
    tmp_path: Path,
) -> None:
    data_root = prepare_data_root(tmp_path / "idea-hard-ceiling")
    provider = _CeilingIdeaProvider()
    runtime = _runtime(data_root, provider)
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("idea-hard-ceiling-start")

        assert not runtime.idea_stage.process_once()
        current = runtime.idea_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"] == {
            "status": "durable",
            "reason": {"code": "codex_operation_timeout"},
        }
        checkpoint = run["recovery_checkpoint"]
        assert checkpoint["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("timeout")
        )
        assert checkpoint["checkpoint"]["attempt_ref"] == run["attempt_ref"]
        assert checkpoint["checkpoint"]["fence_ref"] == run["fence_ref"]
        assert current["stage_commit"] is None

        managed = runtime.owners.agent_runtime.query_managed_run(run["run_ref"])
        assert managed is not None
        assert managed["status"] == "suspended_fenced"
        assert managed["terminal_reason"] == "codex_operation_timeout"
        assert managed["safe_point"] == checkpoint

        # A later worker pass observes the durable fence and cannot replay the
        # sealed provider operation on the same Attempt.
        assert not runtime.idea_stage.process_once()
        assert provider.generate_calls == 1
        assert runtime.idea_stage.query_current()["stage_commit"] is None
        assert completed["cycle_ref"] == current["stage_run_request"]["cycle_ref"]
    finally:
        runtime.close()


def test_plan_signed_output_ceiling_is_a_durable_nonterminal_blocker(
    tmp_path: Path,
) -> None:
    provider = _CeilingPlanProvider()
    runtime = _plan_runtime(
        tmp_path / "plan-hard-ceiling",
        idea_skill=_DeterministicIdeaSkill(),
        plan_skill=provider,
    )
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        for _step in range(4):
            changed = runtime.plan_stage.process_once()
            if provider.generate_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Plan provider boundary was not reached")

        current = runtime.plan_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["blocker"]["reason"] == {
            "code": "codex_operation_output_limit"
        }
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("output_limit")
        )
        assert current["stage_commit"] is None
        assert not runtime.plan_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_bundle_signed_timeout_fences_current_attempt_without_blind_replay(
    tmp_path: Path,
) -> None:
    provider = _CeilingBundleProvider()
    runtime = _bundle_runtime(
        tmp_path / "bundle-hard-ceiling",
        bundle_skill_provider=provider,
    )
    try:
        _prepare_bundle_request(runtime)
        for _step in range(4):
            changed = runtime.bundle_stage.process_once()
            if provider.generate_calls:
                assert not changed
                break
            assert changed
        else:
            raise AssertionError("Bundle provider boundary was not reached")

        current = runtime.bundle_stage.query_current()
        run = current["run"]
        assert run["status"] == "suspended_fenced"
        assert run["fence_status"] == "revoked"
        assert run["blocker"]["reason"] == {"code": "codex_operation_timeout"}
        assert run["recovery_checkpoint"]["checkpoint"]["provider_exit"] == (
            _signed_ceiling_evidence("timeout")
        )
        assert current["stage_commit"] is None
        assert not runtime.bundle_stage.process_once()
        assert provider.generate_calls == 1
    finally:
        runtime.close()


def test_resume_after_production_ceiling_uses_a_new_physical_operation(
    tmp_path: Path,
) -> None:
    invocation_count = tmp_path / "provider-invocation-count"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "import time\n"
        "if '--version' in sys.argv:\n"
        "    print('codex-stage-ceiling-test 1')\n"
        "    raise SystemExit(0)\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
        f"counter = Path({str(invocation_count)!r})\n"
        "count = int(counter.read_text()) + 1 if counter.exists() else 1\n"
        "counter.write_text(str(count))\n"
        "if count == 1:\n"
        "    time.sleep(2)\n"
        "def value(prefix):\n"
        "    return next(line.split('=', 1)[1] for line in prompt.splitlines() "
        "if line.startswith(prefix))\n"
        "question_ref = value('question_ref=')\n"
        "context_pack_ref = value('context_pack_ref=')\n"
        "outcome = {\n"
        "    'kind': 'IdeaSet',\n"
        "    'question_ref': question_ref,\n"
        "    'context_pack_ref': context_pack_ref,\n"
        "    'candidates': [{\n"
        "        'candidate_key': 'resumed-operation',\n"
        "        'direction': '以跨增强拓扑一致性约束自监督去噪。',\n"
        "        'rationale': '新物理操作可从永久封存的旧 ceiling 后继续。',\n"
        "        'assumptions': ['受控增强不改变稀有形态拓扑。'],\n"
        "        'risks': ['增强可能保留传感器伪影。'],\n"
        "        'evidence_boundary': {\n"
        "            'accepted_evidence_refs': [],\n"
        "            'supported': 'Question 固定了低照度形态保真范围。',\n"
        "            'inferred': '拓扑一致性可能提高稀有结构保真。',\n"
        "            'unknown': '跨设备稳健性仍未知。',\n"
        "        },\n"
        "        'falsification_hint': {\n"
        "            'test': '比较稀有形态召回率与伪影率。',\n"
        "            'would_refute': '召回率未提高或伪影显著增加。',\n"
        "        },\n"
        "        'material_difference': {\n"
        "            'from_history': '不复用旧 ceiling 输出。',\n"
        "            'from_peers': '以拓扑而非像素误差组织机制。',\n"
        "            'plan_commitment_change': 'Plan 比较拓扑干预轴与基线。',\n"
        "        },\n"
        "    }],\n"
        "    'recommendation': None,\n"
        "}\n"
        "args = sys.argv[1:]\n"
        "result_path = Path(args[args.index('--output-last-message') + 1])\n"
        "result_path.write_text(json.dumps({'outcome': outcome}), encoding='utf-8')\n"
        "print(json.dumps({'type': 'thread.started', "
        "'thread_id': 'resumed-primary'}))\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(executable),
        timeout_seconds=0.2,
    )
    drafting = _DraftingProvider()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "production-ceiling-resume"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=adapter,
    )
    try:
        completed = _confirm_question(runtime)
        runtime.idea_stage.start("production-ceiling-start")
        assert not runtime.idea_stage.process_once()
        fenced = runtime.idea_stage.query_current()["run"]
        assert fenced["status"] == "suspended_fenced"
        assert invocation_count.read_text(encoding="utf-8") == "1"

        foreground = runtime.owners.advancement_engine.query_foreground(
            completed["quest_ref"]
        )
        assert foreground is not None
        resume = _confirmed_control(
            runtime.owners.human_collaboration,
            scope_ref=f"quest:{completed['quest_ref']}",
            payload={
                "action": "resume",
                "target": {
                    "quest_ref": completed["quest_ref"],
                    "cycle_ref": completed["cycle_ref"],
                    "question_ref": foreground["question_ref"],
                    "epoch": foreground["epoch"],
                    "target_scope": "run",
                    "run_ref": fenced["run_ref"],
                },
                "reason": "operator_requested",
            },
            key="production-ceiling-resume",
        )
        _execute_control(
            runtime.owners.human_collaboration,
            resume,
            "production-ceiling-resume",
        )
        resumed = runtime.idea_stage.query_current()["run"]
        assert resumed["attempt_ref"] != fenced["attempt_ref"]
        assert resumed["fence_ref"] != fenced["fence_ref"]
        assert resumed["root_session_ref"] == fenced["root_session_ref"]
        assert (
            resumed["provider_operations"]["primary"]["invocation_ref"]
            != fenced["provider_operations"]["primary"]["invocation_ref"]
        )

        assert runtime.idea_stage.process_once()
        advanced = runtime.idea_stage.query_current()["run"]
        assert invocation_count.read_text(encoding="utf-8") == "2"
        assert advanced["primary_draft_checkpoint"]["status"] == "recorded"
        assert advanced["status"] == "admitted"
        operation_roots = sorted(
            path
            for path in (
                tmp_path / "idea-provider" / "provider-operations"
            ).iterdir()
            if path.is_dir()
        )
        assert len(operation_roots) == 2
        receipts = [
            (root / "primary" / "supervisor-exit.json").read_text(
                encoding="utf-8"
            )
            for root in operation_roots
        ]
        assert sum(
            '"termination_reason":"timeout"' in receipt for receipt in receipts
        ) == 1
        assert sum(
            '"termination_reason":"completed"' in receipt for receipt in receipts
        ) == 1
    finally:
        runtime.close()


def test_failed_inhibitor_prevents_runtime_version_probe_and_provider_spawn(
    tmp_path: Path,
) -> None:
    invocation_log = tmp_path / "codex-invocations.log"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(invocation_log)!r}\n"
        "printf 'codex-test 1.0.0\\n'\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    adapter = CodexIdeaSkillAdapter(
        tmp_path / "idea-provider",
        executable=str(executable),
    )
    drafting = _DraftingProvider()
    inhibitor = _UnavailableInhibitor()
    runtime = build_production_runtime(
        prepare_data_root(tmp_path / "pre-hold-probe"),
        proposal_drafter=drafting,
        intent_drafting_provider=drafting,
        host_compute_probe=_ComputeProbe(),
        idea_skill_provider=adapter,
        power_inhibitor=inhibitor,
    )
    try:
        _confirm_question(runtime)
        inhibitor.available = False
        runtime.idea_stage.start("pre-hold-probe-start")
        assert not runtime.idea_stage.process_once()

        assert not invocation_log.exists()
        assert runtime.idea_stage.transient_error == (
            "power_inhibitor_acquisition_failed"
        )
        waiting = runtime.query_runtime_observability()["durable_waiting"]
        assert len(waiting) == 1
        assert waiting[0]["reason"] == {
            "code": "power_inhibitor_acquisition_failed"
        }
    finally:
        runtime.close()
