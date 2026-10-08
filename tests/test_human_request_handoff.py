from __future__ import annotations

from pathlib import Path
from urllib.parse import quote
from html import unescape
import re
import time
from io import BytesIO

from pypdf import PdfReader
import pytest

from fastapi.testclient import TestClient

from meta_research.web import create_app
from test_root_human_request_lifecycle import (
    _bundle_root_channel,
    _initialized_child,
    _open_arguments,
    _runtime,
    _structured,
    _tool_call,
)


HELP_FACTS = {
    "background": "正在比较两种图像标注方案，需要现场专家判断边界。",
    "attempted_work": [
        {"action": "检查 12 个标注样本", "result": "3 个边界仍存在分歧。"},
        {"action": "核对已有标注指南", "result": "指南没有覆盖遮挡情形。"},
    ],
    "problem": "缺少遮挡图像的判定规则。",
    "requested_delivery": "请提供遮挡情形的判定意见及理由。",
    "impact": "规则明确后才继续比较标注一致性。",
    "safe_response": "请在此求助页面附意见文件，并填写处理说明。",
}


def _open_recorded_request(runtime, *, facts=None, key="recorded", expires_at=None, request_kind="offline_action"):
    target = {"condition": HELP_FACTS if facts is None else facts}
    return runtime.owners.research_graph.open_human_request(
        request_kind=request_kind, obligation="请帮助确定遮挡图像的标注规则",
        business_purpose="比较两种标注方案的一致性。", target_assertion=target,
        acceptance_conditions=("提供可用于 3 个争议样本的判定规则。",),
        direct_waiter={
            "waiter_ref": f"annotation-{key}", "generation": 1,
            "target_assertion": target, "wait_scope": "local", "other_blockers": [],
        },
        idempotency_key=f"open-{key}",
        expires_at=expires_at,
    )


def _authenticated_client(runtime):
    client = TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key"))
    response = client.post(
        "/auth/bootstrap", headers={"Origin": "http://testserver"},
        json={"token": runtime.authentication.issue_bootstrap_token()},
    )
    assert response.status_code == 200
    return client


def test_request_creation_and_read_preserve_actual_help_facts(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "recorded-help-facts")
    try:
        _run, channel = _bundle_root_channel(runtime)
        with TestClient(create_app(runtime, base_url="http://testserver", control_key="control-key")) as client:
            headers = _initialized_child(client, channel.connection.token)
            arguments = _open_arguments("annotator-help", "offline_action")
            arguments["condition"] = HELP_FACTS
            result = _structured(_tool_call(
                client, headers, operation_id="human_request.open",
                arguments=arguments, request_id=2,
            ))
            recorded = runtime.owners.agent_runtime.query_human_request(
                result["request_ref"]
            )
            assert recorded["target_assertion"]["condition"] == HELP_FACTS
    finally:
        runtime.close()


def test_http_handoff_shares_recorded_facts_without_changing_request(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "read-only-handoff")
    try:
        request = _open_recorded_request(runtime)
        with _authenticated_client(runtime) as client:
            url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
            response = client.get(url + "/handoff", params={"revision": 1})
            assert response.status_code == 200
            handoff = response.json()
            sections = {section["key"]: section["paragraphs"] for section in handoff["sections"]}
            assert handoff["request_ref"] == request["request_ref"]
            assert handoff["revision"] == 1
            assert handoff["is_current"] is True
            assert sections["background"][0] == HELP_FACTS["background"]
            assert sections["attempted_work"] == [
                "尝试 1：检查 12 个标注样本", "结果：3 个边界仍存在分歧。",
                "尝试 2：核对已有标注指南", "结果：指南没有覆盖遮挡情形。",
            ]
            assert sections["problem"] == [HELP_FACTS["problem"]]
            assert sections["requested_delivery"][0] == HELP_FACTS["requested_delivery"]
            assert sections["impact"] == [HELP_FACTS["impact"]]
            assert "正式提交" in "\n".join(sections["return_instructions"])
            assert client.get(url, params={"revision": 1}).json() == request
    finally:
        runtime.close()


def test_markdown_and_html_download_the_same_handoff_without_submitting(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "text-handoff-downloads")
    try:
        request = _open_recorded_request(runtime)
        with _authenticated_client(runtime) as client:
            url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
            handoff = client.get(url + "/handoff", params={"revision": 1}).json()
            expected = [handoff["title"], *[
                paragraph for section in handoff["sections"]
                for paragraph in [section["title"], *section["paragraphs"]]
            ]]
            for extension, media_type in (("md", "text/markdown"), ("html", "text/html")):
                response = client.get(url + "/handoff." + extension, params={"revision": 1})
                assert response.status_code == 200
                assert response.headers["content-type"].startswith(media_type)
                assert response.headers["content-disposition"].endswith(f'-r1.{extension}"')
                content = re.sub(r"\\([\\`*_{}\[\]<>#+.!|\-])", r"\1", response.text) if extension == "md" else unescape(re.sub(r"<[^>]*>", "", response.text))
                assert all(paragraph in content for paragraph in expected)
            assert client.get(url, params={"revision": 1}).json() == request
    finally:
        runtime.close()


def test_pdf_is_readable_and_preserves_chinese_handoff_without_submitting(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "pdf-handoff-download")
    try:
        request = _open_recorded_request(runtime)
        with _authenticated_client(runtime) as client:
            url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
            handoff = client.get(url + "/handoff", params={"revision": 1}).json()
            response = client.get(url + "/handoff.pdf", params={"revision": 1})
            assert response.status_code == 200
            assert response.headers["content-type"] == "application/pdf"
            document = PdfReader(BytesIO(response.content))
            assert document.pages
            extracted = "".join(page.extract_text() for page in document.pages)
            expected = [handoff["title"], *[
                paragraph for section in handoff["sections"]
                for paragraph in [section["title"], *section["paragraphs"]]
            ]]
            assert all(re.sub(r"\s", "", text) in re.sub(r"\s", "", extracted) for text in expected)
            (tmp_path / "handoff.pdf").write_bytes(response.content)
            assert client.get(url, params={"revision": 1}).json() == request
    finally:
        runtime.close()


def test_historical_handoff_keeps_its_facts_and_missing_attempts_explicit(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "historical-handoff")
    try:
        original = _open_recorded_request(runtime, facts={})
        target = {"condition": {**HELP_FACTS, "background": "修订后另行比较第三种标注方案。"}}
        current = runtime.owners.research_graph.revise_human_request(
            original["request_ref"], expected_revision=1, obligation=original["obligation"],
            target_assertion=target, acceptance_conditions=tuple(original["acceptance_conditions"]),
            direct_waiters=({
                "waiter_ref": "annotation-revised", "generation": 2,
                "target_assertion": target, "wait_scope": "local", "other_blockers": [],
            },), idempotency_key="revise-annotation",
        )
        with _authenticated_client(runtime) as client:
            url = f"/api/v1/human-requests/{quote(original['request_ref'], safe='')}"
            before = client.get(url, params={"revision": 1}).json()
            handoff = client.get(url + "/handoff", params={"revision": 1}).json()
            assert handoff["is_current"] is False
            paragraphs = [paragraph for section in handoff["sections"] for paragraph in section["paragraphs"]]
            assert "发起时未单独记录已尝试工作及结果；请依据原研究说明核对，不能视为已经尝试。" in paragraphs
            assert "研究目的：比较两种标注方案的一致性。" in paragraphs
            assert "这是历史请求修订，请查看当前请求后再决定正式回应。" in paragraphs
            for extension in ("md", "html", "pdf"):
                response = client.get(url + "/handoff." + extension, params={"revision": 1})
                assert response.status_code == 200
                content = "".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages) if extension == "pdf" else response.text
                assert "修订后另行比较第三种标注方案" not in content
                assert "检查 12 个标注样本" not in content
            mismatch = client.get(url + "/handoff.pdf", params={"revision": 2})
            assert mismatch.status_code == 409
            assert mismatch.json()["detail"]["code"] == "human_request_revision_conflict"
            assert client.get(url, params={"revision": 1}).json() == before
            current_url = f"/api/v1/human-requests/{quote(current['request_ref'], safe='')}"
            assert client.get(current_url, params={"revision": 2}).json() == current
    finally:
        runtime.close()


def test_export_read_does_not_materialize_expiration_or_a_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path / "expired-read-only-handoff")
    client = None
    try:
        now = time.time()
        request = _open_recorded_request(runtime, expires_at=now + 20)
        monkeypatch.setattr("meta_research.owners.human_requests.time.time", lambda: now + 30)
        # Do not start independent background workers: this test observes the
        # read/export action itself after the system clock passes the deadline.
        client = _authenticated_client(runtime)
        url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
        for path in ("", "/handoff", "/handoff.md", "/handoff.html", "/handoff.pdf"):
            assert client.get(url + path, params={"revision": 1}).status_code == 200
        assert client.get(url, params={"revision": 1}).json() == request
    finally:
        if client is not None:
            client.close()
        runtime.close()


def test_generated_high_risk_request_records_accepted_work_facts(tmp_path: Path) -> None:
    from test_public_bundle_stage import (
        _HighRiskWaitingBundleSkill, _bundle_runtime, _confirm_direct_quest,
        _finish_idea_stage, _finish_plan_stage, _open_root_target_authorization_request,
    )

    provider = _HighRiskWaitingBundleSkill()
    runtime = _bundle_runtime(tmp_path / "generated-high-risk-help", bundle_skill_provider=provider)
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        for _step in range(12):
            assert runtime.bundle_stage.process_once()
            if provider.schedule_requests:
                break
        else:
            raise AssertionError("High-risk frontier did not reach Bundle scheduling")

        dispatch = provider.schedule_requests[0]
        request = _open_root_target_authorization_request(runtime, dispatch)
        semantic_inputs = dispatch.frontier[0]["spec"]["semantic_inputs"]
        url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
        with _authenticated_client(runtime) as client:
            handoff = client.get(url + "/handoff", params={"revision": 1}).json()
            sections = {section["key"]: section["paragraphs"] for section in handoff["sections"]}
            background = "\n".join(sections["background"])
            for semantic in semantic_inputs:
                for key in ("goal", "characteristics", "boundary_constraints", "semantic_delta"):
                    assert semantic[key] in background
            assert sections["attempted_work"] == [
                "尝试 1：核对已接受计划中的实验目的、约束与执行风险。",
                "结果：当前工作被标记为高风险，执行前需要这一精确工作的单次授权。",
            ]
            assert "单次授权" in "\n".join(sections["problem"])
            assert "授予或拒绝" in sections["requested_delivery"][0]
            assert "其他" in sections["impact"][0]
            assert "请回到本求助页面确认当前修订和精确授权范围，点击“接受”提交本次授权，或选择“拒绝”“稍后处理”。" in sections["return_instructions"]
            assert not any("选择实际要交回" in paragraph for paragraph in sections["return_instructions"])
            expected = [paragraph for section in handoff["sections"] for paragraph in section["paragraphs"]]
            for extension in ("md", "html", "pdf"):
                downloaded = client.get(url + "/handoff." + extension, params={"revision": 1})
                assert downloaded.status_code == 200
                if extension == "pdf":
                    content = "".join(page.extract_text() for page in PdfReader(BytesIO(downloaded.content)).pages)
                elif extension == "html":
                    content = unescape(re.sub(r"<[^>]*>", "", downloaded.text))
                else:
                    content = re.sub(r"\\([\\`*_{}\[\]<>#+.!|\-])", r"\1", downloaded.text)
                assert all(re.sub(r"\s", "", paragraph) in re.sub(r"\s", "", content) for paragraph in expected)
            assert client.get(url, params={"revision": 1}).json() == request
    finally:
        runtime.close()


def test_system_help_handoff_returns_to_the_exact_retry_action_in_all_formats(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "system-help-return-action")
    client = None
    try:
        request = _open_recorded_request(runtime, request_kind="system_operation_help")
        client = _authenticated_client(runtime)
        url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
        handoff = client.get(url + "/handoff", params={"revision": 1}).json()
        instructions = next(section["paragraphs"] for section in handoff["sections"] if section["key"] == "return_instructions")
        retry_instruction = "请回到本求助页面确认当前修订，然后点击“重试”以恢复当前精确操作；无需填写答复表单或上传材料。"
        assert retry_instruction in instructions
        assert not any("选择实际要交回" in paragraph or "助手产物也须" in paragraph for paragraph in instructions)
        for extension in ("md", "html", "pdf"):
            response = client.get(url + "/handoff." + extension, params={"revision": 1})
            assert response.status_code == 200
            if extension == "pdf":
                text = "".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)
            elif extension == "html":
                text = unescape(re.sub(r"<[^>]*>", "", response.text))
            else:
                text = re.sub(r"\\([\\`*_{}\[\]<>#+.!|\-])", r"\1", response.text)
            assert re.sub(r"\s", "", retry_instruction) in re.sub(r"\s", "", text)
        assert client.get(url, params={"revision": 1}).json() == request
    finally:
        if client is not None:
            client.close()
        runtime.close()
