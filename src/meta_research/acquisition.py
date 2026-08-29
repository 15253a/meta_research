from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Literal, Protocol


ACQUISITION_RUNTIME_BINDING_SCHEMA = (
    "meta-research/nature-downloader-runtime-binding/v1"
)
ACQUISITION_ROUTE_POLICY = "oa_first_then_institution"
DEEPFETCH_PROTOTYPE_COMMIT = "cb369c938da835bcd07202e03ccc770551984070"
_CDP_PROXY = "http://127.0.0.1:3456"
_NATURE_ROUTE_CURSOR_SCHEMA = "meta-research/nature-route-cursor/v1"
_LEGACY_NATURE_ATTEMPT_GENERATIONS = 99
_ACQUISITION_REQUEST_ID_PATTERN = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"
)


class AcquisitionUnavailable(RuntimeError):
    """The lawful acquisition adapter could not reach a verified state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AcquisitionRuntimeBinding:
    provider_ref: str
    provider_version: str
    capability_bindings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_ref": ACQUISITION_RUNTIME_BINDING_SCHEMA,
            "provider_ref": self.provider_ref,
            "provider_version": self.provider_version,
            "capability_bindings": list(self.capability_bindings),
        }


@dataclass(frozen=True)
class AcquisitionPreflightRequest:
    session_ref: str
    initialization_id: str
    draft_revision: int
    config_hash: str
    mode: Literal["oa_then_institution", "oa_only", "provided_only"]
    library_entry_url: str
    private_state_dir: str
    previous_browser_context_ref: str | None = None


@dataclass(frozen=True)
class AcquisitionPreflightResult:
    status: Literal["ready", "waiting_user", "unavailable"]
    browser_context_ref: str | None
    reason_code: str | None
    evidence: dict[str, object]


@dataclass(frozen=True)
class AcquisitionPaper:
    paper_id: str
    title: str
    doi: str | None
    arxiv_id: str | None
    source_urls: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "paper_id": self.paper_id,
            "title": self.title,
            "doi": self.doi,
            "arxiv_id": self.arxiv_id,
            "source_urls": list(self.source_urls),
        }


@dataclass(frozen=True)
class AcquisitionBatchRequest:
    request_id: str
    route_policy: Literal["oa_first_then_institution"]
    papers: tuple[AcquisitionPaper, ...]
    session_ref: str = ""
    session_mode: str = ""
    browser_context_ref: str | None = None
    provider_state_dir: str = ""
    target_dir: str = ""

    def bind_to_session(
        self,
        *,
        session_ref: str,
        session_mode: str,
        browser_context_ref: str | None,
        provider_state_dir: Path,
        target_dir: Path,
    ) -> AcquisitionBatchRequest:
        return replace(
            self,
            session_ref=session_ref,
            session_mode=session_mode,
            browser_context_ref=browser_context_ref,
            provider_state_dir=str(provider_state_dir),
            target_dir=str(target_dir),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_ref": "meta-research/acquisition-batch-request/v1",
            "request_id": self.request_id,
            "route_policy": self.route_policy,
            "papers": [paper.as_dict() for paper in self.papers],
        }


@dataclass(frozen=True)
class AcquisitionItemResult:
    paper_id: str
    status: Literal["obtained", "waiting_user", "missing"]
    path: str | None
    format: Literal["pdf", "html", "xml"] | None
    failure: dict[str, str] | None
    content_sha256: str | None = None
    content_bytes: int | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "paper_id": self.paper_id,
            "status": self.status,
            "path": self.path,
            "format": self.format,
            "failure": self.failure,
        }
        if self.content_sha256 is not None or self.content_bytes is not None:
            payload["content_sha256"] = self.content_sha256
            payload["content_bytes"] = self.content_bytes
        return payload


@dataclass(frozen=True)
class AcquisitionBatchExecution:
    request_id: str
    session_ref: str
    status: Literal["obtained", "partial", "waiting_user", "missing"]
    request: AcquisitionBatchRequest
    results: tuple[AcquisitionItemResult, ...]


@dataclass(frozen=True)
class AcquisitionSession:
    session_ref: str
    initialization_id: str
    quest_ref: str | None
    status: str
    config_hash: str
    mode: str
    browser_context_ref: str | None
    runtime_binding: AcquisitionRuntimeBinding
    runtime_binding_hash: str
    preflight_generation: int
    request_count: int
    current_request_id: str | None
    slot_held: bool
    reason_code: str | None
    evidence_hash: str | None

    def as_public_dict(self, *, freshness: str = "current") -> dict[str, object]:
        return {
            "session_ref": self.session_ref,
            "status": self.status,
            "freshness": freshness,
            "mode": self.mode,
            "preflight_generation": self.preflight_generation,
            "request_count": self.request_count,
            "current_request_id": self.current_request_id,
            "slot_held": self.slot_held,
            "browser_context": (
                "verified" if self.browser_context_ref is not None else "unavailable"
            ),
            "reason": (
                None if self.reason_code is None else {"code": self.reason_code}
            ),
        }


class AcquisitionProvider(Protocol):
    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        """Return local immutable metadata without spawning or network I/O."""
        ...

    def preflight(
        self, request: AcquisitionPreflightRequest
    ) -> AcquisitionPreflightResult: ...

    def acquire(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]: ...

    def reconcile(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]: ...


NatureCommandRunner = Callable[
    [list[str], Path, dict[str, str], float],
    subprocess.CompletedProcess[str],
]


_NATURE_SUCCESS_STATUSES = {
    "downloaded",
    "downloaded_with_si",
    "open_access_downloaded",
    "full_text_html_available",
    "native_fulltext_downloaded",
}
_NATURE_WAITING_STATUSES = {
    "carsi_waiting_user",
    "carsi_resolved_retry_needed",
    "publisher_verification_waiting_user",
    "sciencedirect_robot_check",
    "retry_after_user_verification",
    "api_fallback_confirmation_required",
    "verification_auto_failed",
    "publisher_blocked_waiting_user",
}
_LOGIN_PAGE_RE = re.compile(
    r"(?:/login(?:[/?#]|$)|/authserver/|/idp/|/shibboleth|/samlsso|"
    r"/wayf(?:[/?#]|$)|/sso(?:[/?#]|$)|carsi|openathens|"
    r"\b(?:sign[ -]?in|log[ -]?in|single sign[ -]?on)\b)",
    re.IGNORECASE,
)


class NatureDownloaderAdapter:
    """Thin, typed port over the fixed Nature Downloader provider bundle.

    Provider configuration, browser state, and its manifest stay beneath the
    private AcquisitionSession directory.  Only verified per-paper files and
    credential-free status codes cross this port.
    """

    def __init__(
        self,
        provider_root: Path | None = None,
        *,
        command_runner: NatureCommandRunner | None = None,
    ) -> None:
        self._provider_root = self._resolve_provider_root(provider_root)
        self._command_runner = command_runner or _run_nature_command

    def runtime_binding(self) -> AcquisitionRuntimeBinding:
        if self._provider_root is None:
            raise AcquisitionUnavailable("nature_downloader_unavailable")
        bundle_hash = _directory_hash(self._provider_root)
        return AcquisitionRuntimeBinding(
            provider_ref="meta_research.acquisition.NatureDownloaderAdapter",
            provider_version=f"{DEEPFETCH_PROTOTYPE_COMMIT}+sha256:{bundle_hash}",
            capability_bindings=(
                "browser-context-reuse",
                "lawful-fulltext-routing",
                "private-manifest",
            ),
        )

    def preflight(
        self, request: AcquisitionPreflightRequest
    ) -> AcquisitionPreflightResult:
        paths = self._provider_paths()
        if paths is None:
            return _preflight_failure(
                "nature_downloader_unavailable",
                {"provider_bundle": "unavailable"},
            )
        (
            root,
            batch_script,
            configure_script,
            cdp_script,
            functional_probe_script,
            _,
        ) = paths
        environment = self._private_environment(request.private_state_dir)
        try:
            syntax = self._command_runner(
                ["node", "--check", str(batch_script)],
                root,
                environment,
                30.0,
            )
        except (OSError, subprocess.SubprocessError):
            return _preflight_failure(
                "nature_downloader_unavailable",
                {"provider_bundle": "not_executable"},
            )
        if syntax.returncode != 0:
            return _preflight_failure(
                "nature_downloader_unavailable",
                {"provider_bundle": "not_executable"},
            )
        if request.mode in {"oa_only", "provided_only"}:
            return AcquisitionPreflightResult(
                status="ready",
                browser_context_ref=None,
                reason_code=None,
                evidence={
                    "configuration_health": "not_required",
                    "browser_control": "not_required",
                    "authorized_resource": request.mode,
                },
            )
        if not _is_http_url(request.library_entry_url):
            return AcquisitionPreflightResult(
                status="waiting_user",
                browser_context_ref=None,
                reason_code="institutional_entry_required",
                evidence={"authorized_resource": "entry_required"},
            )
        try:
            configure = self._run_json(
                ["python3", str(configure_script), "url", request.library_entry_url],
                root,
                environment,
                60.0,
            )
            shown = self._run_json(
                ["python3", str(configure_script), "show"],
                root,
                environment,
                30.0,
            )
            health = self._run_json(
                ["python3", str(configure_script), "health", "--force"],
                root,
                environment,
                60.0,
            )
            if not all(bool(value.get("ok")) for value in (configure, shown, health)):
                return _preflight_failure(
                    "institutional_preflight_unavailable",
                    {
                        "configuration_health": "unavailable",
                        "browser_control": "unavailable",
                        "authorized_resource": "not_verified",
                    },
                )
            functional = self._run_json(
                ["node", str(functional_probe_script), "--proxy", _CDP_PROXY],
                root,
                environment,
                45.0,
            )
            if not bool(functional.get("ok")):
                return _preflight_failure(
                    "institutional_preflight_unavailable",
                    {
                        "configuration_health": "ready",
                        "browser_control": "unavailable",
                        "authorized_resource": "not_verified",
                    },
                )
            institution = _configured_institution(shown, configure)
            existing = self._authorized_resource_probe(
                root,
                environment,
                institution=institution,
            )
            if existing.get("status") == "verified":
                target_id = existing.get("targetId")
                if isinstance(target_id, str) and target_id:
                    return _verified_preflight(target_id)
            browser = self._run_json(
                [
                    "node",
                    str(cdp_script),
                    "--url",
                    request.library_entry_url,
                    "--proxy",
                    _CDP_PROXY,
                    "--wait",
                ],
                root,
                environment,
                90.0,
            )
        except (AcquisitionUnavailable, OSError, subprocess.SubprocessError):
            return _preflight_failure(
                "institutional_preflight_unavailable",
                {
                    "configuration_health": "unavailable",
                    "browser_control": "unavailable",
                    "authorized_resource": "not_verified",
                },
            )
        target_id = browser.get("targetId")
        if not isinstance(target_id, str) or not target_id.strip():
            return _preflight_failure(
                "institutional_resource_unverified",
                {
                    "configuration_health": "ready",
                    "browser_control": "functional",
                    "authorized_resource": "not_verified",
                },
            )
        try:
            observed = self._authorized_resource_probe(
                root,
                environment,
                institution=_configured_institution(shown, configure),
                target_id=target_id,
            )
        except (AcquisitionUnavailable, OSError, subprocess.SubprocessError):
            observed = {"status": "unavailable"}
        actual_url = browser.get("url")
        title = browser.get("title")
        if observed.get("status") == "login_required" or _LOGIN_PAGE_RE.search(
            f"{actual_url or ''} {title or ''}"
        ):
            return AcquisitionPreflightResult(
                status="waiting_user",
                browser_context_ref=target_id,
                reason_code="institutional_login_required",
                evidence={
                    "configuration_health": "ready",
                    "browser_control": "functional",
                    "authorized_resource": "login_required",
                },
            )
        if observed.get("status") != "verified":
            return _preflight_failure(
                "institutional_resource_unverified",
                {
                    "configuration_health": "ready",
                    "browser_control": "functional",
                    "authorized_resource": "not_verified",
                },
            )
        return _verified_preflight(target_id)

    def acquire(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]:
        return self._run_batch(request, allow_waiting_retry=True)

    def reconcile(
        self, request: AcquisitionBatchRequest
    ) -> tuple[AcquisitionItemResult, ...]:
        """Resume only work proven not to have started; never replay unknown work."""

        return self._run_batch(request, allow_waiting_retry=False)

    def _run_batch(
        self,
        request: AcquisitionBatchRequest,
        *,
        allow_waiting_retry: bool,
    ) -> tuple[AcquisitionItemResult, ...]:
        paths = self._provider_paths()
        if paths is None:
            raise AcquisitionUnavailable("nature_downloader_unavailable")
        root, _, _, _, _, launcher_script = paths
        state_root = Path(request.provider_state_dir).resolve()
        target_root = Path(request.target_dir).resolve()
        if (
            not request.session_ref
            or request.session_mode
            not in {"oa_then_institution", "oa_only", "provided_only"}
            or not state_root.is_absolute()
            or not target_root.is_relative_to(state_root)
        ):
            raise AcquisitionUnavailable("acquisition_provider_binding_invalid")
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = self._private_environment(str(state_root))
        return tuple(
            self._acquire_paper(
                root=root,
                launcher_script=launcher_script,
                environment=environment,
                request=request,
                paper=paper,
                target_root=target_root,
                allow_waiting_retry=allow_waiting_retry,
            )
            for paper in request.papers
        )

    @staticmethod
    def _resolve_provider_root(provider_root: Path | None) -> Path | None:
        # Production is intentionally pinned to the packaged fixed bundle.
        # An explicit root remains a narrow test/development seam, and its real
        # byte hash is carried in RuntimeBinding rather than claiming the fixed
        # commit alone.
        packaged = (
            Path(__file__).resolve().parent
            / "skills"
            / "deepfetch_v4"
            / "providers"
            / "nature-downloader"
        )
        candidates = [provider_root] if provider_root is not None else [packaged]
        for candidate in candidates:
            if candidate is None:
                continue
            expanded = candidate.expanduser().resolve()
            if expanded.is_dir():
                return expanded
        return None

    def _provider_paths(
        self,
    ) -> tuple[Path, Path, Path, Path, Path, Path] | None:
        if self._provider_root is None:
            return None
        root = self._provider_root
        required = (
            root / "SKILL.md",
            root / "scripts" / "batch_download.mjs",
            root / "scripts" / "configure_school.py",
            root / "scripts" / "cdp_open_url.mjs",
            root / "scripts" / "functional_cdp_probe.mjs",
            root / "scripts" / "run_batch_download.py",
        )
        if any(not path.is_file() for path in required):
            return None
        return root, required[1], required[2], required[3], required[4], required[5]

    @staticmethod
    def _private_environment(private_state_dir: str) -> dict[str, str]:
        state_root = Path(private_state_dir).expanduser().resolve()
        state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_root = state_root / "nature-downloader-config"
        config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment = dict(os.environ)
        environment["LIT_DL_CONFIG_DIR"] = str(config_root)
        return environment

    def _run_json(
        self,
        argv: list[str],
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        completed = self._command_runner(argv, cwd, environment, timeout)
        if completed.returncode != 0:
            raise AcquisitionUnavailable("nature_downloader_command_failed")
        return _decode_json_object(completed.stdout)

    def _authorized_resource_probe(
        self,
        root: Path,
        environment: dict[str, str],
        *,
        institution: str,
        target_id: str | None = None,
    ) -> dict[str, object]:
        probe = (
            Path(__file__).resolve().parent
            / "provider_tools"
            / "authorized_resource_probe.py"
        )
        if not probe.is_file():
            raise AcquisitionUnavailable("institutional_resource_probe_unavailable")
        argv = [
            sys.executable,
            str(probe),
            "--proxy",
            _CDP_PROXY,
            "--institution",
            institution,
        ]
        if target_id is not None:
            argv.extend(["--target", target_id])
        return self._run_json(argv, root, environment, 45.0)

    def _acquire_paper(
        self,
        *,
        root: Path,
        launcher_script: Path,
        environment: dict[str, str],
        request: AcquisitionBatchRequest,
        paper: AcquisitionPaper,
        target_root: Path,
        allow_waiting_retry: bool,
    ) -> AcquisitionItemResult:
        plans = self._attempt_plans(launcher_script, paper, request.session_mode)
        if not plans:
            return _missing_item(
                paper.paper_id,
                "provided_material_missing",
                "该条目没有可执行的用户提供全文地址。",
            )
        last_missing: AcquisitionItemResult | None = None
        paper_key = hashlib.sha256(paper.paper_id.encode("utf-8")).hexdigest()[:20]
        paper_root = target_root / f"paper-{paper_key}"
        paper_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for route_name, base_argv in plans:
            result = self._execute_resumable_attempt(
                root=root,
                environment=environment,
                request_id=request.request_id,
                paper=paper,
                paper_root=paper_root,
                target_root=target_root,
                route_name=route_name,
                base_argv=base_argv,
                allow_waiting_retry=allow_waiting_retry,
            )
            if result.status in {"obtained", "waiting_user"}:
                return result
            last_missing = result
        if (
            request.session_mode == "oa_only"
            and last_missing is not None
            and last_missing.failure is not None
            and last_missing.failure.get("code")
            in {
                "institutional_access_not_authorized",
                "library_no_permission",
                "no_authorized_pdf_found",
                "oa_not_found",
            }
        ):
            return _missing_item(
                paper.paper_id,
                "oa_not_found",
                "用户选择的 OA-only 路线已穷尽，未找到可验证全文。",
            )
        return last_missing or _missing_item(
            paper.paper_id,
            "acquisition_route_exhausted",
            "适用的合法全文路线已穷尽。",
        )

    def _execute_resumable_attempt(
        self,
        *,
        root: Path,
        environment: dict[str, str],
        request_id: str,
        paper: AcquisitionPaper,
        paper_root: Path,
        target_root: Path,
        route_name: str,
        base_argv: list[str],
        allow_waiting_retry: bool,
    ) -> AcquisitionItemResult:
        cursor_identity = {
            "schema_ref": _NATURE_ROUTE_CURSOR_SCHEMA,
            "request_id": request_id,
            "paper_id": paper.paper_id,
            "route": route_name,
            "route_argv_hash": canonical_hash(base_argv),
        }
        cursor_path = (
            paper_root
            / "route-cursors"
            / (canonical_hash({"route": route_name}) + ".json")
        )
        cursor = _read_private_json(cursor_path)
        if cursor is None:
            generation = self._legacy_route_generation(
                request_id=request_id,
                paper=paper,
                paper_root=paper_root,
                route_name=route_name,
                base_argv=base_argv,
                allow_waiting_retry=allow_waiting_retry,
            )
            _write_private_json(
                cursor_path, {**cursor_identity, "generation": generation}
            )
        else:
            generation = cursor.get("generation")
            if (
                any(cursor.get(key) != value for key, value in cursor_identity.items())
                or isinstance(generation, bool)
                or not isinstance(generation, int)
                or generation < 1
            ):
                return _reconciliation_item(paper.paper_id)

        attempt_root, argv, identity, state_path = self._attempt_state(
            request_id=request_id,
            paper=paper,
            paper_root=paper_root,
            route_name=route_name,
            base_argv=base_argv,
            generation=generation,
        )
        state = _read_private_json(state_path)
        if state is not None:
            if any(state.get(key) != value for key, value in identity.items()):
                return _reconciliation_item(paper.paper_id)
            if state.get("status") == "terminal":
                result = _item_result_from_private_state(state, paper.paper_id)
                if result is None:
                    return _reconciliation_item(paper.paper_id)
                if result.status != "waiting_user" or not allow_waiting_retry:
                    return result
                generation += 1
                _write_private_json(
                    cursor_path, {**cursor_identity, "generation": generation}
                )
                attempt_root, argv, identity, state_path = self._attempt_state(
                    request_id=request_id,
                    paper=paper,
                    paper_root=paper_root,
                    route_name=route_name,
                    base_argv=base_argv,
                    generation=generation,
                )
                state = _read_private_json(state_path)
                if state is not None:
                    # The cursor is written before a new provider claim.  Seeing
                    # an existing generation here means a crash/restart, not a
                    # license to skip over or duplicate that physical effect.
                    return self._reconcile_attempt_state(
                        state=state,
                        identity=identity,
                        state_path=state_path,
                        attempt_root=attempt_root,
                        paper=paper,
                        target_root=target_root,
                    )
            elif state.get("status") == "running":
                return self._reconcile_attempt_state(
                    state=state,
                    identity=identity,
                    state_path=state_path,
                    attempt_root=attempt_root,
                    paper=paper,
                    target_root=target_root,
                )
            else:
                return _reconciliation_item(paper.paper_id)

        attempt_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_private_json(state_path, {**identity, "status": "running"})
        try:
            completed = self._command_runner(argv, root, environment, 300.0)
            payload = _decode_json_object(completed.stdout)
            raw_results = payload.get("results")
            if not isinstance(raw_results, list) or len(raw_results) != 1:
                raise AcquisitionUnavailable("nature_downloader_result_invalid")
            result = _map_nature_result(paper.paper_id, raw_results[0], target_root)
        except (AcquisitionUnavailable, OSError, subprocess.SubprocessError):
            # A launched operation without a trustworthy terminal envelope
            # remains unknown.  Reconciliation may consume its durable
            # provider manifest later, but this call never guesses/replays.
            return _reconciliation_item(paper.paper_id)
        _write_private_json(
            state_path,
            {**identity, "status": "terminal", "result": result.as_dict()},
        )
        return result

    @staticmethod
    def _attempt_state(
        *,
        request_id: str,
        paper: AcquisitionPaper,
        paper_root: Path,
        route_name: str,
        base_argv: list[str],
        generation: int,
    ) -> tuple[Path, list[str], dict[str, object], Path]:
        attempt_root = paper_root / "attempts" / f"{route_name}-{generation:02d}"
        argv = [*base_argv, "--out", str(attempt_root)]
        identity: dict[str, object] = {
            "schema_ref": "meta-research/nature-attempt/v1",
            "request_id": request_id,
            "paper_id": paper.paper_id,
            "route": route_name,
            "generation": generation,
            "argv_hash": canonical_hash(argv),
        }
        return attempt_root, argv, identity, attempt_root / "operation.json"

    @classmethod
    def _legacy_route_generation(
        cls,
        *,
        request_id: str,
        paper: AcquisitionPaper,
        paper_root: Path,
        route_name: str,
        base_argv: list[str],
        allow_waiting_retry: bool,
    ) -> int:
        """Select a pre-cursor generation with one fixed compatibility scan."""

        for generation in range(1, _LEGACY_NATURE_ATTEMPT_GENERATIONS + 1):
            _root, _argv, identity, state_path = cls._attempt_state(
                request_id=request_id,
                paper=paper,
                paper_root=paper_root,
                route_name=route_name,
                base_argv=base_argv,
                generation=generation,
            )
            state = _read_private_json(state_path)
            if state is None:
                return generation
            if any(state.get(key) != value for key, value in identity.items()):
                return generation
            if state.get("status") != "terminal":
                return generation
            result = _item_result_from_private_state(state, paper.paper_id)
            if (
                result is None
                or result.status != "waiting_user"
                or not allow_waiting_retry
            ):
                return generation
        return _LEGACY_NATURE_ATTEMPT_GENERATIONS + 1

    @staticmethod
    def _reconcile_attempt_state(
        *,
        state: dict[str, object],
        identity: dict[str, object],
        state_path: Path,
        attempt_root: Path,
        paper: AcquisitionPaper,
        target_root: Path,
    ) -> AcquisitionItemResult:
        if any(state.get(key) != value for key, value in identity.items()):
            return _reconciliation_item(paper.paper_id)
        if state.get("status") == "terminal":
            result = _item_result_from_private_state(state, paper.paper_id)
            return result or _reconciliation_item(paper.paper_id)
        if state.get("status") != "running":
            return _reconciliation_item(paper.paper_id)
        reconciled = _result_from_manifest(paper.paper_id, attempt_root, target_root)
        if reconciled is None:
            return _reconciliation_item(paper.paper_id)
        _write_private_json(
            state_path,
            {**identity, "status": "terminal", "result": reconciled.as_dict()},
        )
        return reconciled

    @staticmethod
    def _attempt_plans(
        launcher_script: Path,
        paper: AcquisitionPaper,
        session_mode: str,
    ) -> list[tuple[str, list[str]]]:
        prefix = [sys.executable, str(launcher_script)]
        suffix = ["--no-si", "--cnki-format", "pdf"]
        sources = [value for value in paper.source_urls if _is_http_url(value)]
        plans: list[tuple[str, list[str]]] = []
        if session_mode == "provided_only":
            return [
                (
                    f"provided-{index:02d}",
                    [
                        *prefix,
                        "--pdf-url",
                        source,
                        "--title",
                        paper.title,
                        "--no-institutional-access",
                        *suffix,
                    ],
                )
                for index, source in enumerate(sources, 1)
            ]

        if _is_chinese_paper(paper):
            chinese = [*prefix, "--title", paper.title]
            if sources:
                chinese.extend(["--source-url", sources[0]])
            if session_mode == "oa_only":
                chinese.append("--no-institutional-access")
            plans.append(("cnki", [*chinese, *suffix]))
            return plans

        for index, source in enumerate(sources, 1):
            if _looks_like_direct_body(source):
                args = [*prefix, "--pdf-url", source, "--title", paper.title]
            else:
                args = [
                    *prefix,
                    "--title",
                    paper.title,
                    "--source-url",
                    source,
                    "--open-access",
                ]
            plans.append(
                (
                    f"oa-source-{index:02d}",
                    [*args, "--no-institutional-access", *suffix],
                )
            )
        if paper.arxiv_id:
            arxiv_id = paper.arxiv_id.removeprefix("arXiv:")
            plans.append(
                (
                    "oa-arxiv",
                    [
                        *prefix,
                        "--pdf-url",
                        f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                        "--title",
                        paper.title,
                        "--no-institutional-access",
                        *suffix,
                    ],
                )
            )
        plans.append(
            (
                "oa-title",
                [
                    *prefix,
                    "--title",
                    paper.title,
                    "--open-access",
                    "--no-institutional-access",
                    *suffix,
                ],
            )
        )
        if paper.doi:
            plans.append(
                (
                    "oa-doi",
                    [
                        *prefix,
                        "--dois",
                        paper.doi,
                        "--open-access",
                        "--no-institutional-access",
                        *suffix,
                    ],
                )
            )
        if session_mode == "oa_then_institution":
            institutional = [*prefix]
            if paper.doi:
                institutional.extend(["--dois", paper.doi])
            else:
                institutional.extend(["--title", paper.title])
            plans.append(
                (
                    "institutional",
                    [*institutional, "--api-fallback-web", *suffix],
                )
            )
        return plans


def _run_nature_command(
    argv: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _preflight_failure(
    reason_code: str,
    evidence: dict[str, object],
) -> AcquisitionPreflightResult:
    return AcquisitionPreflightResult(
        status="unavailable",
        browser_context_ref=None,
        reason_code=reason_code,
        evidence=evidence,
    )


def _verified_preflight(target_id: str) -> AcquisitionPreflightResult:
    return AcquisitionPreflightResult(
        status="ready",
        browser_context_ref=target_id,
        reason_code=None,
        evidence={
            "configuration_health": "ready",
            "browser_control": "functional",
            "authorized_resource": "verified",
        },
    )


def _configured_institution(
    shown: dict[str, object], configured: dict[str, object]
) -> str:
    config = shown.get("config")
    if isinstance(config, dict):
        school = config.get("school")
        if isinstance(school, dict) and isinstance(school.get("name"), str):
            return str(school["name"])
    value = configured.get("school")
    return value if isinstance(value, str) else ""


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    try:
        paths = sorted(
            (
                path
                for path in root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            ),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if not paths:
            raise OSError("empty provider bundle")
        for path in paths:
            relative = path.relative_to(root).as_posix().encode("utf-8")
            content = path.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    except (OSError, ValueError) as error:
        raise AcquisitionUnavailable("nature_downloader_unavailable") from error
    return digest.hexdigest()


def _write_private_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = canonical_json(value).encode("utf-8")
    descriptor = -1
    temporary = ""
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=".operation.", dir=path.parent)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as destination:
            descriptor = -1
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
        temporary = ""
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as error:
        raise AcquisitionUnavailable("acquisition_provider_spool_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _read_private_json(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "invalid"}
    return decoded if isinstance(decoded, dict) else {"status": "invalid"}


def _item_result_from_private_state(
    state: dict[str, object], paper_id: str
) -> AcquisitionItemResult | None:
    value = state.get("result")
    if not isinstance(value, dict) or value.get("paper_id") != paper_id:
        return None
    try:
        result = AcquisitionItemResult(
            paper_id=str(value["paper_id"]),
            status=value["status"],  # type: ignore[arg-type]
            path=value["path"],  # type: ignore[arg-type]
            format=value["format"],  # type: ignore[arg-type]
            failure=value["failure"],  # type: ignore[arg-type]
            content_sha256=value.get("content_sha256"),  # type: ignore[arg-type]
            content_bytes=value.get("content_bytes"),  # type: ignore[arg-type]
        )
        validate_item_results(
            AcquisitionBatchRequest(
                request_id="private-state-validation",
                route_policy=ACQUISITION_ROUTE_POLICY,
                papers=(
                    AcquisitionPaper(
                        paper_id=paper_id,
                        title="private state",
                        doi=None,
                        arxiv_id=None,
                        source_urls=(),
                    ),
                ),
            ),
            (result,),
        )
        return result
    except (KeyError, TypeError, AcquisitionUnavailable):
        return None


def _result_from_manifest(
    paper_id: str, attempt_root: Path, target_root: Path
) -> AcquisitionItemResult | None:
    manifest = attempt_root / "manifest.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, list) or len(raw_results) != 1:
        return None
    return _map_nature_result(paper_id, raw_results[0], target_root)


def _reconciliation_item(paper_id: str) -> AcquisitionItemResult:
    return _missing_item(
        paper_id,
        "acquisition_reconciliation_required",
        "该条目的既有下载结果尚不能安全对账；系统不会重复启动下载。",
        waiting_user=True,
    )


def _is_chinese_paper(paper: AcquisitionPaper) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", paper.title)) or any(
        "cnki." in value.casefold() for value in paper.source_urls
    )


def _looks_like_direct_body(url: str) -> bool:
    normalized = url.casefold().split("?", 1)[0]
    return normalized.endswith((".pdf", ".html", ".htm", ".xml")) or any(
        host in normalized for host in ("arxiv.org/pdf/", "pmc.ncbi.nlm.nih.gov/articles/")
    )


def _decode_json_object(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise AcquisitionUnavailable("nature_downloader_output_invalid") from error
    if not isinstance(payload, dict):
        raise AcquisitionUnavailable("nature_downloader_output_invalid")
    return payload


def _is_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^https?://[^\s]+$", value.strip(), re.IGNORECASE))


def _missing_item(
    paper_id: str,
    code: str,
    detail: str,
    *,
    waiting_user: bool = False,
) -> AcquisitionItemResult:
    return AcquisitionItemResult(
        paper_id=paper_id,
        status="waiting_user" if waiting_user else "missing",
        path=None,
        format=None,
        failure={"code": code, "detail": detail},
    )


def _map_nature_result(
    paper_id: str,
    raw_result: object,
    target_root: Path,
) -> AcquisitionItemResult:
    if not isinstance(raw_result, dict):
        return _missing_item(
            paper_id,
            "nature_downloader_result_invalid",
            "Nature Downloader 返回的逐篇结果无效。",
        )
    raw_status = raw_result.get("status")
    status = raw_status if isinstance(raw_status, str) else "unknown"
    if status in _NATURE_SUCCESS_STATUSES:
        raw_path = raw_result.get("file")
        if not isinstance(raw_path, str) or not raw_path:
            return _missing_item(
                paper_id,
                "nature_downloader_file_unverified",
                "Nature Downloader 未返回可验证的全文文件。",
            )
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = target_root / candidate
        candidate = candidate.resolve()
        if (
            not candidate.is_relative_to(target_root)
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            return _missing_item(
                paper_id,
                "nature_downloader_file_unverified",
                "Nature Downloader 返回的全文文件未通过私有目录校验。",
            )
        raw_format = raw_result.get("format")
        normalized_format = (
            raw_format.lower() if isinstance(raw_format, str) else ""
        )
        if normalized_format in {"jats", "jats_xml"}:
            normalized_format = "xml"
        if normalized_format not in {"pdf", "html", "xml"}:
            suffix = candidate.suffix.lower()
            normalized_format = {
                ".pdf": "pdf",
                ".html": "html",
                ".htm": "html",
                ".xml": "xml",
            }.get(suffix, "")
        if normalized_format not in {"pdf", "html", "xml"}:
            return _missing_item(
                paper_id,
                "nature_downloader_format_unsupported",
                "取得的全文格式不在 DeepFetch 允许范围内。",
            )
        return AcquisitionItemResult(
            paper_id=paper_id,
            status="obtained",
            path=str(candidate),
            format=normalized_format,  # type: ignore[arg-type]
            failure=None,
        )
    if status in _NATURE_WAITING_STATUSES:
        return _missing_item(
            paper_id,
            status,
            "该条目的授权获取需要用户在既有浏览器上下文中处理。",
            waiting_user=True,
        )
    return _missing_item(
        paper_id,
        status if re.fullmatch(r"[a-z0-9_]{1,80}", status) else "acquisition_missing",
        "Nature Downloader 未取得该条目的可用全文。",
    )


def canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_runtime_binding(binding: AcquisitionRuntimeBinding) -> str:
    if (
        not binding.provider_ref
        or not binding.provider_version
        or any(not capability for capability in binding.capability_bindings)
        or len(set(binding.capability_bindings)) != len(binding.capability_bindings)
        or not {
            "browser-context-reuse",
            "lawful-fulltext-routing",
            "private-manifest",
        }.issubset(binding.capability_bindings)
    ):
        raise AcquisitionUnavailable("acquisition_runtime_binding_invalid")
    return canonical_hash(binding.as_dict())


def validate_preflight_result(
    request: AcquisitionPreflightRequest,
    result: AcquisitionPreflightResult,
) -> AcquisitionPreflightResult:
    if result.status not in {"ready", "waiting_user", "unavailable"}:
        raise AcquisitionUnavailable("acquisition_preflight_result_invalid")
    if not isinstance(result.evidence, dict):
        raise AcquisitionUnavailable("acquisition_preflight_result_invalid")
    if result.status == "ready":
        if result.reason_code is not None:
            raise AcquisitionUnavailable("acquisition_preflight_result_invalid")
        if request.mode == "oa_then_institution" and not result.browser_context_ref:
            raise AcquisitionUnavailable("institutional_browser_context_unverified")
    elif not result.reason_code:
        raise AcquisitionUnavailable("acquisition_preflight_result_invalid")
    if result.browser_context_ref is not None and (
        not result.browser_context_ref.strip()
        or len(result.browser_context_ref) > 512
    ):
        raise AcquisitionUnavailable("acquisition_preflight_result_invalid")
    return result


def validate_batch_request(request: AcquisitionBatchRequest) -> str:
    if (
        not isinstance(request.request_id, str)
        or _ACQUISITION_REQUEST_ID_PATTERN.fullmatch(request.request_id) is None
        or request.route_policy != ACQUISITION_ROUTE_POLICY
        or not request.papers
        or len(request.papers) > 10
    ):
        raise AcquisitionUnavailable("acquisition_batch_request_invalid")
    paper_ids: set[str] = set()
    for paper in request.papers:
        if (
            not paper.paper_id
            or paper.paper_id in paper_ids
            or not paper.title.strip()
            or len(paper.paper_id) > 512
            or len(paper.title) > 2_000
        ):
            raise AcquisitionUnavailable("acquisition_batch_request_invalid")
        paper_ids.add(paper.paper_id)
    return canonical_hash(request.identity_payload())


def validate_item_results(
    request: AcquisitionBatchRequest,
    results: tuple[AcquisitionItemResult, ...],
) -> tuple[AcquisitionItemResult, ...]:
    expected = [paper.paper_id for paper in request.papers]
    if [result.paper_id for result in results] != expected:
        raise AcquisitionUnavailable("acquisition_result_identity_mismatch")
    for result in results:
        if result.status == "obtained":
            proof_absent = (
                result.content_sha256 is None and result.content_bytes is None
            )
            proof_valid = (
                isinstance(result.content_sha256, str)
                and len(result.content_sha256) == 64
                and all(
                    character in "0123456789abcdef"
                    for character in result.content_sha256
                )
                and isinstance(result.content_bytes, int)
                and not isinstance(result.content_bytes, bool)
                and result.content_bytes > 0
            )
            if (
                result.path is None
                or result.format not in {"pdf", "html", "xml"}
                or result.failure is not None
                or not (proof_absent or proof_valid)
            ):
                raise AcquisitionUnavailable("acquisition_result_invalid")
        elif result.status in {"waiting_user", "missing"}:
            if (
                result.path is not None
                or result.format is not None
                or result.content_sha256 is not None
                or result.content_bytes is not None
                or not isinstance(result.failure, dict)
                or set(result.failure) != {"code", "detail"}
                or not all(
                    isinstance(value, str) and value.strip()
                    for value in result.failure.values()
                )
            ):
                raise AcquisitionUnavailable("acquisition_result_invalid")
        else:
            raise AcquisitionUnavailable("acquisition_result_invalid")
    return results


def freeze_acquisition_item_artifacts(
    request: AcquisitionBatchRequest,
    results: tuple[AcquisitionItemResult, ...],
) -> tuple[AcquisitionItemResult, ...]:
    """Freeze Owner-observed artifact bytes before terminal result persistence."""

    validate_item_results(request, results)
    if not request.target_dir or not Path(request.target_dir).is_absolute():
        raise AcquisitionUnavailable("acquisition_provider_binding_invalid")
    target_path = Path(request.target_dir)
    try:
        target_root = target_path.resolve(strict=True)
        if (
            target_path.is_symlink()
            or not target_path.is_dir()
            or target_root != target_path
        ):
            raise OSError("unsafe acquisition target root")
    except (OSError, ValueError) as error:
        raise AcquisitionUnavailable("acquisition_artifact_invalid") from error
    frozen: list[AcquisitionItemResult] = []
    for result in results:
        if result.status != "obtained":
            frozen.append(result)
            continue
        assert result.path is not None
        path = Path(result.path)
        try:
            resolved = path.resolve(strict=True)
            if (
                not path.is_absolute()
                or str(resolved) != result.path
                or path.is_symlink()
                or not path.is_file()
                or not resolved.is_relative_to(target_root)
            ):
                raise OSError("unsafe acquisition artifact")
            byte_count = path.stat().st_size
            if byte_count < 1 or byte_count > 32 * 1024 * 1024:
                raise OSError("acquisition artifact size invalid")
            with path.open("rb") as source:
                content = source.read(32 * 1024 * 1024 + 1)
            if len(content) != byte_count:
                raise OSError("acquisition artifact changed while freezing")
        except (OSError, ValueError) as error:
            raise AcquisitionUnavailable("acquisition_artifact_invalid") from error
        content_sha256 = hashlib.sha256(content).hexdigest()
        if result.content_sha256 is not None:
            if (
                result.content_sha256 != content_sha256
                or result.content_bytes != byte_count
            ):
                raise AcquisitionUnavailable("acquisition_artifact_drift")
            frozen.append(result)
        else:
            frozen.append(
                replace(
                    result,
                    content_sha256=content_sha256,
                    content_bytes=byte_count,
                )
            )
    frozen_results = tuple(frozen)
    validate_item_results(request, frozen_results)
    return frozen_results


def aggregate_batch_status(
    results: tuple[AcquisitionItemResult, ...],
) -> Literal["obtained", "partial", "waiting_user", "missing"]:
    statuses = {result.status for result in results}
    if "waiting_user" in statuses:
        return "waiting_user"
    if statuses == {"obtained"}:
        return "obtained"
    if "obtained" in statuses:
        return "partial"
    return "missing"
