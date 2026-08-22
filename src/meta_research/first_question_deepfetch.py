from __future__ import annotations

from meta_research.deepfetch import DeepFetchProvider, DeepFetchUnavailable
from meta_research.owners.agent_runtime import AgentRuntimeInterface
from meta_research.owners.common import OwnerConflict
from meta_research.owners.human_collaboration import HumanCollaborationInterface
from meta_research.owners.research_memory import ResearchMemoryInterface


class FirstQuestionDeepFetchWorker:
    """Deep module coordinating HC authority, AR execution, and RM acceptance."""

    def __init__(
        self,
        human_collaboration: HumanCollaborationInterface,
        agent_runtime: AgentRuntimeInterface,
        research_memory: ResearchMemoryInterface,
        provider: DeepFetchProvider,
    ) -> None:
        self._human_collaboration = human_collaboration
        self._agent_runtime = agent_runtime
        self._research_memory = research_memory
        self._provider = provider

    def process_once(self) -> bool:
        request = self._human_collaboration.query_next_deepfetch_request()
        if request is None:
            return False
        try:
            run = self._agent_runtime.execute_deepfetch(request, self._provider)
            snapshot = self._research_memory.accept_literature_snapshot(request, run)
            self._human_collaboration.record_deepfetch_succeeded(
                request.request_ref,
                run.run_ref,
                snapshot,
            )
        except DeepFetchUnavailable as error:
            if error.code == "deepfetch_acquisition_waiting_user":
                return False
            if error.code == "deepfetch_provider_stopped":
                return True
            if error.code == "deepfetch_provider_reconciliation_pending":
                return False
            run = self._agent_runtime.query_deepfetch_run(request.request_ref)
            self._human_collaboration.record_deepfetch_failed(
                request.request_ref,
                error.code,
                None if run is None else run.run_ref,
            )
        except OwnerConflict as error:
            if error.code == "deepfetch_run_busy":
                return False
            if error.code == "deepfetch_acquisition_not_ready":
                session = self._agent_runtime.query_acquisition_session(
                    session_ref=request.acquisition_session_ref
                )
                if session is not None and session.status == "waiting_user":
                    return False
            if error.code in {
                "deepfetch_attempt_fence_stale",
                "deepfetch_run_cancelled",
            }:
                # A predecessor Attempt is no longer authoritative.  Its late
                # completion must not write failure state onto the successor.
                return True
            run = self._agent_runtime.query_deepfetch_run(request.request_ref)
            self._human_collaboration.record_deepfetch_failed(
                request.request_ref,
                error.code,
                None if run is None else run.run_ref,
            )
        return True
