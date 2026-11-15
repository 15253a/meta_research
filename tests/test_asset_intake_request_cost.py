"""Admission reuses its parsed request; durable processing validates its own input."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict, canonical_hash
import meta_research.owners.research_memory as memory_module
from meta_research.owners.research_memory import AssetIntakeRequest
from meta_research.paths import prepare_data_root


@pytest.mark.parametrize("asynchronous", [False, True])
def test_intake_normalizes_once_per_admission_and_durable_processing(
    tmp_path, monkeypatch, asynchronous,
):
    runtime = build_production_runtime(prepare_data_root(tmp_path / "intake-request"))
    memory = runtime.owners.research_memory
    request = AssetIntakeRequest(
        source_kind="text", custody_mode="managed", display_name="result.txt",
        content=b"Preserve these exact research bytes.\n",
        provenance={"origin": "research-output"}, asynchronous=asynchronous,
    )
    expected_hash = canonical_hash(memory_module._asset_request_document(request))
    normalize = Mock(wraps=memory_module._asset_request_document)
    monkeypatch.setattr(memory_module, "_asset_request_document", normalize)
    try:
        result = memory.submit_asset_intake(request, idempotency_key="exact-request")
        assert normalize.call_count == (1 if asynchronous else 2)
        if asynchronous:
            assert result.status == "queued"
            assert memory.process_asset_intake_once()
            result = memory.query_asset_intake(result.job_ref)
        # Processing the durable command has its own required validation.
        assert normalize.call_count == 2
        assert result.status == "accepted" and result.asset is not None
        assert memory.materialize_asset(result.asset.version_ref).content == request.content
        with runtime._database.read() as connection:
            stored_hash = connection.execute(text(
                "SELECT request_hash FROM rm_asset_intakes WHERE job_ref=:ref"
            ), {"ref": result.job_ref}).scalar_one()
        assert stored_hash == expected_hash

        assert memory.submit_asset_intake(request, idempotency_key="exact-request") == result
        assert normalize.call_count == 3
        with pytest.raises(OwnerConflict, match="asset_intake_idempotency_conflict"):
            memory.submit_asset_intake(
                replace(request, content=b"changed bytes"), idempotency_key="exact-request",
            )
        assert len(memory.query_asset_inventory()) == 1
    finally:
        runtime.close()
