"""Verified, read-only inputs for the timeline's separate summarizing agent.

Research facts come from Owner history, never from the bounded conversation
index. Public root messages are optional observations, not accepted results.
"""
from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.research_notes import manifest_research_notes
from meta_research.research_overview import ResearchOverviewReader, STAGES
from meta_research.root_session_observations import RootSessionObservations


_PUBLIC_BYTES = 65536
_PUBLIC_CHARS = 2000


def _gap(error: Exception, fallback: str) -> dict:
    code = getattr(error, "code", None)
    return {"code": code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,120}", code) else fallback}


def _sources(*groups: list[dict]) -> list[dict]:
    result = {}
    for group in groups:
        for source in group:
            if isinstance(source.get("ref"), str) and source["ref"]:
                result.setdefault(source["ref"], {"ref": source["ref"], "label": source["label"]})
    return sorted(result.values(), key=lambda value: value["ref"])


def _node(kind: str, *, quest_ref: str, question_ref: str, cycle_ref=None,
          stage=None, target_ref=None, content: dict, sources: list[dict]) -> dict:
    key = {"question": f"question:{question_ref}", "cycle": f"cycle:{cycle_ref}",
           "stage": f"stage:{cycle_ref}:{stage}", "target": f"target:{cycle_ref}:{target_ref}"}[kind]
    result = {"node_key": key, "kind": kind, "quest_ref": quest_ref,
              "question_ref": question_ref, "cycle_ref": cycle_ref, "stage": stage,
              "target_ref": target_ref, "content": content, "sources": _sources(sources)}
    result["source_hash"] = canonical_hash(result)
    return result


def _public_messages(page: dict) -> list[dict]:
    """Extract only complete public messages of the transport-verified root.

    Tail reads discard their first partial record and any unfinished last line.
    Explicit actor conflicts, children, tools and analysis never enter inputs.
    """
    root = page.get("root_native_session_ref")
    if not isinstance(root, str) or not root or page.get("exact") is False:
        return []
    raw = page.get("text", "")
    if not isinstance(raw, str):
        return []
    lines = raw.splitlines(keepends=True)
    if page.get("offset", 0) > 0:
        lines = lines[1:]
    messages = {}
    for line in lines:
        if not line.endswith(("\n", "\r")):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "thread.started" and event.get("thread_id") != root:
            return []
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "agent_message":
            continue
        actors = {value for value in (event.get("thread_id"), event.get("sender_thread_id"),
                  item.get("thread_id"), item.get("sender_thread_id")) if value is not None}
        if actors - {root}:
            continue
        if any(value.get("parent_thread_id") not in (None, "")
               or value.get("channel") in {"analysis", "reasoning"}
               or value.get("visibility") == "hidden" for value in (event, item)):
            continue
        body = item.get("text", item.get("content"))
        if not isinstance(body, str) or not body.strip():
            continue
        digest = canonical_hash(body)
        key = item.get("id") if isinstance(item.get("id"), str) else digest
        messages.pop(key, None)
        messages[key] = {"text": body[:_PUBLIC_CHARS], "truncated": len(body) > _PUBLIC_CHARS,
                         "content_hash": digest, "native_session_ref": root}
    return list(messages.values())[-2:]


class TimelineSummarySourceReader:
    def __init__(self, runtime):
        self.runtime = runtime
        self.database = runtime._database
        self.owners = runtime.owners
        self.overview = ResearchOverviewReader(self.owners.research_graph,
            self.owners.advancement_engine, self.owners.research_memory, self.owners.agent_runtime)
        self.observations = RootSessionObservations(runtime)

    def quest_refs(self) -> list[str]:
        # Enumerate identities only; read() verifies the selected Quest through RG.
        with self.database.read_snapshot() as connection:
            return list(connection.execute(text(
                "SELECT quest_ref FROM rg_quests ORDER BY accepted_at, quest_ref"
            )).scalars())

    def read(self, quest_ref: str) -> list[dict]:
        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 256:
            raise OwnerConflict("timeline_summary_quest_invalid")
        # No model/provider invocation and no research mutation occurs in this cut.
        with self.database.read_snapshot():
            rg, ae = self.owners.research_graph, self.owners.advancement_engine
            if rg.query_quest_by_ref(quest_ref) is None:
                raise OwnerConflict("timeline_summary_quest_not_found")
            history = ae.query_quest_stage_history(quest_ref)
            foreground = ae.query_foreground(quest_ref)
            if foreground is not None and (foreground.get("quest_ref") != quest_ref or not any(
                cycle.cycle_ref == foreground.get("cycle_ref") and cycle.question_ref == foreground.get("question_ref")
                for cycle in history
            )):
                raise OwnerConflict("timeline_summary_foreground_binding_invalid")
            try:
                index = self.observations.query(quest_ref)
                if index.get("quest_ref") != quest_ref:
                    raise OwnerConflict("timeline_summary_observation_binding_invalid")
            except Exception as error:
                index = {"sessions": [], "limited": True,
                         "reasons": [_gap(error, "timeline_public_progress_unavailable")]}
            nodes, questions = [], {}
            for ordinal, cycle in enumerate(history, 1):
                if cycle.question_ref not in questions:
                    question = self._question(quest_ref, cycle.question_ref)
                    questions[cycle.question_ref] = question
                    nodes.append(question)
                cycle_nodes = self._cycle(quest_ref, cycle, ordinal, foreground, index)
                nodes.extend(cycle_nodes)
            return nodes

    def _question(self, quest_ref: str, question_ref: str) -> dict:
        sources = [{"ref": question_ref, "label": "Question identity"}]
        content: dict[str, Any] = {"question": None, "gaps": []}
        try:
            question = self.owners.research_graph.query_question_history_by_ref(question_ref)
            if question is None or question.quest_ref != quest_ref or question.question_ref != question_ref:
                raise OwnerConflict("timeline_summary_question_binding_invalid")
            content["question"] = self.owners.research_memory.read_question_content(question.content_ref, question.content_hash)
            content["content_hash"] = question.content_hash
            sources.append({"ref": question.content_ref, "label": "Accepted Question content"})
        except Exception as error:
            content["gaps"].append(_gap(error, "timeline_question_unavailable"))
        return _node("question", quest_ref=quest_ref, question_ref=question_ref, content=content, sources=sources)

    def _cycle(self, quest_ref, cycle, ordinal, foreground, index):
        current = foreground if foreground and foreground["cycle_ref"] == cycle.cycle_ref else None
        requests = {item.request_ref: item for item in cycle.requests}
        commits = {item.request_ref: item for item in cycle.commits}
        pending_gap = None
        if current and current.get("stage") in STAGES:
            try:
                request = getattr(self.owners.advancement_engine,
                    f"query_{current['stage']}_stage_request")(cycle.cycle_ref)
                if request is not None:
                    self._request_scope(quest_ref, cycle, request)
                    if request.stage != current["stage"] or request.epoch != current["epoch"]:
                        raise OwnerConflict("timeline_summary_current_request_binding_invalid")
                    requests[request.request_ref] = request
            except Exception as error:
                pending_gap = _gap(error, "timeline_current_request_unavailable")
        targets, graph_gaps = self._targets(quest_ref, cycle, requests.values(), current, index)
        stages = []
        for stage in STAGES:
            content = {"ordinal": ordinal, "artifacts": [], "current": None, "gaps": []}
            sources = [{"ref": cycle.cycle_ref, "label": "Cycle identity"}]
            stage_requests = sorted((r for r in requests.values() if r.stage == stage), key=lambda r: (r.epoch, r.request_ref))
            for request in stage_requests:
                commit = commits.get(request.request_ref)
                sources.append({"ref": request.request_ref, "label": f"{stage} request"})
                if commit is None and stage != "bundle":
                    continue
                artifact = self._artifact(quest_ref, cycle, request, commit)
                if artifact is None:
                    continue
                content["artifacts"].append(artifact)
                sources.extend({"ref": ref, "label": f"{stage} {key}"}
                    for key, ref in artifact["source"].items() if key.endswith("_ref") and ref)
            # A malformed/missing request must remain a gap, not hide a commit.
            for commit in cycle.commits:
                if commit.stage == stage and commit.request_ref not in requests:
                    content["artifacts"].append(self._artifact(quest_ref, cycle, None, commit))
            if current and current.get("stage") == stage:
                content["current"] = self._control(current)
                if pending_gap:
                    content["gaps"].append(pending_gap)
                matched = next((r for r in stage_requests if r.epoch == current["epoch"]), None)
                if matched is not None:
                    try:
                        run = getattr(self.owners.agent_runtime, f"query_{stage}_stage_run")(matched.request_ref)
                        if run is not None:
                            if (run.request_ref, run.cycle_ref, run.stage, run.epoch) != (matched.request_ref, cycle.cycle_ref, stage, current["epoch"]):
                                raise OwnerConflict("timeline_summary_stage_run_binding_invalid")
                            progress, progress_sources = self._progress(quest_ref, cycle, index, current,
                                stage=stage, run_ref=run.run_ref)
                            content["current"].update(progress)
                            sources.extend(progress_sources)
                    except Exception as error:
                        content["gaps"].append(_gap(error, "timeline_stage_progress_unavailable"))
                elif not content["artifacts"]:
                    content["gaps"].append({"code": "timeline_current_request_not_observed"})
            if stage == "bundle":
                content["targets"] = [{"target_ref": n["target_ref"], **n["content"]} for n in targets]
                content["gaps"].extend(graph_gaps)
                sources = _sources(sources, *(n["sources"] for n in targets))
            stages.append(_node("stage", quest_ref=quest_ref, question_ref=cycle.question_ref,
                cycle_ref=cycle.cycle_ref, stage=stage, content=content, sources=sources))
        cycle_node = _node("cycle", quest_ref=quest_ref, question_ref=cycle.question_ref,
            cycle_ref=cycle.cycle_ref, content={"ordinal": ordinal,
                "current": None if current is None else self._control(current),
                "stages": {n["stage"]: n["content"] for n in stages}},
            sources=_sources([{"ref": cycle.cycle_ref, "label": "Cycle identity"}], *(n["sources"] for n in stages)))
        return [cycle_node, *stages[:3], *targets, stages[3]]

    def _artifact(self, quest_ref, cycle, request, commit):
        try:
            return self.overview._artifact(quest_ref, cycle, request, commit)
        except Exception as error:
            fact = commit or request
            return {"stage": fact.stage, "epoch": fact.epoch, "status": "unavailable",
                    "content": None, "source": {"request_ref": fact.request_ref,
                        "commit_ref": None if commit is None else commit.commit_ref},
                    "reason": _gap(error, "timeline_stage_artifact_unavailable")}

    @staticmethod
    def _request_scope(quest_ref, cycle, request):
        if request.cycle_ref != cycle.cycle_ref or request.accepted_question.quest_ref != quest_ref or request.accepted_question.question_ref != cycle.question_ref:
            raise OwnerConflict("timeline_summary_request_binding_invalid")

    @staticmethod
    def _control(foreground):
        # These are meaningful authorization/position changes, not worker polls.
        return {key: foreground.get(key) for key in ("stage", "epoch", "status", "grant_status")}

    def _targets(self, quest_ref, cycle, requests, current, index):
        result, gaps, seen = [], [], set()
        for request in sorted(requests, key=lambda r: (r.epoch, r.request_ref)):
            if request.stage != "bundle":
                continue
            try:
                self._request_scope(quest_ref, cycle, request)
                graph = self.owners.research_graph.query_target_graph(request.request_ref)
                if graph is None:
                    continue
                if graph.quest_ref != quest_ref or graph.cycle_ref != cycle.cycle_ref or graph.request_ref != request.request_ref:
                    raise OwnerConflict("timeline_summary_graph_binding_invalid")
                for target in graph.targets:
                    if target.graph_ref != graph.graph_ref:
                        raise OwnerConflict("timeline_summary_target_binding_invalid")
                    if target.target_ref not in seen:
                        seen.add(target.target_ref)
                        result.append(self._target(quest_ref, cycle, graph, target, current, index))
            except Exception as error:
                gaps.append({"request_ref": request.request_ref, **_gap(error, "timeline_target_graph_unavailable")})
        return result, gaps

    def _target(self, quest_ref, cycle, graph, target, current, index):
        sources = [{"ref": target.target_ref, "label": "Accepted Target"},
                   {"ref": graph.graph_ref, "label": "Accepted Target graph"}]
        content = {"target_key": target.target_key, "ordinal": target.ordinal,
                   "spec_hash": target.spec_hash, "research_context": None,
                   "accepted_result": None, "progress": None, "gaps": []}
        try:
            context = self.runtime.target_run_authorities.research_graph.query_target_research_context(target_ref=target.target_ref)
            if context["target_ref"] != target.target_ref or context["question_ref"] != cycle.question_ref or context["formal_plan_ref"] != graph.formal_plan_ref:
                raise OwnerConflict("timeline_summary_target_context_binding_invalid")
            content["research_context"] = {key: context[key] for key in (
                "question_ref", "question_content_ref", "question_content_hash", "formal_plan_ref",
                "plan_document_hash", "obligations", "experiment_briefs", "plan_notes", "bundle_notes",
                "bundle_notes_source_ref", "bundle_notes_source_hash", "selection_boundary") if key in context}
            sources.extend({"ref": context[key], "label": label} for key, label in (
                ("question_content_ref", "Accepted Question"), ("formal_plan_ref", "Selected Plan research goals"),
                ("bundle_notes_source_ref", "Bundle strategy at Target admission")) if context.get(key))
        except Exception as error:
            content["gaps"].append(_gap(error, "timeline_target_context_unavailable"))
        try:
            transition = self.owners.research_graph.query_target_root_commit_transition(target.target_ref)
            if transition is not None:
                if transition.target_ref != target.target_ref:
                    raise OwnerConflict("timeline_summary_target_commit_binding_invalid")
                # This is the already-composed, receipt-verifying manifest reader;
                # query() reads accepted ledger content without reopening assets.
                manifest = self.runtime.target_run_finalizer._memory.query(transition.canonical_terminal.asset_manifest_ref)
                if manifest is None or manifest.target_ref != target.target_ref or manifest.target_run_ref != transition.target_run_ref or manifest.completion_ref != transition.target_execution_closure_ref:
                    raise OwnerConflict("timeline_summary_target_result_binding_invalid")
                notes = manifest_research_notes(manifest)
                content["accepted_result"] = {"commit_ref": transition.target_commit_ref,
                    "manifest_ref": manifest.manifest_ref, "result_document": manifest.result_document.as_dict(),
                    "result_document_hash": manifest.result_document_hash, "research_notes": notes}
                sources.extend([{"ref": transition.target_commit_ref, "label": "Accepted TargetCommit"},
                                {"ref": manifest.manifest_ref, "label": "Accepted Target result"}])
                sources.extend({"ref": note["version_ref"], "label": "Accepted research note excerpt"} for note in notes)
        except Exception as error:
            content["gaps"].append(_gap(error, "timeline_target_result_unavailable"))
        if current is not None and content["accepted_result"] is None:
            progress, extra = self._progress(quest_ref, cycle, index, current, target_ref=target.target_ref)
            content["progress"] = progress
            sources.extend(extra)
        return _node("target", quest_ref=quest_ref, question_ref=cycle.question_ref,
            cycle_ref=cycle.cycle_ref, stage="bundle", target_ref=target.target_ref, content=content, sources=sources)

    def _progress(self, quest_ref, cycle, index, current, *, stage=None, run_ref=None, target_ref=None):
        result = {"control": self._control(current), "public_messages": [], "gaps": []}
        sources = []
        candidates = [session for session in index.get("sessions", [])
            if session.get("cycle_ref") == cycle.cycle_ref and session.get("question_ref") == cycle.question_ref
            and session.get("activity_label") != "已由新会话接续"
            and ((target_ref is not None and session.get("kind") == "target" and session.get("target_ref") == target_ref)
                 or (target_ref is None and session.get("kind") == "stage" and session.get("stage") == stage and session.get("run_ref") == run_ref))]
        if not candidates:
            result["gaps"] = [{"code": "timeline_public_progress_not_observed"}]
            return result, sources
        session = max(candidates, key=lambda s: (bool(s.get("is_current")), s.get("created_at", 0), s["session_ref"]))
        result["session_ref"] = session["session_ref"]
        # Polling between executing/waiting is not a scientific status change.
        if session.get("status") in {"completed", "failed", "paused"}:
            result["observed_session_status"] = session["status"]
        if index.get("limited"):
            result["gaps"].append({"code": "timeline_public_progress_index_limited"})
        sources.append({"ref": session["session_ref"], "label": "Public root conversation (unaccepted progress)"})
        operations = sorted(session.get("operations", []), key=lambda op: (op.get("created_at", 0), op["operation_ref"]), reverse=True)
        for operation in operations[:2]:
            try:
                page = self.observations.query_output(quest_ref, session["session_ref"], operation_ref=operation["operation_ref"], limit=4)
                offset = max(0, int(page.get("source_bytes", 0)) - _PUBLIC_BYTES)
                page = self.observations.query_output(quest_ref, session["session_ref"], operation_ref=operation["operation_ref"], after=offset, limit=_PUBLIC_BYTES)
                if page.get("quest_ref") != quest_ref or page.get("session_ref") != session["session_ref"] or page.get("operation_ref") != operation["operation_ref"]:
                    raise OwnerConflict("timeline_summary_public_message_binding_invalid")
                if page.get("transport_caught_up") is False:
                    result["gaps"].append({"code": "timeline_public_output_not_caught_up"})
                    continue
                messages = _public_messages(page)
                if messages:
                    result["public_messages"] = [{**message, "operation_ref": operation["operation_ref"]} for message in messages] + result["public_messages"]
                    sources.append({"ref": operation["operation_ref"], "label": "Public root explanation (not an accepted result)"})
                if len(result["public_messages"]) >= 2:
                    break
            except Exception as error:
                result["gaps"].append(_gap(error, "timeline_public_messages_unavailable"))
        result["public_messages"] = result["public_messages"][-2:]
        # Multiple operations can expose the same observational gap. Their
        # count/order is not new research material and must not retrigger prose.
        result["gaps"] = [{"code": code} for code in sorted({gap["code"] for gap in result["gaps"]})]
        return result, sources
