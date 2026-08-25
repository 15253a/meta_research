from __future__ import annotations

import json

import pytest

from meta_research.target_run_runtime_contract import (
    TargetCompletionArtifact,
    TargetCompletionHandoff,
    TargetCompletionHandoffError,
    decode_target_completion_handoff,
    validate_target_completion_handoff,
)


def _handoff_document() -> dict[str, object]:
    return {
        "schema_ref": "meta-research/target-completion-handoff/v1",
        "target_ref": "target:root-owned",
        "target_run_ref": "target-run:root-owned",
        "status": "completed",
        "artifacts": [
            {"role": "implementation", "relative_path": "implementation"},
            {"role": "checkpoint", "relative_path": "outputs/final.ckpt"},
            {"role": "result", "relative_path": "outputs/metrics.json"},
            {"role": "log", "relative_path": "logs/train.log"},
        ],
        "result_document_path": "outputs/metrics.json",
        "summary": "Final training completed after root-owned iteration.",
    }


def test_completion_handoff_decodes_one_closed_final_workspace_manifest() -> None:
    handoff = decode_target_completion_handoff(
        json.dumps(_handoff_document(), ensure_ascii=False)
    )

    assert handoff == TargetCompletionHandoff(
        schema_ref="meta-research/target-completion-handoff/v1",
        target_ref="target:root-owned",
        target_run_ref="target-run:root-owned",
        status="completed",
        artifacts=(
            TargetCompletionArtifact(
                role="implementation", relative_path="implementation"
            ),
            TargetCompletionArtifact(
                role="checkpoint", relative_path="outputs/final.ckpt"
            ),
            TargetCompletionArtifact(
                role="result", relative_path="outputs/metrics.json"
            ),
            TargetCompletionArtifact(
                role="log", relative_path="logs/train.log"
            ),
        ),
        result_document_path="outputs/metrics.json",
        summary="Final training completed after root-owned iteration.",
    )
    assert validate_target_completion_handoff(
        handoff,
        expected_target_ref="target:root-owned",
        expected_target_run_ref="target-run:root-owned",
    ) is handoff


@pytest.mark.parametrize(
    "relative_path",
    (
        "/tmp/result.json",
        "../result.json",
        ".",
        "outputs/../result.json",
        "./result.json",
        "outputs//result.json",
        "outputs\\result.json",
        "outputs/result.json/",
        " outputs/result.json",
    ),
)
def test_completion_handoff_rejects_unsafe_or_noncanonical_paths(
    relative_path: str,
) -> None:
    document = _handoff_document()
    artifacts = list(document["artifacts"])
    artifacts[2] = {"role": "result", "relative_path": relative_path}
    document["artifacts"] = artifacts
    document["result_document_path"] = relative_path

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_ref", "meta-research/target-completion-handoff/v0"),
        ("status", "running"),
        ("target_ref", ""),
        ("target_run_ref", 7),
        ("summary", ""),
    ),
)
def test_completion_handoff_requires_the_final_completed_envelope(
    field: str, value: object
) -> None:
    document = _handoff_document()
    document[field] = value

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_rejects_unknown_roles_and_unbound_result_document() -> None:
    document = _handoff_document()
    document["artifacts"] = [
        {"role": "arbitrary", "relative_path": "outputs/metrics.json"}
    ]

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_requires_a_selected_implementation() -> None:
    document = _handoff_document()
    document["artifacts"] = [
        artifact
        for artifact in document["artifacts"]
        if artifact["role"] != "implementation"
    ]

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))

    document["artifacts"] = [
        {"role": "result", "relative_path": "outputs/other.json"}
    ]
    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


@pytest.mark.parametrize(
    "mutate",
    (
        lambda value: {**value, "unexpected": True},
        lambda value: {
            **value,
            "artifacts": [
                {**value["artifacts"][0], "unexpected": True},
                *value["artifacts"][1:],
            ],
        },
        lambda value: {
            key: item for key, item in value.items() if key != "summary"
        },
    ),
)
def test_completion_handoff_is_closed_at_every_object_boundary(mutate) -> None:
    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(mutate(_handoff_document())))


def test_completion_handoff_rejects_duplicate_json_keys_and_artifact_paths() -> None:
    duplicate_key_document = json.dumps(_handoff_document()).replace(
        '"status": "completed"',
        '"status": "running", "status": "completed"',
    )
    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(duplicate_key_document)

    document = _handoff_document()
    document["artifacts"] = [
        {"role": "result", "relative_path": "outputs/metrics.json"},
        {"role": "log", "relative_path": "outputs/metrics.json"},
    ]
    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_maps_deep_json_to_its_typed_decoder_error() -> None:
    deeply_nested_json = "[" * 10_000 + "0" + "]" * 10_000

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(deeply_nested_json)


def test_completion_handoff_maps_surrogate_summary_to_typed_error() -> None:
    document = _handoff_document()
    document["summary"] = "\ud800"

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_maps_surrogate_artifact_path_to_typed_error() -> None:
    document = _handoff_document()
    artifacts = list(document["artifacts"])
    artifacts[0] = {"role": "implementation", "relative_path": "\ud800"}
    document["artifacts"] = artifacts

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_validation_binds_the_expected_target_identity() -> None:
    handoff = decode_target_completion_handoff(json.dumps(_handoff_document()))

    with pytest.raises(TargetCompletionHandoffError):
        validate_target_completion_handoff(
            handoff,
            expected_target_ref="target:other",
            expected_target_run_ref="target-run:root-owned",
        )
    with pytest.raises(TargetCompletionHandoffError):
        validate_target_completion_handoff(
            handoff,
            expected_target_ref="target:root-owned",
            expected_target_run_ref="target-run:other",
        )


def test_completion_handoff_rejects_untyped_roles_with_its_typed_error() -> None:
    document = _handoff_document()
    document["artifacts"] = [
        {"role": ["result"], "relative_path": "outputs/metrics.json"}
    ]

    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))


def test_completion_handoff_bounds_final_artifacts_and_text_fields() -> None:
    document = _handoff_document()
    document["artifacts"] = [
        {"role": "result", "relative_path": "outputs/metrics.json"},
        *(
            {"role": "analysis", "relative_path": f"analysis/{index}.json"}
            for index in range(64)
        ),
    ]
    with pytest.raises(TargetCompletionHandoffError):
        decode_target_completion_handoff(json.dumps(document))

    for field, value in (
        ("target_ref", " target:root-owned"),
        ("target_run_ref", "target-run:root-owned\x00"),
        ("summary", "   "),
        ("summary", "x" * 16_385),
    ):
        document = _handoff_document()
        document[field] = value
        with pytest.raises(TargetCompletionHandoffError):
            decode_target_completion_handoff(json.dumps(document))
