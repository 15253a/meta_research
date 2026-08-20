from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from meta_research.composition import build_production_runtime
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    HostComputeDevice,
    HostComputeSnapshot,
    IntentTurnRequest,
    IntentTurnResult,
    ProposalDraftRequest,
    ProposalDraftResult,
)
from meta_research.web import create_app


QUESTION = {
    "title": "低照度显微图像中的稀有形态保真",
    "unknown_statement": "尚不明确哪种自监督去噪条件能保留稀有形态。",
    "answer_shape": "形成带反例和证据边界的比较结论。",
    "applicability_scope": "低照度荧光显微公开数据。",
    "background_context": "研究稀有细胞形态。",
    "requirements_constraints": "两周内，使用获准 GPU。",
}


class DeterministicDraftingAdapter:
    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        assert request.draft["goal"]
        assert request.draft["completion_criteria"]
        return ProposalDraftResult(
            content=QUESTION,
            adapter_kind="test_deterministic",
        )

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        assert request.draft["goal"]
        return IntentTurnResult(
            reply=f"建议先把完成标准具体化：{request.message}",
            native_session_ref=request.native_session_ref or "test-native-session",
            adapter_kind="test_deterministic",
        )


class DeterministicProbe:
    def observe(self) -> HostComputeSnapshot:
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0,
            devices=(
                HostComputeDevice(
                    uuid="GPU-test-1",
                    name="Test GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="test_probe",
        )


def _run_cli(
    executable: str,
    *args: str,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _run_cli_json(
    executable: str,
    *args: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    return json.loads(_run_cli(executable, *args, env=env).stdout)


def _authenticate_http(
    base_url: str,
    bootstrap_token: str,
) -> tuple[httpx.Client, dict[str, str]]:
    client = httpx.Client(base_url=base_url, timeout=5, trust_env=False)
    exchanged = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap_token},
    )
    exchanged.raise_for_status()
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": exchanged.json()["csrf_token"],
    }


def _authenticate_test_client(runtime) -> tuple[TestClient, dict[str, str]]:
    base_url = "http://testserver"
    client = TestClient(
        create_app(runtime, base_url=base_url, control_key="control-secret"),
        base_url=base_url,
    )
    bootstrap = runtime.authentication.issue_bootstrap_token()
    exchanged = client.post(
        "/auth/bootstrap",
        headers={"Origin": base_url},
        json={"token": bootstrap},
    )
    assert exchanged.status_code == 200
    return client, {
        "Origin": base_url,
        "X-CSRF-Token": exchanged.json()["csrf_token"],
    }


def _write_headers(base: dict[str, str], idempotency_key: str) -> dict[str, str]:
    return {**base, "Idempotency-Key": idempotency_key}


def _confirmation_payload(creation: dict[str, object]) -> dict[str, object]:
    draft = creation["quest_draft"]
    proposal = creation["proposal"]
    preview = creation["confirmation_preview"]
    assert isinstance(draft, dict)
    assert isinstance(proposal, dict)
    assert isinstance(preview, dict)
    return {
        "quest_draft_revision": draft["revision"],
        "quest_draft_hash": draft["hash"],
        "proposal_ref": proposal["ref"],
        "proposal_hash": proposal["hash"],
        "preview_ref": preview["ref"],
        "preview_hash": preview["hash"],
    }


def _draft_from(creation: dict[str, object]) -> dict[str, object]:
    draft_view = creation["quest_draft"]
    assert isinstance(draft_view, dict)
    draft = dict(draft_view["value"])
    draft.update(
        {
            "goal": "判断低照度显微图像去噪能否保留稀有形态。",
            "completion_criteria": "形成带反例和证据边界的比较结论。",
            "time_budget": "30d",
            "background_and_initial_direction": "比较自监督和监督基线。",
        }
    )
    return draft


def _poll_http_view(
    client: httpx.Client,
    initialization_id: str,
    predicate,
    *,
    timeout: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    view: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        )
        response.raise_for_status()
        view = response.json()
        if predicate(view):
            return view
        time.sleep(0.05)
    raise AssertionError(f"Quest initialization did not reach the expected state: {view}")


@pytest.fixture
def providerless_installed_product(tmp_path: Path):
    executable = shutil.which("meta-research")
    assert executable is not None

    provider_path = tmp_path / "provider-path"
    provider_path.mkdir()
    nvidia_smi = provider_path / "nvidia-smi"
    nvidia_smi.write_text(
        "#!/bin/sh\n"
        "test \"$*\" = \"--query-gpu=uuid,name,memory.total "
        "--format=csv,noheader,nounits\" || exit 64\n"
        "printf '%s\\n' 'GPU-installed-test, Installed Verification GPU, 81920'\n",
        encoding="utf-8",
    )
    nvidia_smi.chmod(0o755)
    daemon_env = {**os.environ, "PATH": str(provider_path)}

    data_root = tmp_path / "installed-product-data"
    started = _run_cli_json(
        executable,
        "start",
        "--data-root",
        str(data_root),
        "--port",
        "0",
        "--json",
        env=daemon_env,
    )
    base_url = str(started["base_url"])
    unauthenticated = httpx.get(
        f"{base_url}/api/v1/snapshot",
        timeout=5,
        trust_env=False,
    )
    assert unauthenticated.status_code == 401
    client, write_headers = _authenticate_http(
        base_url,
        str(started["bootstrap_token"]),
    )
    try:
        yield executable, daemon_env, data_root, started, client, write_headers
    finally:
        client.close()
        _run_cli(
            executable,
            "stop",
            "--data-root",
            str(data_root),
            "--json",
            check=False,
            env=daemon_env,
        )


def test_installed_v2_api_exposes_typed_provider_unavailability_without_domain_facts(
    providerless_installed_product,
) -> None:
    (
        executable,
        daemon_env,
        data_root,
        started,
        client,
        write_headers,
    ) = providerless_installed_product

    snapshot = client.get("/api/v1/snapshot").json()
    assert snapshot["readiness"]["status"] == "ready"
    assert snapshot["research_space"] == {
        "status": "empty",
        "quest_count": 0,
        "question_count": 0,
        "foreground_cycle_count": 0,
    }

    shell = client.get("/")
    shell.raise_for_status()
    assert "text/html" in shell.headers["content-type"]
    assert "<script" in shell.text

    opened_response = client.post(
        "/api/v1/quest-initializations",
        headers=_write_headers(write_headers, "installed-open-v2"),
        json={},
    )
    assert opened_response.status_code == 201
    opened = opened_response.json()
    assert opened["quest_draft"]["schema_ref"].endswith("/v2")
    assert opened["intent_session"]["status"] == "open"

    probed_response = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/compute-probe",
        headers=_write_headers(write_headers, "installed-compute-probe"),
        json={"selected_device_uuids": ["GPU-installed-test"]},
    )
    probed_response.raise_for_status()
    probed = probed_response.json()
    assert probed["compute"]["status"] == "ready"
    assert probed["resource_envelope"]["selected_device_uuids"] == [
        "GPU-installed-test"
    ]

    saved_response = client.put(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
        headers=_write_headers(write_headers, "installed-autosave-v2"),
        json={
            "expected_draft_revision": probed["quest_draft"]["revision"],
            "expected_draft_hash": probed["quest_draft"]["hash"],
            "draft": _draft_from(probed),
        },
    )
    saved_response.raise_for_status()
    saved = saved_response.json()

    intent_response = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}"
        "/intent-session/messages",
        headers=_write_headers(write_headers, "installed-intent-turn"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
            "message": "怎样把问题边界缩小？",
        },
    )
    assert intent_response.status_code == 202

    generation_response = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}"
        "/proposal-generations",
        headers=_write_headers(write_headers, "installed-generate-proposal"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
        },
    )
    assert generation_response.status_code == 202

    unavailable = _poll_http_view(
        client,
        opened["initialization_id"],
        lambda view: (
            view["proposal_generation"] is not None
            and view["proposal_generation"]["status"] == "capability_unavailable"
            and view["intent_session"]["turns"]
            and view["intent_session"]["turns"][0]["assistant_status"]
            == "unavailable"
        ),
    )
    assert unavailable["proposal_generation"]["failure"] == {
        "code": "codex_cli_unavailable"
    }
    assert unavailable["intent_session"]["turns"][0]["reason"] == {
        "code": "codex_cli_unavailable"
    }
    assert unavailable["proposal"] is None
    assert unavailable["confirmation_preview"] is None
    assert all(
        receipt["status"] == "not_attempted"
        for receipt in unavailable["receipts"].values()
    )
    assert client.get("/api/v1/snapshot").json()["research_space"] == {
        "status": "empty",
        "quest_count": 0,
        "question_count": 0,
        "foreground_cycle_count": 0,
    }

    initialization_id = opened["initialization_id"]
    client.close()
    _run_cli(
        executable,
        "stop",
        "--data-root",
        str(data_root),
        "--json",
        env=daemon_env,
    )
    restarted = _run_cli_json(
        executable,
        "start",
        "--data-root",
        str(data_root),
        "--port",
        "0",
        "--json",
        env=daemon_env,
    )
    resumed_client, _resumed_headers = _authenticate_http(
        str(restarted["base_url"]),
        str(restarted["bootstrap_token"]),
    )
    try:
        restored = resumed_client.get(
            f"/api/v1/quest-initializations/{initialization_id}"
        )
        restored.raise_for_status()
        restored_view = restored.json()
        assert restored_view["proposal_generation"]["status"] == (
            "capability_unavailable"
        )
        assert restored_view["intent_session"]["turns"][0][
            "assistant_status"
        ] == "unavailable"
    finally:
        resumed_client.close()


def test_injected_async_success_auto_previews_and_recovers_owner_receipts(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    data_root_path = tmp_path / "deterministic-success"
    adapter = DeterministicDraftingAdapter()
    runtime = build_production_runtime(
        prepare_data_root(data_root_path),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    client, write_headers = _authenticate_test_client(runtime)
    request.addfinalizer(runtime.close)
    request.addfinalizer(client.close)

    opened = client.post(
        "/api/v1/quest-initializations",
        headers=_write_headers(write_headers, "success-open-v2"),
        json={},
    ).json()
    probed = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/compute-probe",
        headers=_write_headers(write_headers, "success-compute-probe"),
        json={"selected_device_uuids": ["GPU-test-1"]},
    ).json()
    saved = client.put(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/draft",
        headers=_write_headers(write_headers, "success-autosave-v2"),
        json={
            "expected_draft_revision": probed["quest_draft"]["revision"],
            "expected_draft_hash": probed["quest_draft"]["hash"],
            "draft": _draft_from(probed),
        },
    ).json()
    client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}"
        "/intent-session/messages",
        headers=_write_headers(write_headers, "success-intent-turn"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
            "message": "怎样把问题边界缩小？",
        },
    ).raise_for_status()
    queued = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}"
        "/proposal-generations",
        headers=_write_headers(write_headers, "success-generate-proposal"),
        json={
            "expected_draft_revision": saved["quest_draft"]["revision"],
            "expected_draft_hash": saved["quest_draft"]["hash"],
        },
    )
    assert queued.status_code == 202
    assert queued.json()["status"] == "proposal_generating"

    ready: dict[str, object] = {}
    for _ in range(10):
        runtime.owners.human_collaboration.process_drafting_once()
        ready = client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        if (
            ready["status"] == "proposal_ready"
            and ready["intent_session"]["turns"][0]["assistant_status"]
            == "completed"
        ):
            break
    assert ready["status"] == "proposal_ready"
    assert ready["proposal"]["content"] == QUESTION
    assert ready["proposal_generation"]["status"] == "succeeded"
    assert ready["intent_session"]["turns"][0]["assistant_status"] == "completed"
    assert "建议先把完成标准具体化" in ready["intent_session"]["turns"][0][
        "assistant_content"
    ]
    preview = ready["confirmation_preview"]
    assert preview["status"] == "current"
    assert preview["will_happen"]
    assert preview["will_not_happen"]
    assert {
        (assertion["owner"], assertion["operation"])
        for assertion in preview["target_assertions"]
    } == {
        ("research_graph", "accept_quest"),
        ("research_memory", "accept_question_content"),
        ("research_graph", "accept_root_question"),
        ("advancement_engine", "activate_initial_cycle"),
    }

    exact_confirmation = _confirmation_payload(ready)
    confirmed_response = client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/confirmation",
        headers=_write_headers(write_headers, "success-confirm-bundle"),
        json=exact_confirmation,
    )
    assert confirmed_response.status_code == 202
    confirmed = confirmed_response.json()
    assert confirmed["receipts"]["human_confirmation"]["status"] == "accepted"
    confirmation_ref = confirmed["receipts"]["human_confirmation"]["receipt_ref"]

    assert runtime.owners.human_collaboration.reconcile_once()
    assert runtime.owners.human_collaboration.reconcile_once()
    interrupted = client.get(
        f"/api/v1/quest-initializations/{opened['initialization_id']}"
    ).json()
    assert interrupted["status"] == "dispatching"
    accepted_before_restart = {
        name: receipt["receipt_ref"]
        for name, receipt in interrupted["receipts"].items()
        if receipt["status"] == "accepted"
    }
    assert set(accepted_before_restart) == {
        "human_confirmation",
        "quest_goal",
        "question_content",
    }

    client.close()
    runtime.close()

    restarted = build_production_runtime(
        prepare_data_root(data_root_path),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=DeterministicProbe(),
    )
    resumed_client, resumed_headers = _authenticate_test_client(restarted)
    request.addfinalizer(restarted.close)
    request.addfinalizer(resumed_client.close)
    completed: dict[str, object] = {}
    for _ in range(10):
        restarted.owners.human_collaboration.reconcile_once()
        completed = resumed_client.get(
            f"/api/v1/quest-initializations/{opened['initialization_id']}"
        ).json()
        if completed["status"] == "completed":
            break

    assert completed["status"] == "completed"
    assert {
        name: receipt["status"]
        for name, receipt in completed["receipts"].items()
    } == {
        "human_confirmation": "accepted",
        "quest_goal": "accepted",
        "question_content": "accepted",
        "question_identity": "accepted",
        "cycle_activation": "accepted",
    }
    assert completed["receipts"]["human_confirmation"]["receipt_ref"] == (
        confirmation_ref
    )
    for name, receipt_ref in accepted_before_restart.items():
        assert completed["receipts"][name]["receipt_ref"] == receipt_ref
    receipt_refs = [
        receipt["receipt_ref"] for receipt in completed["receipts"].values()
    ]
    assert len(receipt_refs) == len(set(receipt_refs)) == 5
    assert completed["quest_ref"].startswith("quest_")
    assert completed["question_ref"].startswith("question_")
    assert completed["cycle_ref"].startswith("cycle_")
    assert resumed_client.get("/api/v1/snapshot").json()["research_space"] == {
        "status": "active",
        "quest_count": 1,
        "question_count": 1,
        "foreground_cycle_count": 1,
    }
    assert resumed_client.get(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/intent-session"
    ).json()["intent_session"]["turns"][0]["assistant_status"] == "completed"

    replay = resumed_client.post(
        f"/api/v1/quest-initializations/{opened['initialization_id']}/confirmation",
        headers=_write_headers(resumed_headers, "success-confirm-after-restart"),
        json=exact_confirmation,
    )
    replay.raise_for_status()
    assert replay.json()["quest_ref"] == completed["quest_ref"]
    assert replay.json()["question_ref"] == completed["question_ref"]
    assert replay.json()["cycle_ref"] == completed["cycle_ref"]

    resumed_client.close()
    restarted.close()
