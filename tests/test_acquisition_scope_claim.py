from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from meta_research.acquisition import AcquisitionBatchRequest, AcquisitionPaper
from meta_research.composition import build_production_runtime
from meta_research.owners.common import OwnerConflict
from meta_research.paths import prepare_data_root
from test_public_acquisition_session import (
    NoCompute,
    RepeatedPendingAcquisitionProvider,
    _authenticated_client,
    _open_ready_acquisition,
)


class _ChangingBindingProvider(RepeatedPendingAcquisitionProvider):
    timeout = 600

    def runtime_binding(self):
        binding = super().runtime_binding()
        return replace(binding, provider_version=f"bounded-test-{self.timeout}")


def _request() -> AcquisitionBatchRequest:
    return AcquisitionBatchRequest(
        request_id="scope-claim-batch", route_policy="oa_first_then_institution",
        papers=(AcquisitionPaper(
            paper_id="paper:scope-claim", title="Preserve the existing unknown operation",
            doi=None, arxiv_id=None, source_urls=(),
        ),),
    )


def test_rejected_binding_does_not_consume_the_next_acquisition_attempt(
    tmp_path: Path,
) -> None:
    provider = _ChangingBindingProvider(pending_reconciliations=0)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path), acquisition_provider=provider,
        host_compute_probe=NoCompute(), startup_harness_diagnostics=False,
    )
    client, headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        initialization, saved, session_ref = _open_ready_acquisition(
            client, headers, prefix="scope-claim",
        )
        request = _request()
        assert owner.acquire_literature(session_ref, request, provider).status == "waiting_user"

        def prepare():
            return owner.prepare_acquisition_session(
                initialization_id=initialization,
                draft_revision=saved["quest_draft"]["revision"],
                config={"mode": "oa_then_institution",
                        "library_entry_url": "https://library.example.edu/resources"},
                provider=provider,
            )

        provider.timeout = 1800
        assert prepare().status == "ready"
        before_session = owner.query_acquisition_session(session_ref=session_ref)
        before_request = owner.query_acquisition_request(session_ref, request.request_id)
        before_control = owner.query_managed_run(request.request_id)
        with pytest.raises(OwnerConflict, match="external_root_task_scope_conflict"):
            owner.acquire_literature(session_ref, request, provider)
        assert owner.query_acquisition_session(session_ref=session_ref) == before_session
        assert owner.query_acquisition_request(session_ref, request.request_id) == before_request
        assert owner.query_managed_run(request.request_id) == before_control
        assert provider.acquire_calls == 1 and provider.reconcile_calls == []

        provider.timeout = 600
        assert prepare().status == "ready"
        assert owner.acquire_literature(session_ref, request, provider).status == "missing"
        assert provider.acquire_calls == 1
        assert len(provider.reconcile_calls) == 1
        assert provider.reconcile_calls[0].root_runtime_scope["generation"] == 2
    finally:
        client.close()
        runtime.close()


def test_failed_scope_registration_rolls_back_the_initial_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = RepeatedPendingAcquisitionProvider(pending_reconciliations=0)
    runtime = build_production_runtime(
        prepare_data_root(tmp_path), acquisition_provider=provider,
        host_compute_probe=NoCompute(), startup_harness_diagnostics=False,
    )
    client, headers = _authenticated_client(runtime)
    owner = runtime.owners.agent_runtime
    try:
        _, _, session_ref = _open_ready_acquisition(client, headers, prefix="scope-fault")
        request = _request()
        before_session = owner.query_acquisition_session(session_ref=session_ref)
        record = owner._feed.record

        def fail_registration(connection, event, *args, **kwargs):
            record(connection, event, *args, **kwargs)
            if event == "agent_runtime.external_root_task_registered":
                raise RuntimeError("scope registration interrupted")

        with monkeypatch.context() as scoped:
            scoped.setattr(owner._feed, "record", fail_registration)
            with pytest.raises(RuntimeError, match="scope registration interrupted"):
                owner.acquire_literature(session_ref, request, provider)
        assert owner.query_acquisition_session(session_ref=session_ref) == before_session
        assert owner.query_acquisition_request(session_ref, request.request_id) is None
        with owner._database.read() as connection:
            assert connection.execute(text(
                "SELECT count(*) FROM ar_run_controls WHERE run_ref = :ref"
            ), {"ref": request.request_id}).scalar_one() == 0
        assert provider.acquire_calls == 0
        assert owner.acquire_literature(session_ref, request, provider).status == "waiting_user"
        assert owner.query_managed_run(request.request_id)["attempt_generation"] == 1
    finally:
        client.close()
        runtime.close()
