import json
from pathlib import Path

import pytest

from provider_supervisor_support import closed_tree_fixture
from meta_research.owners.common import canonical_json
from meta_research.provider_supervisor import (
    ensure_transport_key,
    read_transport_key_for_workspace,
    read_transport_envelope,
    write_exit_receipt,
    write_transport_envelope,
)
from meta_research.harness_adapters import HarnessSupervisorTransport
from meta_research.root_session_usage import (
    RootSessionUsageError,
    aggregate_root_session_usage,
    read_root_session_observations,
)
from meta_research.quest_drafting import _remove_durable_job


def _native_record(session: str, response_id: str, input_tokens: int) -> dict[str, object]:
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": input_tokens - 20,
        "output_tokens": 10,
        "total_tokens": input_tokens - 10,
    }
    return {
        "type": "token_usage_record",
        "timestamp": response_id,
        "payload": {
            "thread_id": session,
            "session_id": session,
            "response_id": response_id,
            "turn_id": response_id,
            "root_turn_id": response_id,
            "usage": usage,
            "turn_token_usage": usage,
            "thread_token_usage": usage,
        },
    }


def _operation(
    workspace: Path,
    codex_home: Path,
    key: bytes,
    *,
    operation_ref: str,
    invocation_hash: str,
    session: str,
) -> None:
    directory = (
        workspace
        / "provider-operations"
        / "operation-root"
        / operation_ref
    )
    directory.mkdir(parents=True, exist_ok=True)
    prompt = directory / "prompt.txt"
    schema = directory / "output-schema.json"
    stdout = directory / "stdout.jsonl"
    result = directory / "result.json"
    prompt.write_text("First business prompt " + operation_ref, encoding="utf-8")
    schema.write_text("{}", encoding="utf-8")
    stdout.write_bytes(
        b"".join(
            canonical_json(event).encode("utf-8") + b"\n"
            for event in (
                {"type": "thread.started", "thread_id": session},
                {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
            )
        )
    )
    result.write_text("{}", encoding="utf-8")
    write_exit_receipt(
        directory / "supervisor-exit.json",
        key=key,
        invocation_hash=invocation_hash,
        prompt_path=prompt,
        schema_path=schema,
        stdout_path=stdout,
        result_path=result,
        returncode=0,
        input_bytes=prompt.stat().st_size,
        process_tree=closed_tree_fixture(),
        codex_home=codex_home,
        operation_scope={
            "run_ref": "run-root-usage",
            "attempt_ref": "attempt-root-usage",
            "fence_ref": "fence-root-usage",
            "root_session_ref": "root-session-usage",
            "root_kind": "idea",
            "operation_ref": operation_ref,
            "job_ref": operation_ref,
            "model_ref": "gpt-test",
        },
    )


def test_root_session_index_is_signed_idempotent_and_deduplicates_responses(tmp_path):
    workspace = tmp_path / "idea-skill-provider"
    codex_home = tmp_path / "codex-home"
    operation_root = workspace / "provider-operations"
    operation_root.mkdir(parents=True)
    _, key = ensure_transport_key(workspace)
    session = "01a09bb2-95ed-7931-bd38-3b8ebd2de28b"
    native = codex_home / "sessions/2026/09/13" / (
        "rollout-2026-09-13T16-56-07-" + session + ".jsonl"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(
        canonical_json({"type": "session_meta", "payload": {"id": session}}).encode()
        + b"\n"
        + canonical_json(_native_record(session, "response-1", 100)).encode()
        + b"\n"
    )
    _operation(
        workspace, codex_home, key,
        operation_ref="operation-1", invocation_hash="a" * 64, session=session,
    )
    _operation(
        workspace, codex_home, key,
        operation_ref="operation-1", invocation_hash="a" * 64, session=session,
    )
    native.write_bytes(
        native.read_bytes()
        + canonical_json(_native_record(session, "response-2", 200)).encode()
        + b"\n"
    )
    _operation(
        workspace, codex_home, key,
        operation_ref="operation-2", invocation_hash="b" * 64, session=session,
    )

    observations = read_root_session_observations(workspace, "root-session-usage")
    assert len(observations) == 2
    aggregate = aggregate_root_session_usage("root-session-usage", observations)
    assert aggregate["deduplicated_response_count"] == 2
    assert aggregate["cumulative_input_tokens_observed"] == 300
    assert aggregate["cumulative_output_tokens_observed"] == 20
    assert aggregate["cumulative_cached_input_tokens_observed"] == 260
    assert aggregate["first_model_request_input_tokens_observed"] == 100
    assert aggregate["first_model_request_status"] == "observed_native_prefix_order"
    assert aggregate["peak_single_request_input_tokens_observed"] == 200
    assert aggregate["peak_cached_prefix_tokens_observed"] == 180
    assert aggregate["model_context_windows_observed"] == []
    assert aggregate["model_context_windows_status"] == "unknown"
    assert aggregate["compactions_observed"] == []
    assert aggregate["cached_prefix_counts_toward_context_window"] is True
    assert aggregate["first_business_prompt"]["status"] == (
        "unknown_without_explicit_operation_order"
    )
    assert aggregate["child_sessions"]["status"] == "unknown_child_usage_not_indexed"
    assert aggregate["team_total"]["status"] == "unknown_cross_root_aggregation_not_indexed"

    index_dir = workspace / "provider-observations/root-sessions"
    pointer = next(
        path for path in index_dir.rglob("*.json")
        if b"operation-1" in path.read_bytes()
    )
    original = pointer.read_bytes()
    pointer.write_bytes(original.replace(b"operation-1", b"operation-x"))
    with pytest.raises(RootSessionUsageError, match="root_session_observation_index_invalid"):
        read_root_session_observations(workspace, "root-session-usage")
    pointer.write_bytes(original)

    duplicate = pointer.with_name("duplicate.json")
    duplicate_payload = read_transport_envelope(pointer, read_transport_key_for_workspace(workspace))
    write_transport_envelope(duplicate, duplicate_payload, read_transport_key_for_workspace(workspace))
    with pytest.raises(ValueError, match="root_session_observation_duplicate"):
        read_root_session_observations(workspace, "root-session-usage")


def test_partial_native_usage_is_limited_without_crashing():
    aggregate = aggregate_root_session_usage(
        "root-session-partial",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-partial",
                        "model_responses": [
                            {
                                "response_id": "missing-input",
                                "output_tokens": 10,
                                "cached_input_tokens": 2,
                            },
                            {
                                "response_id": "missing-cache",
                                "input_tokens": 100,
                                "output_tokens": 10,
                            },
                        ],
                    }
                },
            }
        ],
    )
    assert aggregate["deduplicated_response_count"] == 1
    assert aggregate["cumulative_input_tokens_observed"] == 100
    assert aggregate["cumulative_output_tokens_observed"] == 10
    assert aggregate["cumulative_cached_input_tokens_observed"] is None
    assert aggregate["first_model_request_input_tokens_observed"] is None
    assert aggregate["first_model_request_status"] == "unknown_response_order"
    assert aggregate["peak_single_request_input_tokens_observed"] == 100
    assert aggregate["peak_cached_prefix_tokens_observed"] is None
    assert aggregate["cached_prefix_counts_toward_context_window"] is None
    assert aggregate["limited"] is True


def test_multiple_native_sessions_keep_first_request_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-replaced",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-a",
                        "model_context_windows": [128000],
                        "compactions": [
                            {
                                "source_sequence": 4,
                                "source_byte_offset": 30,
                                "timestamp": "2026-11-21T00:00:00Z",
                            }
                        ],
                        "model_responses": [
                            {
                                "response_id": "a-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 80,
                                "output_tokens": 8,
                            }
                        ],
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-b",
                        "model_responses": [
                            {
                                "response_id": "b-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 120,
                                "output_tokens": 12,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["first_model_request_input_tokens_observed"] is None
    assert aggregate["first_model_request_status"] == "unknown_response_order"
    assert aggregate["status"] == "partial_multiple_native_sessions"
    assert aggregate["cumulative_input_tokens_observed"] is None
    assert aggregate["cumulative_output_tokens_observed"] is None
    assert aggregate["peak_single_request_input_tokens_observed"] is None
    assert aggregate["model_context_windows_observed"] == [128000]
    assert aggregate["compactions_observed"] == [
        {
            "native_session_ref": "native-a",
            "source_sequence": 4,
            "source_byte_offset": 30,
            "timestamp": "2026-11-21T00:00:00Z",
        }
    ]


def test_compaction_aggregation_is_bounded():
    aggregate = aggregate_root_session_usage(
        "root-session-compactions",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-compactions",
                        "compactions": [
                            {"source_sequence": index, "source_byte_offset": index}
                            for index in range(513)
                        ],
                    }
                },
            }
        ],
    )
    assert len(aggregate["compactions_observed"]) == 512
    assert aggregate["compaction_count_observed"] == 512
    assert aggregate["compaction_count_status"] == "lower_bound"
    assert aggregate["limited"] is True


def test_duplicate_compaction_prefix_does_not_trigger_limit():
    compactions = [
        {"source_sequence": index, "source_byte_offset": index}
        for index in range(512)
    ]
    aggregate = aggregate_root_session_usage(
        "root-session-duplicate-compaction-prefix",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-compactions",
                        "compactions": compactions,
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-compactions",
                        "compactions": compactions,
                    }
                },
            },
        ],
    )
    assert aggregate["compaction_count_observed"] == 512
    assert aggregate["compaction_count_status"] == "observed_exact"
    assert aggregate["limited"] is False


def test_compactions_deduplicate_without_timestamp():
    aggregate = aggregate_root_session_usage(
        "root-session-compaction-dedup",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-compactions",
                        "compactions": [
                            {"source_sequence": 4, "source_byte_offset": 30},
                            {
                                "source_sequence": 4,
                                "source_byte_offset": 30,
                                "timestamp": "2026-11-21T00:00:00Z",
                            },
                        ],
                    }
                },
            }
        ],
    )
    assert aggregate["compactions_observed"] == [
        {
            "native_session_ref": "native-compactions",
            "source_sequence": 4,
            "source_byte_offset": 30,
        }
    ]
    assert aggregate["compaction_count_status"] == "observed_exact"


def test_limited_native_prefix_keeps_first_request_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-limited-order",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-limited",
                        "status": "available",
                        "limited": True,
                        "model_responses": [
                            {
                                "response_id": "response-2",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 200,
                                "output_tokens": 20,
                                "cached_input_tokens": 100,
                            }
                        ],
                    }
                },
            }
        ],
    )
    assert aggregate["first_model_request_input_tokens_observed"] is None
    assert aggregate["first_model_request_status"] == "unknown_response_order"


def test_conflicting_response_source_order_is_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-order-conflict",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-conflict",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                            }
                        ],
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-conflict",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 3,
                                "source_byte_offset": 20,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["first_model_request_status"] == "unknown_response_order"
    assert aggregate["limited"] is True


def test_unavailable_native_session_makes_root_order_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-unavailable-native",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "unavailable",
                        "reason": "native_session_missing_or_ambiguous",
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-available",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["status"] == "partial_native_session_unavailable"
    assert aggregate["first_model_request_status"] == "unknown_response_order"
    assert aggregate["first_model_request_input_tokens_observed"] is None


def test_unavailable_native_context_window_is_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-unavailable-window",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "unavailable",
                        "model_context_windows": [128000],
                    }
                },
            }
        ],
    )
    assert aggregate["model_context_windows_observed"] == [128000]
    assert aggregate["model_context_windows_status"] == "unknown"


def test_available_native_without_session_id_is_partial_and_unusable():
    aggregate = aggregate_root_session_usage(
        "root-session-missing-native-id",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "model_context_windows": [128000],
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 1,
                                "source_byte_offset": 1,
                                "input_tokens": 100,
                                "output_tokens": 10,
                            }
                        ],
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-valid",
                        "model_responses": [
                            {
                                "response_id": "response-2",
                                "source_sequence": 2,
                                "source_byte_offset": 2,
                                "input_tokens": 120,
                                "output_tokens": 12,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["status"] == "partial_native_session_unavailable"
    assert aggregate["cumulative_input_tokens_observed"] is None
    assert aggregate["first_model_request_input_tokens_observed"] is None
    assert aggregate["model_context_windows_status"] == "unknown"


def test_missing_native_response_list_keeps_root_order_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-missing-responses",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-missing",
                        "model_responses": None,
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-missing",
                        "model_responses": [
                            {
                                "response_id": "response-2",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 200,
                                "output_tokens": 20,
                                "cached_input_tokens": 100,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["first_model_request_status"] == "unknown_response_order"
    assert aggregate["first_model_request_input_tokens_observed"] is None


def test_missing_cache_on_one_duplicate_response_remains_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-cache-duplicate",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-cache-duplicate",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 100,
                                "output_tokens": 10,
                            }
                        ],
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "session_id": "native-cache-duplicate",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 2,
                                "source_byte_offset": 10,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["deduplicated_response_count"] == 1
    assert aggregate["cumulative_cached_input_tokens_observed"] is None
    assert aggregate["peak_cached_prefix_tokens_observed"] is None


def test_empty_context_window_sample_makes_mixed_sample_unknown():
    aggregate = aggregate_root_session_usage(
        "root-session-mixed-context-window-sample",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-context",
                        "model_context_windows": [128000],
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 1,
                                "source_byte_offset": 1,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                            }
                        ],
                    }
                },
            },
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-context",
                        "model_context_windows": [],
                        "model_responses": [
                            {
                                "response_id": "response-2",
                                "source_sequence": 2,
                                "source_byte_offset": 1,
                                "input_tokens": 120,
                                "output_tokens": 12,
                                "cached_input_tokens": 100,
                            }
                        ],
                    }
                },
            },
        ],
    )
    assert aggregate["model_context_windows_observed"] == [128000]
    assert aggregate["model_context_windows_status"] == "unknown"


def test_unknown_cache_semantics_does_not_claim_context_occupancy():
    aggregate = aggregate_root_session_usage(
        "root-session-unknown-cache-semantics",
        [
            {
                "pointer": {},
                "observation": {
                    "native_session": {
                        "status": "available",
                        "session_id": "native-cache-semantics",
                        "model_responses": [
                            {
                                "response_id": "response-1",
                                "source_sequence": 1,
                                "source_byte_offset": 1,
                                "input_tokens": 100,
                                "output_tokens": 10,
                                "cached_input_tokens": 80,
                                "cache_semantics": "provider-dependent",
                            }
                        ],
                    }
                },
            }
        ],
    )
    assert aggregate["cached_prefix_semantics"] == ["provider-dependent"]
    assert aggregate["cached_prefix_counts_toward_context_window"] is None


def test_harness_supervisor_request_carries_admitted_operation_scope(tmp_path):
    scope = {
        "run_ref": "run-target",
        "attempt_ref": "attempt-target",
        "fence_ref": "fence-target",
        "root_session_ref": "root-target",
        "root_kind": "target",
        "operation_ref": "operation-target",
        "model_ref": "gpt-test",
    }
    request = HarnessSupervisorTransport._supervisor_request(
        tmp_path,
        "a" * 64,
        "codex",
        30.0,
        operation_scope=scope,
    )
    assert request["operation_scope"] == scope


def test_root_session_index_survives_durable_operation_cleanup(tmp_path):
    workspace = tmp_path / "idea-skill-provider"
    codex_home = tmp_path / "codex-home"
    operation_root = workspace / "provider-operations"
    operation_root.mkdir(parents=True)
    _, key = ensure_transport_key(workspace)
    session = "01a09bb2-95ed-7931-bd38-3b8ebd2de28b"
    native = codex_home / "sessions/2026/09/13" / (
        "rollout-2026-09-13T16-56-07-" + session + ".jsonl"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(
        canonical_json({"type": "session_meta", "payload": {"id": session}}).encode()
        + b"\n"
        + canonical_json(_native_record(session, "response-1", 100)).encode()
        + b"\n"
    )
    _operation(
        workspace,
        codex_home,
        key,
        operation_ref="operation-retired",
        invocation_hash="c" * 64,
        session=session,
    )
    directory = workspace / "provider-operations" / "operation-root" / "operation-retired"
    assert len(read_root_session_observations(workspace, "root-session-usage")) == 1
    _remove_durable_job(directory)
    assert len(read_root_session_observations(workspace, "root-session-usage")) == 1


def test_symlinked_observation_path_is_rejected_before_archive_fallback(tmp_path):
    workspace = tmp_path / "idea-skill-provider"
    codex_home = tmp_path / "codex-home"
    operation_root = workspace / "provider-operations"
    operation_root.mkdir(parents=True)
    _, key = ensure_transport_key(workspace)
    session = "01a09bb2-95ed-7931-bd38-3b8ebd2de28b"
    native = codex_home / "sessions/2026/09/13" / (
        "rollout-2026-09-13T16-56-07-" + session + ".jsonl"
    )
    native.parent.mkdir(parents=True)
    native.write_bytes(
        canonical_json({"type": "session_meta", "payload": {"id": session}}).encode()
        + b"\n"
        + canonical_json(_native_record(session, "response-1", 100)).encode()
        + b"\n"
    )
    _operation(
        workspace,
        codex_home,
        key,
        operation_ref="operation-symlink",
        invocation_hash="d" * 64,
        session=session,
    )
    pointer_path = next(
        (workspace / "provider-observations/root-sessions").rglob("*.json")
    )
    pointer = read_transport_envelope(pointer_path, read_transport_key_for_workspace(workspace))
    observation_path = workspace / pointer["observation_path"]
    moved_path = observation_path.with_name("call-observation-real.json")
    observation_path.replace(moved_path)
    try:
        observation_path.symlink_to(moved_path)
    except OSError:
        moved_path.replace(observation_path)
        pytest.skip("symlink creation is unavailable in this environment")
    with pytest.raises(RootSessionUsageError, match="root_session_observation_path_invalid"):
        read_root_session_observations(workspace, "root-session-usage")
