from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time
from pathlib import Path

import uvicorn

import meta_research.web as web_module
from meta_research.composition import build_production_runtime
from meta_research.deepfetch import (
    DeepFetchProviderRequest,
    DeepFetchResult,
    DeepFetchRuntimeBinding,
)
from meta_research.paths import prepare_data_root
from meta_research.quest_drafting import (
    DraftingUnavailable,
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
    def __init__(self, intent_started: threading.Event) -> None:
        self._failed_proposal_initializations: set[str] = set()
        self._lock = threading.Lock()
        self._intent_started = intent_started

    def draft(self, request: ProposalDraftRequest) -> ProposalDraftResult:
        assert request.draft["goal"]
        assert request.draft["completion_criteria"]
        with self._lock:
            should_fail = (
                "FAIL FIRST PROPOSAL" in str(request.draft["goal"])
                and request.initialization_id
                not in self._failed_proposal_initializations
            )
            if should_fail:
                self._failed_proposal_initializations.add(request.initialization_id)
        if should_fail:
            time.sleep(0.35)
            raise DraftingUnavailable("deterministic_proposal_failed")
        # Keep the durable queued/running state observable across a real browser
        # close/reopen without exposing a test-only HTTP control surface.
        time.sleep(2.0)
        content = dict(QUESTION)
        if request.literature_snapshot is not None:
            content["background_context"] = (
                "DeepFetch 已核查两篇论文；一篇没有可合法获取的开放全文。"
            )
        return ProposalDraftResult(
            content=content,
            adapter_kind="chrome_deterministic",
        )

    def reply(self, request: IntentTurnRequest) -> IntentTurnResult:
        self._intent_started.set()
        time.sleep(1.2 if "并行" in request.message else 0.25)
        if "typed unavailable" in request.message:
            raise DraftingUnavailable("deterministic_intent_unavailable")
        return IntentTurnResult(
            reply=f"建议先固定可证伪边界：{request.message}",
            native_session_ref=request.native_session_ref or "chrome-intent-session",
            adapter_kind="chrome_deterministic",
        )


class SequencedHostProbe:
    """The first observation is unavailable; later observations are real typed rows."""

    def __init__(self, intent_started: threading.Event) -> None:
        self._calls = 0
        self._lock = threading.Lock()
        self._intent_started = intent_started

    def observe(self) -> HostComputeSnapshot:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self._intent_started.wait(timeout=2)
            time.sleep(0.2)
            return HostComputeSnapshot(
                status="unavailable",
                observed_at=1720000000.0,
                devices=(),
                adapter_kind="chrome_controlled_probe",
                reason_code="deterministic_probe_unavailable",
            )
        return HostComputeSnapshot(
            status="ready",
            observed_at=1720000000.0 + call,
            devices=(
                HostComputeDevice(
                    uuid="GPU-deterministic-1",
                    name="Deterministic GPU",
                    memory_total_mib=81920,
                ),
            ),
            adapter_kind="chrome_controlled_probe",
        )


class DeterministicDeepFetchProvider:
    """A real asynchronous provider seam with deterministic Web Research output."""

    def runtime_binding(self) -> DeepFetchRuntimeBinding:
        return DeepFetchRuntimeBinding(
            provider_ref="chrome/deterministic-deepfetch",
            provider_version="1",
            model_ref="chrome-test-model",
            harness_ref="chrome-test-harness",
            capability_bindings=("web-search-live", "web-fetch-live"),
        )

    def execute(self, request: DeepFetchProviderRequest) -> DeepFetchResult:
        assert request.scope["goal"]
        assert request.authorization_receipt.issuer == "human_collaboration"
        time.sleep(1.4)
        return DeepFetchResult(
            completion="limited",
            summary="两篇可核查论文比较了低照度显微去噪。",
            papers=(
                {
                    "title": "Self-supervised microscopy denoising",
                    "url": "https://example.org/papers/one",
                    "doi": "10.1000/chrome.one",
                    "source_kind": "publisher",
                    "fulltext_status": "accepted",
                    "retrieved_at": "2026-08-22T00:00:00Z",
                },
                {
                    "title": "Rare morphology under low light",
                    "url": "https://example.org/papers/two",
                    "doi": None,
                    "source_kind": "publisher",
                    "fulltext_status": "unavailable",
                    "retrieved_at": "2026-08-22T00:00:01Z",
                },
            ),
            fulltexts=(
                {
                    "paper_url": "https://example.org/papers/one",
                    "media_type": "text/plain",
                    "content": "Deterministic accepted open full text.",
                },
            ),
            limitations=("第二篇论文没有可合法获取的开放全文。",),
            native_session_ref="chrome-deepfetch-native-session",
            adapter_kind="chrome_deterministic_deepfetch",
        )


class TransientResearchGraph:
    """The first initialization fails at the first Owner for three retries."""

    def __init__(self, delegate, *, failure_limit: int = 3) -> None:
        self._delegate = delegate
        self._failure_limit = failure_limit
        self._initializations: list[str] = []
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def accept_quest(self, **kwargs):
        initialization_id = str(kwargs["initialization_id"])
        with self._lock:
            if initialization_id not in self._initializations:
                self._initializations.append(initialization_id)
            self._attempts[initialization_id] = (
                self._attempts.get(initialization_id, 0) + 1
            )
            should_fail = (
                self._initializations.index(initialization_id) == 0
                and self._attempts[initialization_id] <= self._failure_limit
            )
        if should_fail:
            raise OSError("deterministic quest acceptance unavailable")
        return self._delegate.accept_quest(**kwargs)


class TransientResearchMemory:
    """The second initialization pauses after Quest acceptance, exposing partial."""

    def __init__(self, delegate) -> None:
        self._delegate = delegate
        self._initializations: list[str] = []
        self._attempts: dict[str, int] = {}
        self._lock = threading.Lock()

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    def accept_question_content(self, **kwargs):
        initialization_id = str(kwargs["initialization_id"])
        with self._lock:
            if initialization_id not in self._initializations:
                self._initializations.append(initialization_id)
            self._attempts[initialization_id] = (
                self._attempts.get(initialization_id, 0) + 1
            )
            should_fail = (
                self._initializations.index(initialization_id) == 1
                and self._attempts[initialization_id] <= 3
            )
        if should_fail:
            # Make recovering observable through the public polling/SSE seam so
            # Chrome can prove a failed retry legally returns to partial.
            time.sleep(0.55)
            raise OSError("deterministic question custody unavailable")
        return self._delegate.accept_question_content(**kwargs)


def seed_legacy_current(human_collaboration, legacy_state: str) -> None:
    opened = human_collaboration.create_quest(
        {
            "goal": "保留升级前已确认的 legacy Quest。",
            "completion_criteria": "恢复期间仍可从公开 Web 检查既有 bundle。",
            "key_configuration": "legacy v1 resource configuration",
            "literature_scope": "open_access",
            "initial_question_direction": "继续核对首个缺失 Owner receipt。",
            "material_receipts": [],
        },
        "chrome-legacy-open",
    )
    if legacy_state == "draft":
        return
    human_collaboration.generate_question_proposal(
        opened["initialization_id"],
        opened["quest_draft"]["hash"],
        "chrome-legacy-generate",
        opened["quest_draft"]["revision"],
    )
    if not human_collaboration.process_drafting_once():
        raise RuntimeError("legacy proposal was not processed")
    ready = human_collaboration.query_quest_creation(opened["initialization_id"])
    previewed = human_collaboration.preview_confirmation(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        idempotency_key="chrome-legacy-preview",
    )
    preview = previewed["confirmation_preview"]
    human_collaboration.confirm_quest(
        ready["initialization_id"],
        quest_draft_revision=ready["quest_draft"]["revision"],
        quest_draft_hash=ready["quest_draft"]["hash"],
        proposal_ref=ready["proposal"]["ref"],
        proposal_hash=ready["proposal"]["hash"],
        preview_ref=preview["ref"],
        preview_hash=preview["hash"],
        idempotency_key="chrome-legacy-confirm",
    )
    human_collaboration.reconcile_once()
    recovered = human_collaboration.query_quest_creation(opened["initialization_id"])
    if recovered["status"] != "recovering":
        raise RuntimeError(f"legacy fixture did not enter recovering: {recovered['status']}")


async def serve(
    data_root: Path,
    legacy_state: str | None,
    web_root: Path | None,
) -> None:
    intent_started = threading.Event()
    adapter = DeterministicDraftingAdapter(intent_started)
    runtime = build_production_runtime(
        prepare_data_root(data_root),
        proposal_drafter=adapter,
        intent_drafting_provider=adapter,
        host_compute_probe=SequencedHostProbe(intent_started),
        deepfetch_provider=DeterministicDeepFetchProvider(),
    )
    human_collaboration = runtime.owners.human_collaboration
    human_collaboration._research_graph = TransientResearchGraph(  # noqa: SLF001
        runtime.owners.research_graph,
        failure_limit=1_000 if legacy_state == "recovering" else 3,
    )
    human_collaboration._research_memory = TransientResearchMemory(  # noqa: SLF001
        runtime.owners.research_memory
    )
    if legacy_state is not None:
        seed_legacy_current(human_collaboration, legacy_state)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(2048)
    port = int(listener.getsockname()[1])
    base_url = f"http://127.0.0.1:{port}"
    original_files = web_module.files
    if web_root is not None:
        resolved_web_root = web_root.resolve()
        if resolved_web_root.name != "web_dist" or not (
            resolved_web_root / "index.html"
        ).is_file():
            raise RuntimeError("--web-root must name a built web_dist directory")
        web_module.files = lambda _package: resolved_web_root.parent
    try:
        app = create_app(runtime, base_url=base_url, control_key="chrome-control-key")
    finally:
        web_module.files = original_files
    bootstrap_token = runtime.authentication.issue_bootstrap_token()
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        while not server.started and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            await task
            raise RuntimeError("deterministic product stopped before startup")
        print(
            json.dumps(
                {
                    "base_url": base_url,
                    "bootstrap_token": bootstrap_token,
                },
                separators=(",", ":"),
            ),
            flush=True,
        )
        await task
    finally:
        server.should_exit = True
        if not task.done():
            await task
        listener.close()
        runtime.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--legacy-state", choices=("draft", "recovering"))
    parser.add_argument("--web-root", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(args.data_root, args.legacy_state, args.web_root))


if __name__ == "__main__":
    main()
