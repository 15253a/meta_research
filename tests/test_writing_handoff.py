from __future__ import annotations

from html import unescape
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import quote

from pypdf import PdfReader

from meta_research.writing_skill import WritingSkillDraft, WritingSkillRequest, WritingSkillUnavailable
from test_human_request_handoff import _authenticated_client
from test_public_writing_report import (
    _admit_report,
    _confirm_direct_quest,
    _runtime,
    _SelectivelyFailingWritingSkill,
)


def test_failed_writing_operation_handoff_explains_the_actual_report_and_attempt(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "writing-handoff", _SelectivelyFailingWritingSkill())
    try:
        quest = _confirm_direct_quest(runtime)
        admitted = _admit_report(
            runtime, str(quest["quest_ref"]), "writing-handoff", title="永久失败的报告"
        )
        assert runtime.writing.process_once()
        report = runtime.writing.query_writing_report(admitted["run"]["run_ref"])
        assert report["status"] == "blocked"
        assert report["run"]["blocker"] == {"code": "writing_test_provider_blocked"}
        requests = runtime.owners.agent_runtime.query_human_requests(
            quest_ref=str(quest["quest_ref"]), include_history=True
        )
        request = next(item for item in requests if item["kind"] == "system_operation_help")
        url = f"/api/v1/human-requests/{quote(request['request_ref'], safe='')}"
        with _authenticated_client(runtime) as client:
            response = client.get(url + "/handoff", params={"revision": 1})
            assert response.status_code == 200
            handoff = response.json()
            assert "永久失败的报告" in handoff["title"]
            sections = {
                section["key"]: "\n".join(section["paragraphs"])
                for section in handoff["sections"]
            }
            for fact in ("研究负责人", "验证控制与重启恢复", "保留精确版本历史。"):
                assert fact in sections["background"]
            assert "生成初稿" in sections["attempted_work"]
            assert "writing_test_provider_blocked" in sections["attempted_work"]
            assert "尚未保存初稿" in sections["attempted_work"]
            assert "writing_test_provider_blocked" in sections["problem"]
            assert "具体原因未记录" in sections["problem"]
            assert "重试" in sections["requested_delivery"]
            assert "永久失败的报告" in sections["impact"]
            assert "重试" in sections["return_instructions"]
            assert "正式提交" not in sections["return_instructions"]

            for extension in ("md", "html", "pdf"):
                exported = client.get(url + "/handoff." + extension, params={"revision": 1})
                assert exported.status_code == 200
                if extension == "pdf":
                    document = PdfReader(BytesIO(exported.content))
                    content = "".join(page.extract_text() for page in document.pages)
                elif extension == "html":
                    content = unescape(re.sub(r"<[^>]*>", "", exported.text))
                else:
                    content = re.sub(r"\\([\\`*_{}\[\]<>#+.!|\-])", r"\1", exported.text)
                for fact in (
                    "永久失败的报告", "验证控制与重启恢复", "保留精确版本历史。",
                    "writing_test_provider_blocked", "尚未保存初稿", "重试",
                ):
                    assert re.sub(r"\s", "", fact) in re.sub(r"\s", "", content)
            assert client.get(url, params={"revision": 1}).json() == request
    finally:
        runtime.close()


class _RetryFailureWritingSkill(_SelectivelyFailingWritingSkill):
    retry_failure_code: str | None = None

    def generate_draft(self, request: WritingSkillRequest) -> WritingSkillDraft:
        if self.retry_failure_code is not None:
            raise WritingSkillUnavailable(self.retry_failure_code)
        return super().generate_draft(request)


def test_writing_retry_handoff_records_the_latest_failed_attempt_and_keeps_history(
    tmp_path: Path,
) -> None:
    provider = _RetryFailureWritingSkill()
    runtime = _runtime(tmp_path / "writing-retry-handoff", provider)
    try:
        quest = _confirm_direct_quest(runtime)
        _admit_report(
            runtime, str(quest["quest_ref"]), "writing-retry-handoff", title="永久失败的报告"
        )
        assert runtime.writing.process_once()
        original = next(
            request for request in runtime.owners.agent_runtime.query_human_requests()
            if request["kind"] == "system_operation_help"
        )
        provider.retry_failure_code = "writing_retry_provider_blocked"
        runtime.writing.retry_system_operation_help(
            original["request_ref"], idempotency_key="writing-handoff-retry"
        )
        assert runtime.writing.process_once()
        current = next(
            request for request in runtime.owners.agent_runtime.query_human_requests()
            if request["request_id"] == original["request_id"]
        )
        assert current["revision"] == 2
        with _authenticated_client(runtime) as client:
            current_url = f"/api/v1/human-requests/{quote(current['request_ref'], safe='')}"
            current_response = client.get(current_url + "/handoff", params={"revision": 2})
            assert current_response.status_code == 200
            handoff = current_response.json()
            assert "永久失败的报告" in handoff["title"]
            sections = {
                section["key"]: "\n".join(section["paragraphs"])
                for section in handoff["sections"]
            }
            assert "writing_retry_provider_blocked" in sections["attempted_work"]
            assert "writing_retry_provider_blocked" in sections["problem"]
            assert "writing_test_provider_blocked" not in sections["problem"]
            original_url = f"/api/v1/human-requests/{quote(original['request_ref'], safe='')}"
            history = client.get(original_url + "/handoff", params={"revision": 1}).json()
            assert history["is_current"] is False
            original_problem = next(
                section["paragraphs"] for section in history["sections"]
                if section["key"] == "problem"
            )
            assert "writing_test_provider_blocked" in "\n".join(original_problem)
            assert client.get(current_url, params={"revision": 2}).json() == current
    finally:
        runtime.close()
