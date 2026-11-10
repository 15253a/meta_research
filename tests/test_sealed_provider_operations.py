import json

import pytest

from meta_research.owners.common import canonical_hash, canonical_json
from meta_research.provider_supervisor import (
    ensure_transport_key, write_exit_receipt, write_transport_envelope,
)
from meta_research.sealed_provider_operations import (
    PROVIDER_OPERATION_SCHEMA, ProviderOperationUnavailable,
    operation_transport_limits, read_business_input, read_completed_operation,
    read_sealed_invocation, verify_operation_inputs,
)


@pytest.fixture
def sealed_operation(tmp_path):
    _, key = ensure_transport_key(tmp_path)
    directory = tmp_path / "provider-operations" / "job" / "primary"
    directory.mkdir(parents=True)
    prompt = "The exact frozen research input."
    schema = {"type": "object"}
    business = {"frontier": ["target-one"], "generation": 1}
    invocation = {
        "schema_ref": PROVIDER_OPERATION_SCHEMA,
        "transport_mode": "durable_supervisor",
        "prompt_hash": canonical_hash(prompt),
        "output_schema_hash": canonical_hash(schema),
        "operation_input_hash": canonical_hash(business),
        "prompt_max_bytes": 8192, "stream_max_bytes": 8192, "result_max_bytes": 8192,
    }
    for name, content in {
        "prompt.txt": prompt,
        "output-schema.json": canonical_json(schema),
        "business-input.json": canonical_json(business),
        "stdout.jsonl": '{"type":"thread.started","thread_id":"native-one"}\n',
        "last-message.json": '{"result":"original"}',
    }.items():
        (directory / name).write_text(content, encoding="utf-8")
    write_transport_envelope(directory / "invocation.json", invocation, key)
    write_exit_receipt(
        directory / "supervisor-exit.json", key=key, invocation_hash=canonical_hash(invocation),
        prompt_path=directory / "prompt.txt", schema_path=directory / "output-schema.json",
        stdout_path=directory / "stdout.jsonl", result_path=directory / "last-message.json",
        returncode=0, input_bytes=len(prompt.encode("utf-8")),
    )
    return directory, key, invocation, business


def _read_completed(directory, invocation, native="native-one"):
    return read_completed_operation(
        directory, invocation_hash=canonical_hash(invocation), native_session_ref=native,
        transport_limits=operation_transport_limits(invocation),
    )


def test_role_independent_reader_replays_exact_signed_completion(sealed_operation):
    directory, key, invocation, business = sealed_operation
    assert read_sealed_invocation(directory / "invocation.json", key=key) == invocation
    limits = operation_transport_limits(invocation)
    assert read_business_input(directory, invocation, limits) == business
    assert verify_operation_inputs(directory, invocation_hash=canonical_hash(invocation)) == (
        invocation["prompt_hash"], invocation["output_schema_hash"], limits,
    )
    result = _read_completed(directory, invocation)
    assert result[:2] == ({"result": "original"}, "native-one")
    sealed_bytes = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert _read_completed(directory, invocation) == result
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == sealed_bytes


@pytest.mark.parametrize("artifact", [
    "invocation.json", "business-input.json", "prompt.txt", "output-schema.json",
    "stdout.jsonl", "last-message.json", "supervisor-exit.json", "completed.json",
])
def test_role_independent_reader_rejects_tampered_sealed_files(sealed_operation, artifact):
    directory, _, invocation, _ = sealed_operation
    _read_completed(directory, invocation)
    path = directory / artifact
    if artifact in {"invocation.json", "supervisor-exit.json"}:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["seal"] = "0" * 64
        path.write_text(canonical_json(envelope), encoding="utf-8")
    else:
        path.write_text('{"tampered":true}', encoding="utf-8")
    with pytest.raises(ProviderOperationUnavailable, match="codex_operation_spool_invalid"):
        _read_completed(directory, invocation)


def test_role_independent_reader_rejects_different_native_identity(sealed_operation):
    directory, _, invocation, _ = sealed_operation
    with pytest.raises(ProviderOperationUnavailable, match="codex_native_session_mismatch"):
        _read_completed(directory, invocation, "other-native")
