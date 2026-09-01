from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from meta_research.bundle_contract import (
    MAX_BUNDLE_TARGET_PLAN_BYTES,
    BundleContractError,
    diagnose_legacy_target_plan_v2,
    material_target_plan_hash,
    validate_target_plan,
)
from meta_research.bundle_skill import (
    BUNDLE_PROVIDER_TRANSPORT_LIMITS,
    BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES,
    BundleDispatchRequest,
    BundleExhaustionSkillResult,
    BundleSkillRequest,
    BundleSkillUnavailable,
    BundleTargetBatchRequest,
    CodexBundleSkillAdapter,
    _owner_rejection_prompt,
    _validate_request as _validate_bundle_request,
    validate_bundle_exhaustion_skill_result,
    validate_bundle_skill_result,
)
from meta_research.idea_skill import (
    CodexIdeaSkillAdapter,
    IdeaSkillUnavailable,
    ProviderTransportLimits,
)
from meta_research.bundle_exhaustion import (
    BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
    bundle_exhaustion_route_fingerprint,
)
from meta_research.bundle_protocol import (
    BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES,
    BUNDLE_ROOT_MAX_SERIALIZED_BYTES,
    HeldFixedBinding,
    RouteSpec,
)
from meta_research.harness import FullConformanceBinding, ResidentMcpChannel
from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.owners.agent_runtime import (
    BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
    BUNDLE_INBOX_CHECKPOINT_SCHEMA,
    BundleRuntimeBinding,
)
from meta_research.semantic_mcp import McpConnection, ResidentMcpBinding
from meta_research.semantic_owner_gateway import (
    BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
)
from meta_research.bundle_target_contract import (
    FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
    FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
    MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
    PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
    build_normalized_completion_contract,
    formal_target_candidate_from_dict,
    normalized_completion_contract_from_dict,
    normalized_completion_contract_to_dict,
)
from meta_research.plan_contract import PLAN_DOCUMENT_SCHEMA_REF
from meta_research.plan_skill import CodexPlanSkillAdapter
from meta_research.quest_drafting import (
    PROVIDER_RESULT_MAX_BYTES,
    PROVIDER_STREAM_MAX_BYTES,
    _CancellableProcessRunner,
)


def _inbox_checkpoint(
    *, run_ref: str, attempt_ref: str, fence_ref: str
) -> dict[str, object]:
    payload = {
        "schema_ref": BUNDLE_INBOX_CHECKPOINT_SCHEMA,
        "checkpoint_ref": f"checkpoint:{run_ref}",
        "run_ref": run_ref,
        "attempt_ref": attempt_ref,
        "fence_ref": fence_ref,
        "checkpoint_revision": 1,
        "cursor": 0,
        "generation": 0,
        "batch_hash": "0" * 64,
        "closed": True,
    }
    return {
        **payload,
        "checkpoint_hash": canonical_hash(payload),
        "receipt": {
            "status": "accepted",
            "issuer": "agent_runtime",
            "kind": BUNDLE_INBOX_CHECKPOINT_RECEIPT_KIND,
            "receipt_ref": f"checkpoint-receipt:{run_ref}",
            "subject_ref": f"checkpoint:{run_ref}",
            "payload_hash": "1" * 64,
        },
    }


def _plan_document() -> dict[str, object]:
    return {
        "schema_ref": PLAN_DOCUMENT_SCHEMA_REF,
        "kind": "PlanDocument",
        "question_ref": "question:bundle-1",
        "idea_set_ref": "idea-set:bundle-1",
        "context_pack_ref": "plan-context:bundle-1",
        "answer_contract": {"answer_contract_hash": "a" * 64},
        "evidence_reuse_set": [],
        "coverage": [],
        "gap_set": ["gap:structure"],
        "experiment_briefs": [
            {
                "experiment_key": "experiment:structure",
                "gap_obligation_keys": ["gap:structure"],
                "goal": "比较冻结结构对结果复现的影响。",
                "characteristics": "在冻结输入上比较结构策略。",
                "boundary_constraints": "固定数据、协议和预算。",
                "semantic_delta": "只改变结构冻结策略。",
                "contributing_idea_refs": ["idea:structure"],
            }
        ],
        "idea_trace": [],
        "bundle_disposition": "experiments_required",
        "source_bindings": {"accepted": True},
    }


def test_bundle_successor_request_carries_owner_rejection_feedback() -> None:
    plan = _plan_document()
    question_binding = {"question_ref": "question:bundle-1"}
    formal_plan_binding = {
        "formal_plan_ref": "formal-plan:bundle-1",
        "plan_document": plan,
        "plan_document_hash": canonical_hash(plan),
        "answer_contract_hash": "a" * 64,
    }
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "accepted_question_binding": question_binding,
        "accepted_formal_plan_binding": formal_plan_binding,
    }
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:2",
        fence_ref="bundle-fence:2",
        cycle_ref="cycle:bundle-1",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=canonical_hash(context),
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-1",
        runtime_binding=BundleRuntimeBinding(
            packaged_skill_bundle_hash="1" * 64,
            instruction_set_hash="2" * 64,
            model_ref="test-model",
            harness_adapter_ref="test-harness",
            mcp_bindings=(),
            capability_bindings=(),
            resource_bindings=(),
        ),
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:2",
            fence_ref="bundle-fence:2",
        ),
        native_session_ref="provider-session:bundle-1",
        predecessor_candidate_ref="bundle-submission:rejected",
        owner_rejection_receipt_ref="rg-target-rejection-receipt:1",
        owner_rejection_kind="domain",
        owner_feedback=("Attach accepted Owner proofs to every candidate.",),
    )

    _validate_bundle_request(request)
    prompt = _owner_rejection_prompt(request)
    assert "同一 Root/native Session" in prompt
    assert "bundle-submission:rejected" in prompt
    assert "rg-target-rejection-receipt:1" in prompt
    assert "Attach accepted Owner proofs" in prompt
    completion_request = replace(request, owner_rejection_kind="completion")
    _validate_bundle_request(completion_request)
    assert "structured-completion rejection" in _owner_rejection_prompt(
        completion_request
    )

    with pytest.raises(
        BundleContractError, match="owner_feedback_lineage_incomplete"
    ):
        _validate_bundle_request(
            replace(request, owner_rejection_receipt_ref=None)
        )


def _measurement_contract(label: str, cell: str) -> dict[str, object]:
    parts = [f"part:{label}:fold-2", f"part:{label}:fold-1"]
    return {
        "schema_ref": MEASUREMENT_CONTRACT_CANDIDATE_SCHEMA_REF,
        "experiment_keys": ["experiment:structure"],
        "measurement_unit_key": cell,
        "baseline_forward_contract": {
            "schema_ref": "test/baseline-forward/v1",
            "input_role": "accepted baseline",
            "output_role": "prediction",
        },
        "variant_recipe": {
            "schema_ref": "test/variant-recipe/v1",
            "semantic_delta": "freeze structure",
        },
        "evaluation_protocol_lineage": {
            "schema_ref": "test/protocol-lineage/v1",
            "parent_ref": "protocol-family:structure",
        },
        "protocol_version": {
            "schema_ref": PROTOCOL_VERSION_CANDIDATE_SCHEMA_REF,
            "evaluation_data": {
                "dataset_ref": "dataset:structure",
                "selection": "frozen",
            },
            "split": {"kind": "fixed", "split_ref": "split:structure"},
            "preprocessing": {
                "pipeline_ref": "preprocessing:structure",
                "steps": ["normalize"],
            },
            "required_metrics": [
                {
                    "metric_key": "metric:agreement",
                    "definition": {
                        "formula": "matching / total",
                        "units": "ratio",
                        "direction": "maximize",
                        "value_schema": {"type": "number"},
                    },
                },
                {
                    "metric_key": "metric:conflicts",
                    "definition": {
                        "formula": "conflict count",
                        "units": "count",
                        "direction": "minimize",
                        "value_schema": {"type": "integer"},
                    },
                },
            ],
            "optional_metrics": [],
            "internal_part_keys": parts,
            "aggregation": {
                "rule_ref": "aggregation:structure:mean-v1",
                "rule": {
                    "kind": "arithmetic_mean",
                    "ordered_part_keys": parts,
                },
            },
            "preregistered_stop_rules": [
                {
                    "rule_ref": "stop:structure:fixed-budget-v1",
                    "rule": {"kind": "fixed_budget", "maximum_parts": 2},
                }
            ],
        },
        "checkpoint_policy": "forbidden",
        "result_schema_ref": "test/measurement-result/v1",
        "result_schema": {
            "type": "object",
            "required": ["metric_values"],
            "properties": {
                "metric_values": {
                    "type": "array",
                    "ordered_by": "required_metrics",
                }
            },
        },
    }


def _target_plan(plan: dict[str, object], context_hash: str) -> dict[str, object]:
    completion = build_normalized_completion_contract(
        plan,
        (
            {
                "experiment_key": "experiment:structure",
                "held_fixed_slots": ["shared-model"],
                "required_measurement_unit_keys": [
                    "cell:structure-primary",
                    "cell:structure-replication",
                ],
            },
        ),
    )
    completion_document = normalized_completion_contract_to_dict(completion)
    implementation_hash = canonical_hash({"implementation": "structure"})
    source_version = "source-version:structure"
    candidate = {
        "schema_ref": FORMAL_TARGET_CANDIDATE_SCHEMA_REF,
        "candidate": {
            "local_label": "target:structure",
            "experiment_keys": ["experiment:structure"],
            "measurement_unit_keys": ["cell:structure-primary"],
            "held_fixed_bindings": [
                {
                    "semantic_slot": "shared-model",
                    "implementation_revision_ref": "implementation:shared-model",
                }
            ],
            "implementation_revision_ref": "implementation:structure",
            "code_changed": False,
            "reuse_trace": {
                "tier_decisions": [
                    {
                        "tier": "self-implementation",
                        "disposition": "selected",
                        "reason_ref": "reuse-reason:structure",
                        "source_proofs": [
                            {
                                "source_ref": "source:structure",
                                "exact_version_ref": source_version,
                                "implementation_revision_ref": (
                                    "implementation:structure"
                                ),
                                "eligible_tier": "self-implementation",
                                "verification_receipt": _receipt(
                                    "source-verification:structure",
                                    source_version,
                                ),
                                "implementation_binding": {
                                    "subject_ref": "implementation:structure",
                                    "content_hash_ref": implementation_hash,
                                },
                                "implementation_acceptance_receipt": _receipt(
                                    "implementation-acceptance:structure",
                                    implementation_hash,
                                ),
                                "eligibility_anchor_ref": None,
                                "eligibility_binding": None,
                                "eligibility_receipt": None,
                                "license_ref": None,
                                "content_hash_ref": None,
                                "patch_ref": None,
                            }
                        ],
                    }
                ],
                "greenfield_exception": "simple-implementation",
            },
            "routes": [
                {
                    "route_ref": "route:structure",
                    "known_external_operation_refs": [],
                }
            ],
            "depends_on_labels": [],
            "direct_accepted_input_asset_refs": [],
        },
        "semantic_inputs": [
            deepcopy(completion_document["experiments"][0]["semantic_inputs"])
        ],
        "measurement_contract": _measurement_contract(
            "structure",
            "cell:structure-primary",
        ),
        "risk_class": "normal",
    }
    return {
        "schema_ref": "meta-research/target-plan/v3",
        "kind": "TargetPlan",
        "formal_plan_ref": "formal-plan:bundle-1",
        "context_pack_ref": "context-pack:bundle-1",
        "completion_contract": completion_document,
        "initial_strategy_update": {
            "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
            "revision": 1,
            "candidates": [candidate],
            "requires_accepted_labels": [],
            "strategy_complete": False,
        },
        "source_bindings": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document_hash": canonical_hash(plan),
            "context_pack_ref": "context-pack:bundle-1",
            "context_pack_hash": context_hash,
        },
    }


def _receipt(receipt_ref: str, subject_ref: str) -> dict[str, object]:
    return {
        "receipt_ref": receipt_ref,
        "subject_ref": subject_ref,
        "verified": True,
        "currentness_known": True,
        "current": True,
    }


def _provider_wire_value(
    value: object,
    schema: dict[str, object],
    *,
    encode_domain_documents: bool = True,
) -> object:
    """Encode deterministic fake output through the frozen provider schema."""

    union = schema.get("anyOf")
    if isinstance(union, list):
        matching = [
            branch
            for branch in union
            if isinstance(branch, dict)
            and (
                "const" not in branch
                or branch["const"] == value
            )
            and (
                not isinstance(value, dict)
                or not isinstance(branch.get("required"), list)
                or set(branch["required"]) <= set(value)
            )
        ]
        if matching:
            return _provider_wire_value(
                value,
                matching[0],
                encode_domain_documents=encode_domain_documents,
            )
    if (
        encode_domain_documents
        and isinstance(value, dict)
        and schema.get("type") == "string"
        and "canonical JSON object string"
        in str(schema.get("description", ""))
    ):
        return canonical_json(value)
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            if set(properties) == {"provider_output"} and set(value) != {
                "provider_output"
            }:
                child = properties["provider_output"]
                assert isinstance(child, dict)
                return {
                    "provider_output": _provider_wire_value(
                        value,
                        child,
                        encode_domain_documents=encode_domain_documents,
                    )
                }
            return {
                key: (
                    _provider_wire_value(
                        nested,
                        properties[key],
                        encode_domain_documents=encode_domain_documents,
                    )
                    if key in properties and isinstance(properties[key], dict)
                    else nested
                )
                for key, nested in value.items()
            }
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return [
            _provider_wire_value(
                nested,
                schema["items"],
                encode_domain_documents=encode_domain_documents,
            )
            for nested in value
        ]
    return value


class _SequenceRunner:
    def __init__(
        self,
        outputs: list[dict[str, object]],
        *,
        emit_review_trace: bool = True,
        emit_primary_review_trace: bool = False,
        provider_wire_mode: str = "strict",
    ) -> None:
        self._outputs = iter(outputs)
        self._emit_review_trace = emit_review_trace
        self._emit_primary_review_trace = emit_primary_review_trace
        self._provider_wire_mode = provider_wire_mode
        self.calls: list[tuple[list[str], str, dict[str, object]]] = []
        self.environments: list[dict[str, str] | None] = []

    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        schema_path = Path(argv[argv.index("--output-schema") + 1])
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        output_path = Path(argv[argv.index("--output-last-message") + 1])
        output = next(self._outputs)
        if self._provider_wire_mode != "raw":
            output = _provider_wire_value(
                output,
                schema,
                encode_domain_documents=self._provider_wire_mode == "strict",
            )
        output_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
        self.calls.append((argv, prompt, schema))
        self.environments.append(environment)
        thread_id = "codex-bundle-primary:1"
        events: list[dict[str, object]] = [
            {"type": "thread.started", "thread_id": thread_id}
        ]
        if self._emit_primary_review_trace and (
            "target_plan" in schema.get("properties", {})
            or "exhaustion_assessment" in schema.get("properties", {})
        ):
            reviewer = "codex-bundle-primary-reviewer:1"
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
                events.append(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": f"collab-primary-{tool}:1",
                            "type": "collab_tool_call",
                            "tool": tool,
                            "sender_thread_id": thread_id,
                            "receiver_thread_ids": [reviewer],
                            "agents_states": {reviewer: {"status": status}},
                            "status": "completed",
                        },
                    }
                )
        if (
            self._emit_review_trace
            and "reviewer_agent_ref" in schema.get("properties", {})
        ):
            reviewer = output["reviewer_agent_ref"]
            child_task = next(
                (
                    json.loads(line.removeprefix("child_task="))
                    for line in prompt.splitlines()
                    if line.startswith("child_task=")
                ),
                None,
            )
            for tool, status in (
                ("spawn_agent", "pending_init"),
                ("wait", "completed"),
            ):
                state: dict[str, object] = {"status": status}
                if tool == "wait" and child_task is not None:
                    state["message"] = "The exact assessment is accepted."
                item: dict[str, object] = {
                    "id": f"collab-{tool}:1",
                    "type": "collab_tool_call",
                    "tool": tool,
                    "sender_thread_id": thread_id,
                    "receiver_thread_ids": [reviewer],
                    "agents_states": {reviewer: state},
                    "status": "completed",
                }
                if tool == "spawn_agent" and child_task is not None:
                    item["prompt"] = child_task
                events.append(
                    {
                        "type": "item.completed",
                        "item": item,
                    }
                )
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        )

    def run_job(
        self,
        job_ref: str,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del job_ref
        return self(argv, prompt, timeout, environment)


class _LargeSequenceRunner(_SequenceRunner):
    def __call__(
        self,
        argv: list[str],
        prompt: str,
        timeout: float,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(argv, prompt, timeout, environment)
        return subprocess.CompletedProcess(
            completed.args,
            completed.returncode,
            stdout=completed.stdout
            + "\n"
            + "x" * (16 * 1024 * 1024 + 4096),
            stderr=completed.stderr,
        )


class _DurableLimitProbe(_CancellableProcessRunner):
    def __init__(self) -> None:
        super().__init__()
        self.stdout_max_bytes: int | None = None

    def run_durable_job(
        self,
        job_ref: str,
        argv: list[str],
        input_text: str,
        timeout: float,
        stdout_path: Path,
        pid_path: Path,
        supervisor_request_path: Path,
        environment: dict[str, str] | None = None,
        stdout_max_bytes: int = PROVIDER_STREAM_MAX_BYTES,
    ) -> subprocess.CompletedProcess[str]:
        del (
            job_ref,
            argv,
            input_text,
            timeout,
            stdout_path,
            pid_path,
            supervisor_request_path,
            environment,
        )
        self.stdout_max_bytes = stdout_max_bytes
        raise OSError("limit probe stops before provider launch")


class _FullConformanceAuthority:
    def __init__(self) -> None:
        self.binding = FullConformanceBinding(
            contract_ref="meta-research/harness-full-conformance/v1",
            contract_hash="1" * 64,
            conformance_ref="hfc_bundle_adapter",
            semantic_mcp_catalog_hash="2" * 64,
            semantic_mcp_operation_bindings_hash=canonical_hash(
                [
                    {"semantic_operation_id": operation_id}
                    for operation_id in BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
                ]
            ),
            required_families=("codex", "claude"),
            required_capabilities=("semantic_mcp",),
            required_operation_ids=BUNDLE_ROOT_SEMANTIC_OPERATION_IDS,
            profile_receipts=("harness-profile:codex", "harness-profile:claude"),
        )
        self.issued: list[dict[str, object]] = []
        self.revoked: list[ResidentMcpChannel] = []

    def require_full_conformance_binding(self) -> FullConformanceBinding:
        return self.binding

    def require_operation_binding(
        self,
        *,
        harness_family: str,
        required_operation_ids: tuple[str, ...],
        required_capabilities: tuple[str, ...],
    ) -> FullConformanceBinding:
        assert harness_family == "codex"
        assert required_operation_ids == BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        assert required_capabilities == ("semantic_mcp",)
        return FullConformanceBinding(
            contract_ref="meta-research/harness-operation-binding/v1",
            contract_hash="6" * 64,
            conformance_ref="operation_binding_" + "6" * 48,
            semantic_mcp_catalog_hash=self.binding.semantic_mcp_catalog_hash,
            semantic_mcp_operation_bindings_hash=(
                self.binding.semantic_mcp_operation_bindings_hash
            ),
            required_families=("codex",),
            required_capabilities=required_capabilities,
            required_operation_ids=required_operation_ids,
            profile_receipts=(),
        )

    def issue_resident_mcp_channel(
        self,
        *,
        root_kind: str,
        phase: str,
        subject_policy: str,
        run_ref: str,
        attempt_ref: str,
        root_session_ref: str,
        fence_ref: str,
        capability_binding_hash: str,
        operation_ids: tuple[str, ...],
    ) -> ResidentMcpChannel:
        assert root_kind == "bundle"
        assert subject_policy == "operation_tree"
        request = {
            "root_kind": root_kind,
            "phase": phase,
            "subject_policy": subject_policy,
            "run_ref": run_ref,
            "attempt_ref": attempt_ref,
            "root_session_ref": root_session_ref,
            "fence_ref": fence_ref,
            "capability_binding_hash": capability_binding_hash,
            "operation_ids": operation_ids,
        }
        self.issued.append(request)
        generation = len(self.issued)
        operation_bindings = tuple(
            {"semantic_operation_id": operation_id}
            for operation_id in operation_ids
        )
        return ResidentMcpChannel(
            connection=McpConnection(
                token=f"resident-secret-{generation}",
                grant_ref=f"mcp-grant:{generation}",
            ),
            binding=ResidentMcpBinding(
                server_instance_ref="semantic-mcp:test",
                endpoint_ref="/mcp",
                catalog_revision=1,
                catalog_hash=self.binding.semantic_mcp_catalog_hash,
                health_receipt_ref="mcp-health:test",
                connection_grant_ref=f"mcp-grant:{generation}",
                operation_bindings=operation_bindings,
            ),
        )

    def revoke_resident_mcp_channel(self, channel: ResidentMcpChannel) -> None:
        self.revoked.append(channel)


def _fake_codex(path: Path) -> Path:
    path.write_text("#!/bin/sh\nprintf 'codex-bundle-test 1\\n'\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_completion_rejection_feedback_reaches_bundle_primary_prompt(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan,
            "plan_document_hash": canonical_hash(plan),
            "answer_contract_hash": "a" * 64,
        },
    }
    context_hash = canonical_hash(context)
    runner = _SequenceRunner(
        [{"target_plan": _target_plan(plan, context_hash)}]
    )
    adapter = CodexBundleSkillAdapter(
        tmp_path / "bundle-successor-provider",
        executable=str(_fake_codex(tmp_path / "codex-successor")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://semantic-mcp.invalid")
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:2",
        fence_ref="bundle-fence:2",
        cycle_ref="cycle:bundle-1",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-1",
        runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:2",
            fence_ref="bundle-fence:2",
        ),
        native_session_ref="codex-bundle-primary:1",
        predecessor_candidate_ref="bundle-candidate:rejected",
        owner_rejection_receipt_ref="receipt:bundle-completion-rejected",
        owner_rejection_kind="completion",
        owner_feedback=("修正 TargetPlan 的 reference closure。",),
    )

    adapter.execute(request)

    assert len(runner.calls) == 1
    prompt = runner.calls[0][1]
    assert "owner_rejection_kind=completion" in prompt
    assert "bundle-candidate:rejected" in prompt
    assert "receipt:bundle-completion-rejected" in prompt
    assert "修正 TargetPlan 的 reference closure" in prompt


def test_production_adapter_freezes_skill_without_requiring_child_choreography(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    question_binding = {"question_ref": "question:bundle-1"}
    formal_plan_binding = {
        "formal_plan_ref": "formal-plan:bundle-1",
        "plan_document": plan,
        "plan_document_hash": canonical_hash(plan),
        "answer_contract_hash": "a" * 64,
    }
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "accepted_question_binding": question_binding,
        "accepted_formal_plan_binding": formal_plan_binding,
    }
    context_hash = canonical_hash(context)
    target_plan = _target_plan(plan, context_hash)
    runner = _SequenceRunner(
        [
            {"target_plan": target_plan},
            {
                "action": "dispatch",
                "selected_target_ref": "target:accepted-structure",
                "rationale": "This frontier item closes the frozen gap.",
            },
            {
                "strategy_update": {
                    "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": 2,
                    "candidates": [
                        {
                            **deepcopy(
                                target_plan["initial_strategy_update"][
                                    "candidates"
                                ][0]
                            ),
                            "candidate": {
                                **deepcopy(
                                    target_plan["initial_strategy_update"][
                                        "candidates"
                                    ][0]["candidate"]
                                ),
                                "local_label": "target:structure-replication",
                                "measurement_unit_keys": [
                                    "cell:structure-replication"
                                ],
                                "implementation_revision_ref": (
                                    "implementation:structure-replication"
                                ),
                                "reuse_trace": {
                                    "tier_decisions": [
                                        {
                                            "tier": "self-implementation",
                                            "disposition": "selected",
                                            "reason_ref": (
                                                "reuse-reason:structure-replication"
                                            ),
                                            "source_proofs": [
                                                {
                                                    **deepcopy(
                                                        target_plan[
                                                            "initial_strategy_update"
                                                        ]["candidates"][0][
                                                            "candidate"
                                                        ]["reuse_trace"][
                                                            "tier_decisions"
                                                        ][0]["source_proofs"][0]
                                                    ),
                                                    "source_ref": (
                                                        "source:structure-replication"
                                                    ),
                                                    "exact_version_ref": (
                                                        "source-version:structure-replication"
                                                    ),
                                                    "implementation_revision_ref": (
                                                        "implementation:structure-replication"
                                                    ),
                                                    "verification_receipt": _receipt(
                                                        "source-verification:structure-replication",
                                                        "source-version:structure-replication",
                                                    ),
                                                    "implementation_binding": {
                                                        "subject_ref": (
                                                            "implementation:structure-replication"
                                                        ),
                                                        "content_hash_ref": canonical_hash(
                                                            {
                                                                "implementation": (
                                                                    "structure-replication"
                                                                )
                                                            }
                                                        ),
                                                    },
                                                    "implementation_acceptance_receipt": _receipt(
                                                        "implementation-acceptance:structure-replication",
                                                        canonical_hash(
                                                            {
                                                                "implementation": (
                                                                    "structure-replication"
                                                                )
                                                            }
                                                        ),
                                                    ),
                                                }
                                            ],
                                        }
                                    ],
                                    "greenfield_exception": "simple-implementation",
                                },
                                "routes": [
                                    {
                                        "route_ref": "route:structure-replication",
                                        "known_external_operation_refs": [],
                                    }
                                ],
                            },
                            "measurement_contract": _measurement_contract(
                                "structure-replication",
                                "cell:structure-replication",
                            ),
                        }
                    ],
                    "requires_accepted_labels": [],
                    "strategy_complete": True,
                },
                "rationale": "The frozen gap now has a formal TargetCommit.",
            },
        ]
    )
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=runner,
    )
    authority = _FullConformanceAuthority()
    adapter.bind_full_conformance_authority(authority)
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    binding = adapter.runtime_binding()
    assert (
        "provider-output-limits:"
        f"prompt={BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes};"
        f"stream={BUNDLE_PROVIDER_TRANSPORT_LIMITS.stream_max_bytes};"
        f"result={BUNDLE_PROVIDER_TRANSPORT_LIMITS.result_max_bytes}"
    ) in binding.resource_bindings
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1",
        cycle_ref="cycle:bundle-1",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-1",
        runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1",
            fence_ref="bundle-fence:1",
        ),
    )

    result = adapter.execute(request)
    validate_bundle_skill_result(request, result)
    dispatch_request = BundleDispatchRequest(
        stage_request_ref=request.stage_request_ref,
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1",
        graph_ref="target-graph:1",
        generation=1,
        frontier=(
            {
                "target_ref": "target:accepted-structure",
                "target_key": "target:structure",
            },
        ),
        state={
            "schema_ref": "meta-research/bundle-dispatch-state/v1",
            "target_commit_refs": [],
            "running_targets": [],
            "blocked_targets": [],
        },
        root_session_ref=request.root_session_ref,
        native_session_ref=result.primary_session_ref,
        runtime_binding=binding,
        inbox_checkpoint=request.inbox_checkpoint,
    )
    dispatch = adapter.schedule_target(dispatch_request)
    batch_request = BundleTargetBatchRequest(
            stage_request_ref=request.stage_request_ref,
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1",
            fence_ref="bundle-fence:1",
            graph_ref="target-graph:1",
            formal_plan_ref=request.formal_plan_ref,
            context_pack_ref=request.context_pack_ref,
            context_pack_hash=request.context_pack_hash,
            plan_document=plan,
            initial_target_plan=target_plan,
            base_generation=0,
            base_head_receipt={
                "receipt_ref": "target-graph-head-receipt:1",
                "receipt_kind": "target_graph_head_acceptance",
                "owner": "research_graph",
                "subject_ref": "target-graph:1",
                "payload_hash": "b" * 64,
                "bindings": {},
            },
            current_targets=(
                {
                    "target_ref": "target:accepted-structure",
                    "target_key": "target:structure",
                    "spec_hash": canonical_hash(
                        target_plan["initial_strategy_update"]["candidates"][0]
                    ),
                    "spec": target_plan["initial_strategy_update"][
                        "candidates"
                    ][0],
                    "dependency_refs": [],
                    "receipt": {},
                },
            ),
            target_commits=(
                {
                    "commit_ref": "target-commit:1",
                    "target_ref": "target:accepted-structure",
                    "closure_hash": "c" * 64,
                },
            ),
            root_session_ref=request.root_session_ref,
            native_session_ref=result.primary_session_ref,
            runtime_binding=binding,
            inbox_checkpoint=request.inbox_checkpoint,
        )
    batch = adapter.propose_target_batch(batch_request)

    assert result.primary_session_ref == "codex-bundle-primary:1"
    assert result.review_mode == "advisory_unobserved"
    assert result.reviewer_agent_ref is None
    assert binding.model_ref == "gpt-5.6-sol"
    assert (
        "codex-config:model_reasoning_effort=max"
        in binding.resource_bindings
    )
    assert any(
        "meta_research.skills.bundle_stage/SKILL.md" in item
        for item in binding.resource_bindings
    )
    assert any(
        item.startswith(
            "adapter-source:meta_research.idea_skill@sha256:"
        )
        for item in binding.resource_bindings
    )
    primary_argv, primary_prompt, primary_schema = runner.calls[0]
    dispatch_argv, dispatch_prompt, dispatch_schema = runner.calls[1]
    batch_argv, batch_prompt, batch_schema = runner.calls[2]
    serialized_checkpoint = canonical_json(request.inbox_checkpoint)
    assert primary_argv[:2] == [str(tmp_path / "codex"), "exec"]
    assert 'model_reasoning_effort="max"' in primary_argv
    assert f"inbox_checkpoint={serialized_checkpoint}" in primary_prompt
    assert "Agent Session 绝不是 Target 或 TargetRun" in primary_prompt
    assert "本回合仅执行 Primary draft phase" in primary_prompt
    assert "TargetPlan 不选择 provider、adapter" in primary_prompt
    assert "installed_experiment_provider_catalog=" not in primary_prompt
    assert "Provider transport envelope" in primary_prompt
    assert "Provider transport encoding" in primary_prompt
    assert set(primary_schema) == {
        "type",
        "additionalProperties",
        "properties",
        "required",
    }
    assert primary_schema["required"] == ["provider_output"]
    provider_output_schema = primary_schema["properties"]["provider_output"]
    assert len(provider_output_schema["anyOf"]) == 2
    target_plan_branch = next(
        branch
        for branch in provider_output_schema["anyOf"]
        if "target_plan" in branch["properties"]
    )
    exhaustion_branch = next(
        branch
        for branch in provider_output_schema["anyOf"]
        if "exhaustion_assessment" in branch["properties"]
    )
    assert target_plan_branch["additionalProperties"] is False
    assert target_plan_branch["required"] == ["target_plan"]
    assert exhaustion_branch["additionalProperties"] is False
    assert exhaustion_branch["required"] == ["exhaustion_assessment"]
    assert "target_plan" not in exhaustion_branch["properties"]
    assert "exhaustion_assessment" not in target_plan_branch["properties"]
    target_plan_schema = target_plan_branch["properties"]["target_plan"]
    assert "targets" not in target_plan_schema["properties"]
    assert "strategy_complete" not in target_plan_schema["properties"]
    target_schema = target_plan_schema["properties"]["initial_strategy_update"][
        "properties"
    ]["candidates"]["items"]
    assert "target_type" not in target_schema["properties"]
    assert "risk_class" in target_schema["required"]
    assert "candidate" in target_schema["properties"]
    assert "measurement_contract" in target_schema["required"]
    measurement_schema = target_schema["properties"]["measurement_contract"]
    assert measurement_schema["additionalProperties"] is False
    protocol_schema = measurement_schema["properties"]["protocol_version"]
    assert protocol_schema["additionalProperties"] is False
    assert protocol_schema["required"] == [
        "schema_ref",
        "evaluation_data",
        "split",
        "preprocessing",
        "required_metrics",
        "optional_metrics",
        "internal_part_keys",
        "aggregation",
        "preregistered_stop_rules",
    ]
    assert protocol_schema["properties"]["required_metrics"]["items"][
        "additionalProperties"
    ] is False
    candidate_core = target_schema["properties"]["candidate"]["properties"]
    assert not {
        "title",
        "hypothesis",
        "variant_parameter",
        "sample_count",
        "target_type",
    } & set(candidate_core)
    assert "execution" not in target_schema["properties"]
    assert "semantic_inputs" in target_schema["properties"]
    assert "ordered Metric definitions" in primary_prompt
    assert dispatch.selected_target_ref == "target:accepted-structure"
    assert dispatch.native_session_ref == result.primary_session_ref
    assert dispatch_argv[-3:] == ["resume", "codex-bundle-primary:1", "-"]
    assert f"inbox_checkpoint={serialized_checkpoint}" in dispatch_prompt
    assert "durable frontier" in dispatch_prompt
    selected_target_schema = dispatch_schema["properties"][
        "selected_target_ref"
    ]["anyOf"]
    assert selected_target_schema == [
        {"type": "string", "minLength": 1},
        {"type": "null"},
    ]
    assert batch.strategy_update["strategy_complete"] is True
    assert len(batch.strategy_update["candidates"]) == 1
    assert batch.native_session_ref == result.primary_session_ref
    assert batch_argv[-3:] == ["resume", "codex-bundle-primary:1", "-"]
    assert canonical_json(
        {"inbox_checkpoint": request.inbox_checkpoint}
    )[1:-1] in batch_prompt
    assert "append-only Target" in batch_prompt
    assert "measurement_contract" in batch_prompt
    assert "installed_experiment_provider_catalog" not in batch_prompt
    assert "targets" not in batch_schema["properties"]
    assert batch_schema["properties"]["strategy_update"]["properties"][
        "candidates"
    ]["minItems"] == 0
    batch_candidate_schema = batch_schema["properties"]["strategy_update"][
        "properties"
    ]["candidates"]["items"]
    assert "measurement_contract" in batch_candidate_schema["required"]
    oversized_target = {
        **batch_request.current_targets[0],
        "prompt_padding": "x" * BUNDLE_TARGET_BATCH_PROMPT_MAX_BYTES,
    }
    call_count_before_oversized_prompt = len(runner.calls)
    with pytest.raises(
        BundleSkillUnavailable, match="bundle_target_batch_prompt_too_large"
    ):
        adapter.propose_target_batch(
            replace(batch_request, current_targets=(oversized_target,))
        )
    assert len(runner.calls) == call_count_before_oversized_prompt
    forged_checkpoint = {**request.inbox_checkpoint, "closed": False}
    with pytest.raises(
        BundleContractError, match="bundle_inbox_checkpoint_invalid"
    ):
        adapter.schedule_target(
            replace(dispatch_request, inbox_checkpoint=forged_checkpoint)
        )
    with pytest.raises(
        BundleContractError, match="bundle_inbox_checkpoint_invalid"
    ):
        adapter.propose_target_batch(
            replace(batch_request, inbox_checkpoint=forged_checkpoint)
        )
    assert len(runner.calls) == 3
    assert len(authority.issued) == 3
    assert len(authority.revoked) == 3
    assert all(
        issued["operation_ids"] == BUNDLE_ROOT_SEMANTIC_OPERATION_IDS
        for issued in authority.issued
    )
    assert all(
        environment is not None
        and set(environment)
        == {"META_RESEARCH_MCP_TOKEN", "NO_PROXY", "no_proxy"}
        and environment["META_RESEARCH_MCP_TOKEN"].startswith(
            "resident-secret-"
        )
        and environment["NO_PROXY"] == environment["no_proxy"]
        and {"127.0.0.1", "localhost", "::1"}
        <= set(environment["NO_PROXY"].split(","))
        for environment in runner.environments
    )
    for (argv, prompt, _schema), environment in zip(
        runner.calls, runner.environments, strict=True
    ):
        assert environment is not None
        token = environment["META_RESEARCH_MCP_TOKEN"]
        assert token not in prompt
        assert all(token not in argument for argument in argv)
        assert "mcp_servers.meta_research.required=true" in argv
        assert (
            'mcp_servers.meta_research.default_tools_approval_mode="approve"'
            in argv
        )


def test_production_adapter_fails_before_provider_without_resident_mcp(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([])
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-1",
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan,
            "plan_document_hash": canonical_hash(plan),
            "answer_contract_hash": "a" * 64,
        },
    }
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-1",
        run_ref="bundle-run:1",
        attempt_ref="bundle-attempt:1",
        fence_ref="bundle-fence:1",
        cycle_ref="cycle:bundle-1",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=canonical_hash(context),
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-1",
        runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:1",
            attempt_ref="bundle-attempt:1",
            fence_ref="bundle-fence:1",
        ),
    )

    with pytest.raises(
        BundleContractError, match="bundle_inbox_checkpoint_invalid"
    ):
        adapter.generate_draft(
            replace(
                request,
                inbox_checkpoint={**request.inbox_checkpoint, "closed": False},
            )
        )
    assert runner.calls == []

    forged_rejection = {
        "attempt_ref": "bundle-attempt:rejected",
        "submission_ref": "bundle-submission:rejected",
        "submission_content_hash": "1" * 64,
        "execution_receipt": {
            "status": "accepted",
            "issuer": "agent_runtime",
            "kind": "bundle_attempt_executed",
            "receipt_ref": "ar-receipt:rejected",
            "subject_ref": "bundle-attempt:rejected",
            "payload_hash": "2" * 64,
        },
        "rejection_receipt": {
            "status": "accepted",
            "issuer": "caller",
            "kind": "bundle_submission_rejected",
            "receipt_ref": "fake-receipt:rejected",
            "subject_ref": "bundle-submission:rejected",
            "payload_hash": "3" * 64,
        },
    }
    with pytest.raises(
        BundleContractError,
        match="bundle_predecessor_rejections_invalid",
    ):
        adapter.generate_draft(
            replace(
                request,
                predecessor_rejections=(forged_rejection,),
            )
        )
    assert runner.calls == []

    with pytest.raises(BundleSkillUnavailable, match="bundle_semantic_mcp_unavailable"):
        adapter.generate_draft(request)
    assert runner.calls == []


def test_production_exhaustion_acceptance_does_not_require_child_trace(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-exhaustion",
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan,
            "plan_document_hash": canonical_hash(plan),
            "answer_contract_hash": "a" * 64,
        },
    }
    target_plan = _target_plan(plan, canonical_hash(context))
    held_fixed = (
        HeldFixedBinding(
            semantic_slot="shared-model",
            implementation_revision_ref="implementation:shared-model",
        ),
    )

    def exploration_record(
        *, cell: str, suffix: str, outcome: str
    ) -> dict[str, object]:
        route = RouteSpec(
            route_ref=f"route:semantic-{suffix}",
            known_external_operation_refs=(),
        )
        return {
            "record_ref": f"exploration:{suffix}",
            "experiment_key": "experiment:structure",
            "measurement_unit_key": cell,
            "held_fixed_bindings": [
                {
                    "semantic_slot": "shared-model",
                    "implementation_revision_ref": "implementation:shared-model",
                }
            ],
            "route": {
                "route_ref": route.route_ref,
                "known_external_operation_refs": [],
            },
            "route_disposition": {
                "disposition_ref": f"route-disposition:{suffix}",
                "route_ref": route.route_ref,
                "experiment_keys": ["experiment:structure"],
                "outcome": outcome,
                "required_changes": [],
                "evidence_refs": [f"evidence:{suffix}"],
                "external_reconciliations": [],
            },
            "frozen_semantic_fingerprint": bundle_exhaustion_route_fingerprint(
                formal_plan_content_hash=canonical_hash(plan),
                experiment_key="experiment:structure",
                measurement_unit_key=cell,
                held_fixed_bindings=held_fixed,
                route=route,
            ),
        }

    assessment = {
        "exhaustion_assessment": {
            "schema_ref": BUNDLE_EXHAUSTION_ASSESSMENT_SCHEMA,
            "completion_contract": target_plan["completion_contract"],
            "exploration_records": [
                exploration_record(
                    cell="cell:structure-primary",
                    suffix="primary",
                    outcome="semantically_ineligible",
                ),
                exploration_record(
                    cell="cell:structure-replication",
                    suffix="replication",
                    outcome="duplicate_frozen_semantics",
                ),
            ],
        }
    }
    def configured_adapter(runner: _SequenceRunner, name: str):
        adapter = CodexBundleSkillAdapter(
            tmp_path / name,
            executable=str(_fake_codex(tmp_path / f"codex-{name}")),
            model_ref="test-model",
            process_runner=runner,
        )
        adapter.bind_full_conformance_authority(_FullConformanceAuthority())
        adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
        return adapter

    runner = _SequenceRunner([assessment])
    adapter = configured_adapter(runner, "exhaustion-provider")
    binding = adapter.runtime_binding()
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-exhaustion",
        run_ref="bundle-run:exhaustion",
        attempt_ref="bundle-attempt:exhaustion",
        fence_ref="bundle-fence:exhaustion",
        cycle_ref="cycle:bundle-exhaustion",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-exhaustion",
        context_pack_hash=canonical_hash(context),
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-exhaustion",
        runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:exhaustion",
            attempt_ref="bundle-attempt:exhaustion",
            fence_ref="bundle-fence:exhaustion",
        ),
    )
    result = adapter.execute(request)
    assert isinstance(result, BundleExhaustionSkillResult)
    validate_bundle_exhaustion_skill_result(request, result)
    assert result.review_mode == "advisory_unobserved"
    assert result.reviewer_agent_ref is None
    assert result.review_trace is None
    assert len(runner.calls) == 1

    missing_trace_runner = _SequenceRunner(
        [assessment], emit_review_trace=False
    )
    missing_trace_adapter = configured_adapter(
        missing_trace_runner, "missing-exhaustion-trace"
    )
    missing_binding = missing_trace_adapter.runtime_binding()
    missing_request = replace(
        request,
        runtime_binding=missing_binding,
        job_ref="bundle-exhaustion-missing-trace-job",
    )
    missing_result = missing_trace_adapter.execute(missing_request)
    assert isinstance(missing_result, BundleExhaustionSkillResult)
    assert missing_result.review_trace is None
    assert len(missing_trace_runner.calls) == 1

    no_replay = _SequenceRunner([])
    restarted = configured_adapter(no_replay, "missing-exhaustion-trace")
    replay = restarted.execute(
        replace(missing_request, runtime_binding=restarted.runtime_binding())
    )
    assert replay == missing_result
    assert no_replay.calls == []


def test_production_adapter_does_not_bind_an_experiment_provider_catalog(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([])
    adapter = CodexBundleSkillAdapter(
        tmp_path / "provider",
        executable=str(_fake_codex(tmp_path / "codex")),
        model_ref="test-model",
        process_runner=runner,
    )

    binding = adapter.runtime_binding()
    assert "experiment-provider-capability-catalog-v1" not in (
        binding.capability_bindings
    )
    assert not any(
        "experiment-provider-capability-catalog" in item
        for item in binding.resource_bindings
    )
    assert runner.calls == []


def test_target_plan_has_an_explicit_aggregate_byte_limit() -> None:
    assert MAX_BUNDLE_TARGET_PLAN_BYTES == BUNDLE_ROOT_MAX_SERIALIZED_BYTES
    plan = _plan_document()
    context_hash = "9" * 64
    target_plan = _target_plan(plan, context_hash)
    target = target_plan["initial_strategy_update"]["candidates"][0]
    assert isinstance(target, dict)
    target["candidate"]["routes"][0]["route_ref"] = (
        "x" * MAX_BUNDLE_TARGET_PLAN_BYTES
    )
    with pytest.raises(BundleContractError, match="target_plan_too_large"):
        validate_target_plan(
            target_plan,
            formal_plan_ref="formal-plan:bundle-1",
            context_pack_ref="context-pack:bundle-1",
            context_pack_hash=context_hash,
            plan_document=plan,
        )


def test_complete_measurement_contract_is_not_limited_to_the_old_192k_slice() -> None:
    plan = _plan_document()
    context_hash = "5" * 64
    target_plan = _target_plan(plan, context_hash)
    evaluation_data = target_plan["initial_strategy_update"]["candidates"][0][
        "measurement_contract"
    ]["protocol_version"]["evaluation_data"]
    evaluation_data["frozen_shards"] = ["x" * 4096 for _index in range(50)]

    assert len(canonical_json(target_plan).encode("utf-8")) > 192 * 1024
    assert validate_target_plan(
        target_plan,
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        plan_document=plan,
    ) == material_target_plan_hash(target_plan)


def test_domain_validator_preserves_legal_nested_empty_object() -> None:
    plan = _plan_document()
    target_plan = _target_plan(plan, "a" * 64)
    candidate = deepcopy(
        target_plan["initial_strategy_update"]["candidates"][0]
    )
    candidate["measurement_contract"]["protocol_version"][
        "evaluation_data"
    ]["optional_metadata"] = {}
    completion = normalized_completion_contract_from_dict(
        target_plan["completion_contract"],
        plan_document=plan,
    )

    formal_target_candidate_from_dict(
        candidate,
        completion_contract=completion,
    )


@pytest.mark.parametrize(
    ("provider_wire_mode", "encoded_document"),
    [
        ("strict", '{"alpha":1e999}'),
        ("strict", '{"alpha":-1e999}'),
        ("envelope_only", None),
        ("raw", None),
    ],
)
def test_invalid_provider_transport_is_terminal_and_never_replayed(
    tmp_path: Path,
    provider_wire_mode: str,
    encoded_document: str | None,
) -> None:
    plan = _plan_document()
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-transport",
        "accepted_question_binding": {"question_ref": "question:bundle-1"},
        "accepted_formal_plan_binding": {
            "formal_plan_ref": "formal-plan:bundle-1",
            "plan_document": plan,
            "plan_document_hash": canonical_hash(plan),
            "answer_contract_hash": "a" * 64,
        },
    }
    context_hash = canonical_hash(context)
    target_plan = _target_plan(plan, context_hash)
    if encoded_document is not None:
        target_plan["initial_strategy_update"]["candidates"][0][
            "measurement_contract"
        ]["baseline_forward_contract"] = encoded_document
    workspace = tmp_path / (
        "bundle-transport-"
        + provider_wire_mode
        + ("-negative" if encoded_document and "-1e999" in encoded_document else "")
    )
    executable = str(_fake_codex(tmp_path / "codex-bundle-transport"))
    runner = _SequenceRunner(
        [{"target_plan": target_plan}],
        provider_wire_mode=provider_wire_mode,
    )
    adapter = CodexBundleSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-transport",
        run_ref="bundle-run:transport",
        attempt_ref="bundle-attempt:transport",
        fence_ref="bundle-fence:transport",
        cycle_ref="cycle:bundle-transport",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-transport",
        runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:transport",
            attempt_ref="bundle-attempt:transport",
            fence_ref="bundle-fence:transport",
        ),
        job_ref="bundle-provider-transport-job",
    )

    with pytest.raises(
        BundleSkillUnavailable,
        match="bundle_primary_result_contract_invalid",
    ) as caught:
        adapter.generate_draft(request)
    assert caught.value.recovery_checkpoint is not None
    assert caught.value.recovery_checkpoint["contract_failure_detail_code"] == (
        "codex_bundle_primary_invalid"
    )
    assert len(runner.calls) == 1

    no_replay = _SequenceRunner([])
    restarted = CodexBundleSkillAdapter(
        workspace,
        executable=executable,
        model_ref="test-model",
        process_runner=no_replay,
    )
    restarted.bind_full_conformance_authority(_FullConformanceAuthority())
    restarted.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    with pytest.raises(
        BundleSkillUnavailable,
        match="bundle_primary_result_contract_invalid",
    ):
        restarted.generate_draft(
            replace(request, runtime_binding=restarted.runtime_binding())
        )
    assert no_replay.calls == []


def test_bundle_adapter_transports_a_legal_target_plan_larger_than_one_mib(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    question_binding = {"question_ref": "question:bundle-1"}
    formal_plan_binding = {
        "formal_plan_ref": "formal-plan:bundle-1",
        "plan_document": plan,
        "plan_document_hash": canonical_hash(plan),
        "answer_contract_hash": "a" * 64,
    }
    context = {
        "schema_ref": "meta-research/bundle-context-pack/v1",
        "cycle_ref": "cycle:bundle-large",
        "accepted_question_binding": question_binding,
        "accepted_formal_plan_binding": formal_plan_binding,
    }
    context_hash = canonical_hash(context)
    target_plan = _target_plan(plan, context_hash)
    evaluation_data = target_plan["initial_strategy_update"]["candidates"][0][
        "measurement_contract"
    ]["protocol_version"]["evaluation_data"]
    evaluation_data["frozen_shards"] = ["x" * 4096 for _index in range(260)]
    encoded_target_plan = canonical_json(target_plan).encode("utf-8")
    assert 1024 * 1024 < len(encoded_target_plan) <= MAX_BUNDLE_TARGET_PLAN_BYTES

    runner = _SequenceRunner([{"target_plan": target_plan}])
    adapter = CodexBundleSkillAdapter(
        tmp_path / "large-target-plan-provider",
        executable=str(_fake_codex(tmp_path / "large-target-plan-codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    request = BundleSkillRequest(
        stage_request_ref="stage-request:bundle-large",
        run_ref="bundle-run:large",
        attempt_ref="bundle-attempt:large",
        fence_ref="bundle-fence:large",
        cycle_ref="cycle:bundle-large",
        question_ref="question:bundle-1",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        context_pack=context,
        plan_document=plan,
        root_session_ref="ar-session:bundle-large",
        runtime_binding=adapter.runtime_binding(),
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:large",
            attempt_ref="bundle-attempt:large",
            fence_ref="bundle-fence:large",
        ),
    )

    draft = adapter.generate_draft(request)

    assert draft.draft == target_plan
    assert validate_target_plan(
        draft.draft,
        formal_plan_ref=request.formal_plan_ref,
        context_pack_ref=request.context_pack_ref,
        context_pack_hash=request.context_pack_hash,
        plan_document=request.plan_document,
    ) == material_target_plan_hash(target_plan)
    result_path = next(
        (tmp_path / "large-target-plan-provider").glob(
            "idea-provider-*/last-message.json"
        ),
        None,
    )
    assert result_path is None  # The non-durable spool was cleaned after decoding.


def test_bundle_durable_runner_uses_the_sealed_stream_limit(tmp_path: Path) -> None:
    runner = _DurableLimitProbe()
    adapter = CodexBundleSkillAdapter(
        tmp_path / "bundle-durable-limit",
        process_runner=runner,
    )

    with pytest.raises(IdeaSkillUnavailable, match="codex_cli_io_unavailable"):
        adapter._invoke(
            operation_name="target-batch-2",
            prompt="bounded prompt",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            native_session_ref=None,
            job_ref="bundle-provider-operation:limit-probe",
        )

    assert runner.stdout_max_bytes == BUNDLE_PROVIDER_TRANSPORT_LIMITS.stream_max_bytes


def test_bundle_transport_limits_are_sealed_and_reused_on_durable_restart(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "large-bundle-transport"
    result_document = {"payload": "r" * (1024 * 1024 + 4096)}
    prompt = "p" * (1024 * 1024 + 4096)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["payload"],
        "properties": {"payload": {"type": "string"}},
    }
    runner = _LargeSequenceRunner([result_document])
    first = CodexBundleSkillAdapter(workspace, process_runner=runner)

    result = first._invoke(
        operation_name="target-batch-2",
        prompt=prompt,
        schema=schema,
        native_session_ref=None,
        job_ref="bundle-provider-operation:large",
    )

    assert result[0] == result_document
    assert len(result[2].encode("utf-8")) > 16 * 1024 * 1024
    operation = next(workspace.glob("provider-operations/*/target-batch-2"))
    invocation_path = operation / "invocation.json"
    invocation_envelope = json.loads(invocation_path.read_text(encoding="utf-8"))
    invocation = invocation_envelope["payload"]
    assert invocation["schema_ref"] == "meta-research/codex-provider-operation/v3"
    assert "root_capability_diagnostics" not in invocation
    assert {
        name: invocation[name]
        for name in (
            "prompt_max_bytes",
            "stream_max_bytes",
            "result_max_bytes",
        )
    } == BUNDLE_PROVIDER_TRANSPORT_LIMITS.as_dict()
    assert (operation / "prompt.txt").stat().st_size > 1024 * 1024
    assert (operation / "stdout.jsonl").stat().st_size > 16 * 1024 * 1024
    assert (operation / "last-message.json").stat().st_size > 1024 * 1024

    (operation / "completed.json").unlink()
    no_replay = _LargeSequenceRunner([])
    restarted = CodexBundleSkillAdapter(workspace, process_runner=no_replay)
    recovered = restarted._invoke(
        operation_name="target-batch-2",
        prompt=prompt,
        schema=schema,
        native_session_ref=None,
        job_ref="bundle-provider-operation:large",
    )

    assert recovered == result
    assert no_replay.calls == []

    invocation_envelope["payload"]["result_max_bytes"] -= 1
    invocation_path.write_text(
        json.dumps(invocation_envelope, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    with pytest.raises(
        BundleSkillUnavailable,
        match="codex_operation_identity_conflict",
    ):
        restarted._invoke(
            operation_name="target-batch-2",
            prompt=prompt,
            schema=schema,
            native_session_ref=None,
            job_ref="bundle-provider-operation:large",
        )


def test_bundle_large_rolling_prompt_reaches_the_public_batch_operation(
    tmp_path: Path,
) -> None:
    plan = _plan_document()
    context_hash = "7" * 64
    target_plan = _target_plan(plan, context_hash)
    base_spec = target_plan["initial_strategy_update"]["candidates"][0]
    cells = ["cell:structure-primary"]
    target_specs = [base_spec]
    for index in range(1, 5):
        label = f"structure-{index}"
        cell = f"cell:structure-{index}"
        implementation_ref = f"implementation:{label}"
        implementation_hash = canonical_hash({"implementation": label})
        source_version_ref = f"source-version:{label}"
        spec = deepcopy(base_spec)
        candidate = spec["candidate"]
        candidate["local_label"] = f"target:{label}"
        candidate["measurement_unit_keys"] = [cell]
        candidate["implementation_revision_ref"] = implementation_ref
        source_proof = candidate["reuse_trace"]["tier_decisions"][0][
            "source_proofs"
        ][0]
        source_proof.update(
            {
                "source_ref": f"source:{label}",
                "exact_version_ref": source_version_ref,
                "implementation_revision_ref": implementation_ref,
                "verification_receipt": _receipt(
                    f"source-verification:{label}", source_version_ref
                ),
                "implementation_binding": {
                    "subject_ref": implementation_ref,
                    "content_hash_ref": implementation_hash,
                },
                "implementation_acceptance_receipt": _receipt(
                    f"implementation-acceptance:{label}", implementation_hash
                ),
            }
        )
        candidate["routes"] = [
            {
                "route_ref": f"route:{label}",
                "known_external_operation_refs": [],
            }
        ]
        spec["measurement_contract"] = _measurement_contract(label, cell)
        cells.append(cell)
        target_specs.append(spec)
    target_plan["completion_contract"]["experiments"][0]["brief"][
        "required_measurement_unit_keys"
    ] = cells
    target_plan["initial_strategy_update"]["candidates"] = target_specs
    assert validate_target_plan(
        target_plan,
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        plan_document=plan,
    )

    runner = _SequenceRunner(
        [
            {
                "strategy_update": {
                    "schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": 2,
                    "candidates": [],
                    "requires_accepted_labels": [],
                    "strategy_complete": True,
                },
                "rationale": "The immutable completion cell is committed.",
            }
        ]
    )
    adapter = CodexBundleSkillAdapter(
        tmp_path / "large-rolling-provider",
        executable=str(_fake_codex(tmp_path / "large-rolling-codex")),
        process_runner=runner,
    )
    adapter.bind_full_conformance_authority(_FullConformanceAuthority())
    adapter.configure_resident_mcp_endpoint("http://127.0.0.1:8765")
    binding = adapter.runtime_binding()
    projection_chunks = ["x" * 4096 for _index in range(900)]
    current_targets = tuple(
        {
            "target_ref": f"target:large-rolling-{index}",
            "target_key": spec["candidate"]["local_label"],
            "spec_hash": canonical_hash(spec),
            "spec": spec,
            "dependency_refs": [],
            "receipt": {},
        }
        for index, spec in enumerate(target_specs)
    )
    target_commits = tuple(
        {
            "commit_ref": f"target-commit:large-rolling-{index}",
            "target_ref": current["target_ref"],
            "target_run_ref": f"target-run:large-rolling-{index}",
            "evaluation_attempt_ref": f"evaluation-attempt:large-rolling-{index}",
            "target_spec_hash": current["spec_hash"],
            "closure_hash": canonical_hash(
                {"target_ref": current["target_ref"], "index": index}
            ),
            "result_disposition": "accepted",
            "protocol": {
                "protocol_version_ref": f"protocol-version:large-rolling-{index}",
                "frozen_projection_chunks": projection_chunks,
            },
            "metric_result": {
                "metric_result_ref": f"metric-result:large-rolling-{index}",
                "metric_values": [{"metric_key": "agreement", "value": 1.0}],
            },
            "receipt": {},
        }
        for index, current in enumerate(current_targets)
    )
    assert all(
        len(canonical_json(commit).encode("utf-8"))
        < BUNDLE_HANDOFF_MAX_SERIALIZED_BYTES
        for commit in target_commits
    )
    request = BundleTargetBatchRequest(
        stage_request_ref="stage-request:bundle-large-rolling",
        run_ref="bundle-run:large-rolling",
        attempt_ref="bundle-attempt:large-rolling",
        fence_ref="bundle-fence:large-rolling",
        graph_ref="target-graph:large-rolling",
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        plan_document=plan,
        initial_target_plan=target_plan,
        base_generation=0,
        base_head_receipt={
            "receipt_ref": "target-graph-head-receipt:large-rolling",
            "receipt_kind": "target_graph_head_acceptance",
            "owner": "research_graph",
            "subject_ref": "target-graph:large-rolling",
            "payload_hash": "b" * 64,
            "bindings": {},
        },
        current_targets=current_targets,
        target_commits=target_commits,
        root_session_ref="ar-session:bundle-large-rolling",
        native_session_ref="codex-bundle-primary:1",
        runtime_binding=binding,
        inbox_checkpoint=_inbox_checkpoint(
            run_ref="bundle-run:large-rolling",
            attempt_ref="bundle-attempt:large-rolling",
            fence_ref="bundle-fence:large-rolling",
        ),
    )

    result = adapter.propose_target_batch(request)

    assert result.strategy_update["strategy_complete"] is True
    assert len(runner.calls) == 1
    assert len(runner.calls[0][1].encode("utf-8")) > 16 * 1024 * 1024


@pytest.mark.parametrize("adapter_type", [CodexIdeaSkillAdapter, CodexPlanSkillAdapter])
def test_idea_and_plan_keep_the_existing_transport_limits(
    tmp_path: Path,
    adapter_type: type[CodexIdeaSkillAdapter],
) -> None:
    runner = _SequenceRunner([{"payload": "unused"}])
    adapter = adapter_type(tmp_path / adapter_type.__name__, process_runner=runner)

    assert adapter._provider_transport_limits.as_dict() == {
        "prompt_max_bytes": PROVIDER_STREAM_MAX_BYTES,
        "stream_max_bytes": PROVIDER_STREAM_MAX_BYTES,
        "result_max_bytes": PROVIDER_RESULT_MAX_BYTES,
    }
    local_limit = 64 * 1024
    adapter._provider_transport_limits = ProviderTransportLimits(
        prompt_max_bytes=local_limit,
        stream_max_bytes=local_limit,
        result_max_bytes=local_limit,
    )
    with pytest.raises(IdeaSkillUnavailable, match="codex_prompt_too_large"):
        adapter._invoke(
            operation_name="primary",
            prompt="x" * (local_limit + 1),
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            native_session_ref=None,
            job_ref="unchanged-default-limit",
        )
    assert runner.calls == []
    assert not (tmp_path / adapter_type.__name__ / "provider-operations").exists()


def test_bundle_oversized_prompt_fails_before_provider_or_spool(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([{"payload": "unused"}])
    workspace = tmp_path / "oversized-bundle-prompt"
    adapter = CodexBundleSkillAdapter(workspace, process_runner=runner)

    with pytest.raises(BundleSkillUnavailable, match="codex_prompt_too_large"):
        adapter._invoke(
            operation_name="target-batch-2",
            prompt="x" * (BUNDLE_PROVIDER_TRANSPORT_LIMITS.prompt_max_bytes + 1),
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
                "required": [],
            },
            native_session_ref=None,
            job_ref="bundle-provider-operation:oversized",
        )
    assert runner.calls == []
    assert not (workspace / "provider-operations").exists()


def test_provider_dialect_gate_fails_before_runner_or_durable_spool(
    tmp_path: Path,
) -> None:
    runner = _SequenceRunner([])
    workspace = tmp_path / "invalid-provider-schema"
    adapter = CodexBundleSkillAdapter(workspace, process_runner=runner)

    with pytest.raises(
        BundleSkillUnavailable, match="codex_output_schema_invalid"
    ):
        adapter._invoke(
            operation_name="primary",
            prompt="valid prompt",
            schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"value": {"type": "string"}},
                "required": [],
            },
            native_session_ref=None,
            job_ref="bundle-provider-operation:invalid-schema",
        )
    assert runner.calls == []
    assert not (workspace / "provider-operations").exists()


def test_metric_and_result_schema_content_are_frozen_into_target_plan_hash() -> None:
    plan = _plan_document()
    context_hash = "6" * 64
    target_plan = _target_plan(plan, context_hash)
    original_hash = validate_target_plan(
        target_plan,
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        plan_document=plan,
    )

    metric_drift = deepcopy(target_plan)
    metric_drift["initial_strategy_update"]["candidates"][0][
        "measurement_contract"
    ]["protocol_version"]["required_metrics"][0]["definition"][
        "formula"
    ] = "weighted matching / total"
    assert material_target_plan_hash(metric_drift) != original_hash

    schema_drift = deepcopy(target_plan)
    schema_drift["initial_strategy_update"]["candidates"][0][
        "measurement_contract"
    ]["result_schema"]["required"] = ["metric_values", "assets"]
    assert material_target_plan_hash(schema_drift) != original_hash


def test_initial_nonempty_update_may_atomically_seal_exact_completion() -> None:
    plan = _plan_document()
    context_hash = "7" * 64
    target_plan = _target_plan(plan, context_hash)
    completion = target_plan["completion_contract"]
    completion["experiments"][0]["brief"][
        "required_measurement_unit_keys"
    ] = ["cell:structure-primary"]
    target_plan["initial_strategy_update"]["strategy_complete"] = True

    validated = validate_target_plan(
        target_plan,
        formal_plan_ref="formal-plan:bundle-1",
        context_pack_ref="context-pack:bundle-1",
        context_pack_hash=context_hash,
        plan_document=plan,
    )
    assert validated == material_target_plan_hash(target_plan)


def test_target_plan_rejects_a_cycle_before_research_graph_acceptance() -> None:
    plan = _plan_document()
    context_hash = "9" * 64
    target_plan = _target_plan(plan, context_hash)
    first = target_plan["initial_strategy_update"]["candidates"][0]
    assert isinstance(first, dict)
    second = deepcopy(first)
    first_candidate = first["candidate"]
    second_candidate = second["candidate"]
    first_candidate["depends_on_labels"] = ["target:replication"]
    second_candidate["local_label"] = "target:replication"
    second_candidate["measurement_unit_keys"] = ["cell:structure-replication"]
    second["measurement_contract"] = _measurement_contract(
        "replication",
        "cell:structure-replication",
    )
    second_candidate["depends_on_labels"] = ["target:structure"]
    second_candidate["implementation_revision_ref"] = "implementation:replication"
    second_candidate["reuse_trace"]["tier_decisions"][0]["source_proofs"][0][
        "implementation_revision_ref"
    ] = "implementation:replication"
    second_hash = canonical_hash({"implementation": "replication"})
    second_candidate["reuse_trace"]["tier_decisions"][0]["source_proofs"][0][
        "implementation_binding"
    ] = {
        "subject_ref": "implementation:replication",
        "content_hash_ref": second_hash,
    }
    second_candidate["reuse_trace"]["tier_decisions"][0]["source_proofs"][0][
        "implementation_acceptance_receipt"
    ] = _receipt("implementation-acceptance:replication", second_hash)
    target_plan["initial_strategy_update"]["candidates"] = [first, second]

    with pytest.raises(BundleContractError, match="strategy_dependency_cycle"):
        validate_target_plan(
            target_plan,
            formal_plan_ref="formal-plan:bundle-1",
            context_pack_ref="context-pack:bundle-1",
            context_pack_hash=context_hash,
            plan_document=plan,
        )


def test_default_v3_rejects_v2_and_v2_diagnostic_never_yields_completion() -> None:
    plan = _plan_document()
    context_hash = "8" * 64
    target_plan = _target_plan(plan, context_hash)
    legacy_target = {
        "target_key": "legacy-structure",
        "title": "legacy minimal Target",
        "experiment_key": "experiment:structure",
        "gap_obligation_keys": ["gap:structure"],
        "depends_on": [],
        "goal": "legacy goal",
        "characteristics": "legacy characteristics",
        "boundary_constraints": "legacy boundary",
        "semantic_delta": "legacy delta",
        "contributing_idea_refs": ["idea:structure"],
        "risk_class": "normal",
        "execution": {
            "adapter_kind": "experiment_retrain",
            "schema_ref": "meta-research/target-execution/experiment-retrain/v1",
            "request": {
                "hypothesis": "legacy execution payload",
                "variant_parameter": 0.25,
                "sample_count": 8,
            },
        },
    }
    legacy_plan = {
        "schema_ref": "meta-research/target-plan/v2",
        "kind": "TargetPlan",
        "formal_plan_ref": "formal-plan:bundle-1",
        "context_pack_ref": "context-pack:bundle-1",
        "targets": [legacy_target],
        "strategy_complete": False,
        "source_bindings": target_plan["source_bindings"],
    }

    with pytest.raises(BundleContractError, match="target_plan_invalid"):
        validate_target_plan(
            legacy_plan,
            formal_plan_ref="formal-plan:bundle-1",
            context_pack_ref="context-pack:bundle-1",
            context_pack_hash=context_hash,
            plan_document=plan,
        )
    diagnosed = diagnose_legacy_target_plan_v2(legacy_plan)
    assert len(diagnosed) == 1
    assert not hasattr(diagnosed[0], "strategy_complete")
    assert not hasattr(diagnosed[0], "candidate")

    legacy_plan["strategy_complete"] = True
    with pytest.raises(BundleContractError, match="legacy_target_plan_v2_invalid"):
        diagnose_legacy_target_plan_v2(legacy_plan)


def test_cancel_reconciliation_inspects_dynamic_bundle_dispatch_phases(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "provider"
    adapter = CodexBundleSkillAdapter(
        workspace,
        executable=str(_fake_codex(tmp_path / "codex")),
        process_runner=_SequenceRunner([]),
    )
    job_ref = "bundle-job:cancel-reconciliation"
    operation = (
        workspace
        / "provider-operations"
        / canonical_hash({"job_ref": job_ref})
        / "dispatch-1"
    )
    operation.mkdir(parents=True)
    (operation / "partial-spool").write_text("unknown", encoding="utf-8")

    assert adapter.reconcile_cancelled_job(job_ref) is False
