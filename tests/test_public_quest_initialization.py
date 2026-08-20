from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

import httpx
import pytest


def run_cli(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["meta-research", *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=20,
    )


def run_cli_json(*args: str) -> dict[str, object]:
    return json.loads(run_cli(*args).stdout)


def preview_confirmation(
    client: httpx.Client,
    write_headers: dict[str, str],
    creation: dict[str, object],
    *,
    idempotency_key: str,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/quest-initializations/"
        f"{creation['initialization_id']}/confirmation-preview",
        headers={**write_headers, "Idempotency-Key": idempotency_key},
        json={
            "quest_draft_revision": creation["quest_draft"]["revision"],
            "quest_draft_hash": creation["quest_draft"]["hash"],
            "proposal_ref": creation["proposal"]["ref"],
            "proposal_hash": creation["proposal"]["hash"],
        },
    )
    response.raise_for_status()
    return response.json()


def confirmation_payload(creation: dict[str, object]) -> dict[str, object]:
    return {
        "quest_draft_revision": creation["quest_draft"]["revision"],
        "quest_draft_hash": creation["quest_draft"]["hash"],
        "proposal_ref": creation["proposal"]["ref"],
        "proposal_hash": creation["proposal"]["hash"],
        "preview_ref": creation["confirmation_preview"]["ref"],
        "preview_hash": creation["confirmation_preview"]["hash"],
    }


def test_confirming_an_unknown_initialization_returns_typed_not_found(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    response = client.post(
        "/api/v1/quest-initializations/quest_init_missing/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-missing"},
        json={
            "quest_draft_revision": 1,
            "quest_draft_hash": "0" * 64,
            "proposal_ref": "question_proposal_missing",
            "proposal_hash": "1" * 64,
            "preview_ref": "hc_preview_missing",
            "preview_hash": "2" * 64,
        },
    )
    assert response.status_code == 404
    assert response.json()["detail"] == {"code": "quest_initialization_not_found"}


@pytest.fixture
def authenticated_product(tmp_path: Path):
    data_root = tmp_path / "quest-initialization-data"
    started = run_cli_json(
        "start",
        "--data-root",
        str(data_root),
        "--port",
        "0",
        "--json",
    )
    client = httpx.Client(
        base_url=str(started["base_url"]), timeout=5, trust_env=False
    )
    exchanged = client.post(
        "/auth/bootstrap",
        headers={"Origin": str(started["base_url"])},
        json={"token": str(started["bootstrap_token"])},
    )
    exchanged.raise_for_status()
    try:
        yield data_root, client, {
            "Origin": str(started["base_url"]),
            "X-CSRF-Token": exchanged.json()["csrf_token"],
        }
    finally:
        client.close()
        run_cli("stop", "--data-root", str(data_root), "--json", check=False)


def test_direct_first_question_reaches_distinct_owner_receipts(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    draft = {
        "goal": "判断低照度显微图像的自监督去噪能否保留稀有细胞形态。",
        "completion_criteria": "给出可复核的适用条件、反例和证据边界。",
        "key_configuration": "单卡 24 GB；优先复用公开数据；两周内形成阶段结论。",
        "literature_scope": "open_access",
        "initial_question_direction": "比较自监督去噪与监督基线对稀有形态的影响。",
        "material_receipts": [],
    }

    created_response = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-direct-quest-1"},
        json=draft,
    )
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["creation_context"] == "quest_initialization"
    assert created["route"] == "direct"
    assert created["receipts"] == {
        "human_confirmation": {"status": "not_attempted"},
        "quest_goal": {"status": "not_attempted"},
        "question_content": {"status": "not_attempted"},
        "question_identity": {"status": "not_attempted"},
        "cycle_activation": {"status": "not_attempted"},
    }
    assert "quest_ref" not in json.dumps(created)
    assert "question_ref" not in json.dumps(created)

    generated_response = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-proposal-1"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    )
    assert generated_response.status_code == 201
    generated = generated_response.json()
    proposal = generated["proposal"]
    assert proposal["status"] == "current"
    assert proposal["basis_hash"] == created["quest_draft"]["hash"]
    assert set(proposal["content"]) == {
        "title",
        "unknown_statement",
        "answer_shape",
        "applicability_scope",
        "background_context",
        "requirements_constraints",
    }
    assert all(value.strip() for value in proposal["content"].values())

    without_preview = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-without-preview"},
        json={
            "quest_draft_revision": generated["quest_draft"]["revision"],
            "quest_draft_hash": generated["quest_draft"]["hash"],
            "proposal_ref": proposal["ref"],
            "proposal_hash": proposal["hash"],
            "preview_ref": "hc_preview_not_issued",
            "preview_hash": "0" * 64,
        },
    )
    assert without_preview.status_code == 409
    assert without_preview.json()["detail"]["code"] == "confirmation_preview_required"
    rejected_view = client.get(
        f"/api/v1/quest-initializations/{created['initialization_id']}"
    ).json()
    assert rejected_view["receipts"]["human_confirmation"] == {
        "status": "rejected",
        "reason": {"code": "confirmation_preview_required"},
    }

    before_preview = client.get("/api/v1/snapshot").json()
    previewed = preview_confirmation(
        client,
        write_headers,
        generated,
        idempotency_key="preview-bundle-1",
    )
    preview = previewed["confirmation_preview"]
    assert preview["status"] == "current"
    assert preview["basis_revision"] == generated["quest_draft"]["revision"]
    assert {
        (assertion["owner"], assertion["operation"])
        for assertion in preview["target_assertions"]
    } == {
        ("research_graph", "accept_quest"),
        ("research_memory", "accept_question_content"),
        ("research_graph", "accept_root_question"),
        ("advancement_engine", "activate_initial_cycle"),
    }
    after_preview = client.get("/api/v1/snapshot").json()
    for owner in ("research_graph", "research_memory", "advancement_engine"):
        assert after_preview["owners"][owner] == before_preview["owners"][owner]

    confirmed_response = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-bundle-1"},
        json=confirmation_payload(previewed),
    )
    assert confirmed_response.status_code == 202
    confirmed = confirmed_response.json()
    assert confirmed["receipts"]["human_confirmation"]["status"] == "accepted"

    deadline = time.monotonic() + 5
    while confirmed["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.05)
        response = client.get(
            f"/api/v1/quest-initializations/{created['initialization_id']}"
        )
        response.raise_for_status()
        confirmed = response.json()

    assert confirmed["status"] == "completed"
    assert {
        name: receipt["status"]
        for name, receipt in confirmed["receipts"].items()
    } == {
        "human_confirmation": "accepted",
        "quest_goal": "accepted",
        "question_content": "accepted",
        "question_identity": "accepted",
        "cycle_activation": "accepted",
    }
    receipt_refs = [
        receipt["receipt_ref"] for receipt in confirmed["receipts"].values()
    ]
    assert len(receipt_refs) == len(set(receipt_refs))
    assert [
        (receipt["issuer"], receipt["kind"])
        for receipt in confirmed["receipts"].values()
    ] == [
        ("human_collaboration", "quest_bundle_confirmation"),
        ("research_graph", "quest_acceptance"),
        ("research_memory", "question_content_acceptance"),
        ("research_graph", "root_question_acceptance"),
        ("advancement_engine", "initial_cycle_activation"),
    ]
    assert confirmed["quest_ref"].startswith("quest_")
    assert confirmed["question_ref"].startswith("question_")
    assert confirmed["cycle_ref"].startswith("cycle_")

    snapshot = client.get("/api/v1/snapshot").json()
    assert snapshot["research_space"] == {
        "status": "active",
        "quest_count": 1,
        "question_count": 1,
        "foreground_cycle_count": 1,
    }
    assert not any(
        item["capability"] == "quest_creation"
        for item in snapshot["unavailable"]
    )


def test_production_web_exposes_the_continuous_direct_creation_window(
    authenticated_product,
) -> None:
    _data_root, client, _write_headers = authenticated_product

    shell = client.get("/")
    shell.raise_for_status()
    script_path = re.search(r'<script[^>]+src="([^"]+)"', shell.text)
    assert script_path is not None
    bundle = client.get(script_path.group(1))
    bundle.raise_for_status()

    assert "/api/v1/quest-initializations" in bundle.text
    assert "定义 Quest 与首问题" in bundle.text
    assert "生成六字段问题" in bundle.text
    assert "修改 Quest 基底" in bundle.text
    assert "按新基底明确复核原问题" in bundle.text
    assert "确定性 Impact Preview" in bundle.text
    assert "生成影响预览" in bundle.text
    assert "确认 Quest 与首问题" in bundle.text
    assert "查看并恢复当前创建" in bundle.text
    assert "创建新的 Quest" in bundle.text
    assert "CreationSeed" not in bundle.text


def test_changed_quest_basis_makes_the_old_proposal_and_confirmation_stale(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    draft = {
        "goal": "解释一种催化体系的选择性来源。",
        "completion_criteria": "形成带反例的机制边界。",
        "key_configuration": "仅使用公开数据，计算预算为单卡。",
        "literature_scope": "open_access",
        "initial_question_direction": "电子效应还是位阻主导选择性？",
        "material_receipts": [],
    }
    created = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-stale-quest"},
        json=draft,
    ).json()
    generated = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-stale-proposal"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    ).json()
    old_proposal = generated["proposal"]
    previewed = preview_confirmation(
        client,
        write_headers,
        generated,
        idempotency_key="preview-stale-bundle",
    )
    old_preview = previewed["confirmation_preview"]

    revised_response = client.put(
        f"/api/v1/quest-initializations/{created['initialization_id']}/draft",
        headers={**write_headers, "Idempotency-Key": "revise-stale-draft"},
        json={
            **draft,
            "goal": "解释并可证伪该催化体系的选择性来源。",
            "expected_draft_hash": created["quest_draft"]["hash"],
        },
    )
    revised_response.raise_for_status()
    revised = revised_response.json()
    assert revised["proposal"]["status"] == "stale"
    assert revised["proposal"]["ref"] == old_proposal["ref"]
    assert revised["confirmation_preview"]["status"] == "stale"
    assert revised["confirmation_preview"]["ref"] == old_preview["ref"]

    reverted_response = client.put(
        f"/api/v1/quest-initializations/{created['initialization_id']}/draft",
        headers={**write_headers, "Idempotency-Key": "revert-stale-draft"},
        json={
            **draft,
            "expected_draft_hash": revised["quest_draft"]["hash"],
        },
    )
    reverted_response.raise_for_status()
    reverted = reverted_response.json()
    assert reverted["quest_draft"]["hash"] == created["quest_draft"]["hash"]
    assert reverted["quest_draft"]["revision"] == 3
    assert reverted["proposal"]["status"] == "stale"
    assert reverted["confirmation_preview"]["status"] == "stale"

    stale_confirmation = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-stale-bundle"},
        json=confirmation_payload(previewed),
    )
    assert stale_confirmation.status_code == 409
    assert stale_confirmation.json()["detail"]["code"] == "quest_draft_stale"

    after_rejection = client.get(
        f"/api/v1/quest-initializations/{created['initialization_id']}"
    ).json()
    assert after_rejection["receipts"]["human_confirmation"] == {
        "status": "stale",
        "reason": {"code": "quest_draft_stale"},
    }
    assert all(
        receipt["status"] == "not_attempted"
        and receipt["reason"]["code"] == "human_confirmation_not_accepted"
        for name, receipt in after_rejection["receipts"].items()
        if name != "human_confirmation"
    )
    assert client.get("/api/v1/snapshot").json()["research_space"] == {
        "status": "empty",
        "quest_count": 0,
        "question_count": 0,
        "foreground_cycle_count": 0,
    }

    explicitly_reviewed = client.put(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "review-old-content-on-new-basis"},
        json={
            "expected_draft_hash": reverted["quest_draft"]["hash"],
            "content": old_proposal["content"],
        },
    ).json()
    assert explicitly_reviewed["proposal"]["status"] == "current"
    assert explicitly_reviewed["proposal"]["basis_revision"] == 3
    assert explicitly_reviewed["proposal"]["ref"] != old_proposal["ref"]
    assert explicitly_reviewed["receipts"]["human_confirmation"] == {
        "status": "not_attempted"
    }


def test_undelivered_material_basis_fails_typed_without_creating_domain_facts(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    response = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "material-basis-unavailable"},
        json={
            "goal": "整理用户提供的论文材料。",
            "completion_criteria": "形成可审查的问题边界。",
            "key_configuration": "仅使用已接纳材料。",
            "literature_scope": "provided_materials",
            "initial_question_direction": "材料支持哪些可检验问题？",
            "material_receipts": ["unverified-material-is-not-a-receipt"],
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "research_memory_asset_intake_not_delivered",
            "status": "capability_unavailable",
        }
    }
    snapshot = client.get("/api/v1/snapshot").json()
    assert snapshot["quest_creation"]["current"] is None
    assert snapshot["research_space"]["quest_count"] == 0
    assert snapshot["owners"]["research_memory"]["facts"]["asset_count"] == 0


def test_cancelled_draft_never_allocates_quest_or_question_identity(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    draft = {
        "goal": "建立一个可取消的研究草案。",
        "completion_criteria": "取消前不产生正式研究身份。",
        "key_configuration": "不启动外部工作。",
        "literature_scope": "open_access",
        "initial_question_direction": "该草案是否值得继续？",
        "material_receipts": [],
    }
    created = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-cancelled-draft"},
        json=draft,
    ).json()
    client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "draft-before-cancel"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    ).raise_for_status()

    cancelled_response = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/cancel",
        headers={**write_headers, "Idempotency-Key": "cancel-draft-once"},
        json={},
    )
    cancelled_response.raise_for_status()
    cancelled = cancelled_response.json()

    assert cancelled["status"] == "cancelled"
    assert "quest_ref" not in cancelled
    assert "question_ref" not in cancelled
    assert all(
        receipt["status"] == "not_attempted"
        for receipt in cancelled["receipts"].values()
    )
    assert client.get("/api/v1/snapshot").json()["research_space"] == {
        "status": "empty",
        "quest_count": 0,
        "question_count": 0,
        "foreground_cycle_count": 0,
    }


def test_proposal_can_be_regenerated_edited_and_idempotently_confirmed(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    draft = {
        "goal": "判断一种时序预测方法在哪些分布漂移下仍可靠。",
        "completion_criteria": "给出可证伪的可靠性边界。",
        "key_configuration": "公开基准；单卡；固定两周窗口。",
        "literature_scope": "open_access",
        "initial_question_direction": "比较协变量漂移与概念漂移的影响。",
        "material_receipts": [],
    }
    created = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-editable-proposal"},
        json=draft,
    ).json()
    first = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-proposal-first"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    ).json()
    regenerated = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-proposal-second"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    ).json()
    assert regenerated["proposal"]["revision"] == 2
    assert regenerated["proposal"]["ref"] != first["proposal"]["ref"]

    edited_content = {
        **regenerated["proposal"]["content"],
        "title": "分布漂移下时序预测可靠性的适用边界",
        "answer_shape": "按漂移类型给出可复核的成立条件、失效反例与不确定性。",
    }
    edit_payload = {
        "expected_draft_hash": created["quest_draft"]["hash"],
        "content": edited_content,
    }
    saved = client.put(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "save-edited-proposal"},
        json=edit_payload,
    ).json()
    assert saved["proposal"]["revision"] == 3
    assert saved["proposal"]["content"] == edited_content
    previewed = preview_confirmation(
        client,
        write_headers,
        saved,
        idempotency_key="preview-edited-proposal",
    )

    conflicting_replay = client.put(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "save-edited-proposal"},
        json={
            **edit_payload,
            "content": {**edited_content, "title": "不同的重放内容"},
        },
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["detail"]["code"] == "idempotency_conflict"

    confirmed = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-edited-proposal"},
        json=confirmation_payload(previewed),
    ).json()
    original_confirmation = confirmed["receipts"]["human_confirmation"][
        "receipt_ref"
    ]
    replayed = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-edited-proposal-retry"},
        json=confirmation_payload(previewed),
    ).json()
    assert (
        replayed["receipts"]["human_confirmation"]["receipt_ref"]
        == original_confirmation
    )


def test_daemon_restart_resumes_from_the_first_missing_owner_receipt(
    authenticated_product,
) -> None:
    data_root, client, write_headers = authenticated_product
    draft = {
        "goal": "验证跨 daemon 重启的创建恢复。",
        "completion_criteria": "恢复后只存在一个 Quest、Question 与 Cycle。",
        "key_configuration": "真实 SQLite；禁止重复领域效果。",
        "literature_scope": "open_access",
        "initial_question_direction": "分层 receipt 能否精确恢复？",
        "material_receipts": [],
    }
    created = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-restart-quest"},
        json=draft,
    ).json()
    generated = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-restart-proposal"},
        json={"expected_draft_hash": created["quest_draft"]["hash"]},
    ).json()
    previewed = preview_confirmation(
        client,
        write_headers,
        generated,
        idempotency_key="preview-before-restart",
    )
    exact_confirmation = confirmation_payload(previewed)
    confirmed = client.post(
        f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-before-restart"},
        json=exact_confirmation,
    ).json()
    assert confirmed["status"] == "dispatching"
    confirmation_ref = confirmed["receipts"]["human_confirmation"]["receipt_ref"]

    client.close()
    run_cli("stop", "--data-root", str(data_root), "--json")
    restarted = run_cli_json(
        "start", "--data-root", str(data_root), "--port", "0", "--json"
    )
    with httpx.Client(
        base_url=str(restarted["base_url"]), timeout=5, trust_env=False
    ) as resumed_client:
        exchange = resumed_client.post(
            "/auth/bootstrap",
            headers={"Origin": str(restarted["base_url"])},
            json={"token": str(restarted["bootstrap_token"])},
        )
        exchange.raise_for_status()
        resumed_headers = {
            "Origin": str(restarted["base_url"]),
            "X-CSRF-Token": exchange.json()["csrf_token"],
        }
        deadline = time.monotonic() + 5
        resumed = resumed_client.get(
            f"/api/v1/quest-initializations/{created['initialization_id']}"
        ).json()
        while resumed["status"] != "completed" and time.monotonic() < deadline:
            time.sleep(0.05)
            resumed = resumed_client.get(
                f"/api/v1/quest-initializations/{created['initialization_id']}"
            ).json()

        assert resumed["status"] == "completed"
        assert (
            resumed["receipts"]["human_confirmation"]["receipt_ref"]
            == confirmation_ref
        )
        replay = resumed_client.post(
            f"/api/v1/quest-initializations/{created['initialization_id']}/confirmation",
            headers={
                **resumed_headers,
                "Idempotency-Key": "confirm-after-lost-ack",
            },
            json=exact_confirmation,
        )
        replay.raise_for_status()
        assert replay.json()["quest_ref"] == resumed["quest_ref"]
        assert replay.json()["memory_ref"] == resumed["memory_ref"]
        assert replay.json()["question_ref"] == resumed["question_ref"]
        assert replay.json()["cycle_ref"] == resumed["cycle_ref"]
        assert resumed_client.get("/api/v1/snapshot").json()["research_space"] == {
            "status": "active",
            "quest_count": 1,
            "question_count": 1,
            "foreground_cycle_count": 1,
        }


def test_completed_initialization_leaves_the_recovery_queue_and_allows_next_quest(
    authenticated_product,
) -> None:
    _data_root, client, write_headers = authenticated_product
    first = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-first-of-two"},
        json={
            "goal": "建立第一项研究。",
            "completion_criteria": "形成第一项可复核结论。",
            "key_configuration": "公开资料。",
            "literature_scope": "open_access",
            "initial_question_direction": "第一项未知是什么？",
            "material_receipts": [],
        },
    ).json()
    generated = client.post(
        f"/api/v1/quest-initializations/{first['initialization_id']}/proposal",
        headers={**write_headers, "Idempotency-Key": "generate-first-of-two"},
        json={"expected_draft_hash": first["quest_draft"]["hash"]},
    ).json()
    previewed = preview_confirmation(
        client,
        write_headers,
        generated,
        idempotency_key="preview-first-of-two",
    )
    client.post(
        f"/api/v1/quest-initializations/{first['initialization_id']}/confirmation",
        headers={**write_headers, "Idempotency-Key": "confirm-first-of-two"},
        json=confirmation_payload(previewed),
    ).raise_for_status()

    deadline = time.monotonic() + 5
    completed = previewed
    while completed["status"] != "completed" and time.monotonic() < deadline:
        time.sleep(0.05)
        completed = client.get(
            f"/api/v1/quest-initializations/{first['initialization_id']}"
        ).json()
    assert completed["status"] == "completed"

    second_response = client.post(
        "/api/v1/quest-initializations",
        headers={**write_headers, "Idempotency-Key": "create-second-of-two"},
        json={
            "goal": "建立第二项独立研究。",
            "completion_criteria": "形成第二项可复核结论。",
            "key_configuration": "另一组公开资料。",
            "literature_scope": "open_access",
            "initial_question_direction": "第二项未知是什么？",
            "material_receipts": [],
        },
    )
    assert second_response.status_code == 201
    second = second_response.json()
    assert second["initialization_id"] != first["initialization_id"]
    assert second["status"] == "draft"
    assert client.get("/api/v1/snapshot").json()["quest_creation"]["current"][
        "initialization_id"
    ] == second["initialization_id"]
