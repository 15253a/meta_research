from __future__ import annotations

import json
import subprocess
from pathlib import Path

from meta_research.acquisition import (
    AcquisitionBatchRequest,
    AcquisitionPaper,
    AcquisitionPreflightRequest,
    NatureDownloaderAdapter,
)
from meta_research.provider_tools.authorized_resource_probe import (
    _authorization_expression,
)


class RecordingNatureRunner:
    def __init__(
        self,
        *,
        login_page: bool = False,
        authorized_resource: bool = True,
        oa_missing: bool = False,
        waiting_attempts_before_success: int = 0,
    ) -> None:
        self.login_page = login_page
        self.authorized_resource = authorized_resource
        self.oa_missing = oa_missing
        self.waiting_attempts_before_success = waiting_attempts_before_success
        self.batch_call_count = 0
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(
        self,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        self.calls.append((argv, cwd, environment))
        joined = " ".join(argv)
        if "authorized_resource_probe.py" in joined:
            target = "authenticated-library-target-1"
            if self.login_page:
                payload = {"ok": True, "status": "login_required", "targetId": target}
            elif self.authorized_resource:
                payload = {"ok": True, "status": "verified", "targetId": target}
            else:
                payload = {"ok": True, "status": "unverified", "targetId": None}
        elif "functional_cdp_probe.mjs" in joined:
            payload = {"ok": True}
        elif "cdp_open_url.mjs" in joined:
            payload = {
                "targetId": "authenticated-library-target-1",
                "title": "Sign in" if self.login_page else "Licensed database record",
                "url": (
                    "https://sso.example.edu/login"
                    if self.login_page
                    else "https://database.example.edu/record/1"
                ),
                "ready": "complete",
            }
        elif "configure_school.py" in joined:
            payload = {"ok": True, "school": "Example University"}
        elif "--check" in argv:
            payload = {"ok": True}
        elif "run_batch_download.py" in joined:
            self.batch_call_count += 1
            output_root = Path(argv[argv.index("--out") + 1])
            output_root.mkdir(parents=True, exist_ok=True)
            institutional = "--api-fallback-web" in argv
            missing = self.oa_missing and not institutional
            waiting = self.batch_call_count <= self.waiting_attempts_before_success
            fulltext = output_root / "PDFs" / "paper.pdf"
            if not missing and not waiting:
                fulltext.parent.mkdir(parents=True, exist_ok=True)
                fulltext.write_bytes(b"%PDF-1.4\nverified body")
            raw_result = (
                {"status": "publisher_verification_waiting_user"}
                if waiting
                else {"status": "no_authorized_pdf_found"}
                if missing
                else {
                    "status": "open_access_downloaded",
                    "file": str(fulltext),
                    "format": "pdf",
                    "sha256": "unused-by-port",
                }
            )
            payload = {
                "summary": {
                    "total": 1,
                    "downloaded": 0 if missing or waiting else 1,
                    "seconds": 0.1,
                },
                "manifest": str(output_root / "manifest.json"),
                "results": [raw_result],
            }
        else:
            payload = {"ok": True}
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(payload),
            stderr="",
        )


def _provider_root(tmp_path: Path) -> Path:
    root = tmp_path / "nature-downloader"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        "batch_download.mjs",
        "configure_school.py",
        "cdp_open_url.mjs",
        "functional_cdp_probe.mjs",
        "run_batch_download.py",
    ):
        (scripts / name).write_text("# provider seam\n", encoding="utf-8")
    (root / "SKILL.md").write_text("# Nature Downloader\n", encoding="utf-8")
    return root


def _evaluate_authorization_expression(
    *, page_text: str, institution: str, content_type: str = "text/html"
) -> dict[str, bool]:
    expression = _authorization_expression(institution)
    script = f"""
globalThis.document = {{
  body: {{innerText: {json.dumps(page_text)}}},
  title: "Scholarly article",
  contentType: {json.dumps(content_type)},
  querySelector: () => ({{}}),
}};
globalThis.location = {{href: "https://journals.example.test/article/1"}};
process.stdout.write(JSON.stringify({expression}));
"""
    completed = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_authorization_probe_rejects_an_author_affiliation_only() -> None:
    observed = _evaluate_authorization_expression(
        page_text="Authors\nAlice Researcher\nExample University\nAbstract\nResults",
        institution="Example University",
    )

    assert observed == {"login": False, "scholarly": True, "institution": False}


def test_authorization_probe_rejects_an_oa_pdf_mentioning_the_institution() -> None:
    observed = _evaluate_authorization_expression(
        page_text="Open access article by a laboratory at Example University",
        institution="Example University",
        content_type="application/pdf",
    )

    assert observed == {"login": False, "scholarly": True, "institution": False}


def test_authorization_probe_rejects_another_institutions_access_banner() -> None:
    observed = _evaluate_authorization_expression(
        page_text="Access provided by Other University",
        institution="Example University",
    )

    assert observed == {"login": False, "scholarly": True, "institution": False}


def test_authorization_probe_accepts_a_matching_institution_access_banner() -> None:
    observed = _evaluate_authorization_expression(
        page_text="Access provided by Example University",
        institution="Example University",
    )

    assert observed == {"login": False, "scholarly": True, "institution": True}


def test_nature_downloader_preflight_verifies_the_authenticated_resource(
    tmp_path: Path,
) -> None:
    runner = RecordingNatureRunner()
    adapter = NatureDownloaderAdapter(
        _provider_root(tmp_path), command_runner=runner
    )
    private_root = tmp_path / "private" / "session-1"

    result = adapter.preflight(
        AcquisitionPreflightRequest(
            session_ref="acquisition-session-1",
            initialization_id="initialization-1",
            draft_revision=2,
            config_hash="a" * 64,
            mode="oa_then_institution",
            library_entry_url="https://library.example.edu/resources",
            private_state_dir=str(private_root),
        )
    )

    assert result.status == "ready"
    assert result.browser_context_ref == "authenticated-library-target-1"
    assert result.evidence == {
        "configuration_health": "ready",
        "browser_control": "functional",
        "authorized_resource": "verified",
    }
    assert any("configure_school.py" in " ".join(call[0]) for call in runner.calls)
    assert any("functional_cdp_probe.mjs" in " ".join(call[0]) for call in runner.calls)
    assert any("authorized_resource_probe.py" in " ".join(call[0]) for call in runner.calls)
    assert not any("cdp_open_url.mjs" in " ".join(call[0]) for call in runner.calls)
    assert all(
        call[2]["LIT_DL_CONFIG_DIR"].startswith(str(private_root))
        for call in runner.calls
    )


def test_nature_downloader_preflight_keeps_login_as_waiting_user(
    tmp_path: Path,
) -> None:
    adapter = NatureDownloaderAdapter(
        _provider_root(tmp_path),
        command_runner=RecordingNatureRunner(login_page=True),
    )

    result = adapter.preflight(
        AcquisitionPreflightRequest(
            session_ref="acquisition-session-1",
            initialization_id="initialization-1",
            draft_revision=2,
            config_hash="b" * 64,
            mode="oa_then_institution",
            library_entry_url="https://library.example.edu/resources",
            private_state_dir=str(tmp_path / "private"),
        )
    )

    assert result.status == "waiting_user"
    assert result.reason_code == "institutional_login_required"
    assert result.evidence["authorized_resource"] == "login_required"


def test_nature_downloader_preflight_rejects_a_library_home_page(
    tmp_path: Path,
) -> None:
    adapter = NatureDownloaderAdapter(
        _provider_root(tmp_path),
        command_runner=RecordingNatureRunner(authorized_resource=False),
    )

    result = adapter.preflight(
        AcquisitionPreflightRequest(
            session_ref="acquisition-session-1",
            initialization_id="initialization-1",
            draft_revision=2,
            config_hash="c" * 64,
            mode="oa_then_institution",
            library_entry_url="https://library.example.edu/",
            private_state_dir=str(tmp_path / "private"),
        )
    )

    assert result.status == "unavailable"
    assert result.reason_code == "institutional_resource_unverified"
    assert result.evidence["authorized_resource"] == "not_verified"


def test_nature_downloader_executes_each_exact_oa_batch_without_public_manifest(
    tmp_path: Path,
) -> None:
    runner = RecordingNatureRunner()
    adapter = NatureDownloaderAdapter(
        _provider_root(tmp_path), command_runner=runner
    )
    target = tmp_path / "private" / "session-1" / "requests" / "acq-1"

    results = adapter.acquire(
        AcquisitionBatchRequest(
            request_id="acq-1",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="doi:10.1000/example",
                    title="Exact paper",
                    doi="10.1000/example",
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
            session_ref="acquisition-session-1",
            session_mode="oa_only",
            provider_state_dir=str(tmp_path / "private" / "session-1"),
            target_dir=str(target),
        )
    )

    assert len(results) == 1
    assert results[0].status == "obtained"
    assert results[0].format == "pdf"
    assert results[0].path is not None
    assert Path(results[0].path).is_relative_to(target)
    command = next(
        call[0]
        for call in runner.calls
        if "run_batch_download.py" in " ".join(call[0])
    )
    assert command[command.index("--title") + 1] == "Exact paper"
    assert "--open-access" in command
    assert "--no-institutional-access" in command
    assert "--no-si" in command
    assert command[command.index("--cnki-format") + 1] == "pdf"
    assert "manifest.json" not in json.dumps([result.as_dict() for result in results])


def test_nature_downloader_has_no_hidden_logical_retry_generation_ceiling(
    tmp_path: Path,
) -> None:
    runner = RecordingNatureRunner(waiting_attempts_before_success=99)
    adapter = NatureDownloaderAdapter(
        _provider_root(tmp_path), command_runner=runner
    )
    target = tmp_path / "private" / "session-1" / "requests" / "acq-long"
    request = AcquisitionBatchRequest(
        request_id="acq-long",
        route_policy="oa_first_then_institution",
        papers=(
            AcquisitionPaper(
                paper_id="provided:long-running",
                title="Long-running authorized acquisition",
                doi=None,
                arxiv_id=None,
                source_urls=("https://example.test/fulltext.pdf",),
            ),
        ),
        session_ref="acquisition-session-1",
        session_mode="provided_only",
        provider_state_dir=str(tmp_path / "private" / "session-1"),
        target_dir=str(target),
    )

    for expected_generation in range(1, 100):
        result = adapter.acquire(request)
        assert result[0].status == "waiting_user"
        assert result[0].failure == {
            "code": "publisher_verification_waiting_user",
            "detail": "该条目的授权获取需要用户在既有浏览器上下文中处理。",
        }
        assert runner.batch_call_count == expected_generation

    cursor_files = list(target.rglob("route-cursors/*.json"))
    assert len(cursor_files) == 1
    # Simulate an upgrade from the pre-cursor implementation after all 99
    # generations that old code could represent.  Migration must continue at
    # generation 100 instead of reviving the old ceiling.
    cursor_files[0].unlink()
    completed = adapter.acquire(request)

    assert completed[0].status == "obtained"
    assert completed[0].failure is None
    assert runner.batch_call_count == 100
    provider_commands = [
        call[0]
        for call in runner.calls
        if "run_batch_download.py" in " ".join(call[0])
    ]
    assert len(provider_commands) == 100
    assert len(
        {command[command.index("--out") + 1] for command in provider_commands}
    ) == 100
    assert provider_commands[-1][provider_commands[-1].index("--out") + 1].endswith(
        "provided-01-100"
    )


def test_nature_downloader_runs_oa_then_institutional_fallback(
    tmp_path: Path,
) -> None:
    runner = RecordingNatureRunner(oa_missing=True)
    adapter = NatureDownloaderAdapter(_provider_root(tmp_path), command_runner=runner)
    target = tmp_path / "private" / "session-1" / "requests" / "acq-2"

    results = adapter.acquire(
        AcquisitionBatchRequest(
            request_id="acq-2",
            route_policy="oa_first_then_institution",
            papers=(
                AcquisitionPaper(
                    paper_id="doi:10.1000/two-pass",
                    title="Exact two pass paper",
                    doi="10.1000/two-pass",
                    arxiv_id=None,
                    source_urls=(),
                ),
            ),
            session_ref="acquisition-session-1",
            session_mode="oa_then_institution",
            browser_context_ref="authenticated-library-target-1",
            provider_state_dir=str(tmp_path / "private" / "session-1"),
            target_dir=str(target),
        )
    )

    commands = [
        call[0]
        for call in runner.calls
        if "run_batch_download.py" in " ".join(call[0])
    ]
    assert results[0].status == "obtained"
    assert len(commands) == 3  # exact-title OA, DOI OA, then institutional
    assert all("--no-institutional-access" in command for command in commands[:2])
    assert "--api-fallback-web" in commands[2]
    assert "--no-institutional-access" not in commands[2]
    assert all(command[command.index("--cnki-format") + 1] == "pdf" for command in commands)
    assert len({command[command.index("--out") + 1] for command in commands}) == 3
