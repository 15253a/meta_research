from __future__ import annotations

from typing import Any

from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.writing_snapshot import WritingResearchSnapshotReader


STAGES = ("idea", "plan", "bundle", "reasoning")
SCHEMA = "meta-research/research-overview/v1"


class ResearchOverviewReader(WritingResearchSnapshotReader):
    """Read accepted research artifacts through the existing receipt verifiers.

    This deliberately excludes Writing's asset inventory and frozen snapshot:
    neither is the live research position. No model, Owner write, or provider is
    invoked. An unavailable historical artifact remains a visible bounded gap.
    """

    def query(self, quest_ref: str) -> dict[str, Any]:
        for _attempt in range(3):
            candidate = self._query_once(quest_ref)
            verified = self._query_once(quest_ref)
            if verified == candidate:
                return verified
        raise OwnerConflict("research_overview_consistency_unavailable")

    def _query_once(self, quest_ref: str) -> dict[str, Any]:
        quest = self._research_graph.query_quest_by_ref(quest_ref)
        if quest is None:
            raise OwnerConflict("research_overview_quest_not_found")
        foreground = self._advancement_engine.query_foreground(quest_ref)
        question_ref = None if foreground is None else foreground["question_ref"]
        cycle_ref = None if foreground is None else foreground["cycle_ref"]
        if foreground is not None and foreground["quest_ref"] != quest_ref:
            raise OwnerConflict("research_overview_foreground_binding_invalid")
        cycles: list[dict[str, Any]] = []
        findings: dict[str, list[dict[str, Any]]] = {
            "quest": [], "question": [], "cycle": []
        }
        limited = False
        # Owner order is created_at, cycle_ref; do not use foreground epoch or
        # question-tree order as a cycle number, including revisited questions.
        for ordinal, cycle in enumerate(
            self._advancement_engine.query_quest_stage_history(quest_ref), 1
        ):
            stages: dict[str, list[dict[str, Any]]] = {stage: [] for stage in STAGES}
            requests = {item.request_ref: item for item in cycle.requests}
            committed = {item.request_ref for item in cycle.commits}
            entries = [(requests.get(item.request_ref), item) for item in cycle.commits]
            entries.extend(
                (request, None) for request in cycle.requests
                if request.stage == "bundle" and request.request_ref not in committed
            )
            entries.sort(key=self._history_entry_order)
            for request, commit in entries:
                fact = commit or request
                if fact is None or fact.stage not in STAGES:
                    raise OwnerConflict("research_overview_stage_invalid")
                artifact = self._artifact(quest_ref, cycle, request, commit)
                if artifact is None:
                    continue
                stages[fact.stage].append(artifact)
                limited = limited or artifact["status"] == "unavailable"
                if artifact["stage"] == "reasoning" and artifact["status"] == "accepted":
                    for scope in findings:
                        if scope == "question" and cycle.question_ref != question_ref:
                            continue
                        if scope == "cycle" and cycle.cycle_ref != cycle_ref:
                            continue
                        finding = self._finding(quest_ref, cycle, ordinal, artifact, scope)
                        if finding is not None:
                            findings[scope].append(finding)
            cycles.append({
                "cycle_ref": cycle.cycle_ref,
                "question_ref": cycle.question_ref,
                "ordinal": ordinal,
                "stages": stages,
            })
        current = next((item for item in cycles if item["cycle_ref"] == cycle_ref), None)
        if foreground is not None and (
            current is None or current["question_ref"] != question_ref
        ):
            raise OwnerConflict("research_overview_foreground_binding_invalid")
        # Newest first, retaining every exact finding and its original source.
        for items in findings.values():
            items.reverse()
        payload: dict[str, Any] = {
            "schema_ref": SCHEMA,
            "status": "limited" if limited else "idle" if foreground is None else "ready",
            "quest_ref": quest_ref,
            "question_ref": question_ref,
            "cycle_ref": cycle_ref,
            "cycle_ordinal": None if current is None else current["ordinal"],
            "foreground": foreground,
            "findings": findings,
            "cycles": cycles,
            "reason": {"code": "research_overview_artifact_unavailable"} if limited else None,
        }
        # Equality/ETag-like identity contains only this exact read cut.
        payload["projection_hash"] = canonical_hash(payload)
        return payload

    def _artifact(self, quest_ref: str, cycle: Any, request: Any, commit: Any) -> dict[str, Any] | None:
        fact = commit or request
        source = {
            "commit_ref": None if commit is None else commit.commit_ref,
            "request_ref": fact.request_ref,
            "run_ref": None if commit is None else commit.run_ref,
            "outcome_ref": None if commit is None else commit.outcome_ref,
            "content_ref": None,
            "content_hash": None,
        }
        artifact: dict[str, Any] = {
            "stage": fact.stage, "epoch": fact.epoch,
            "status": "unavailable", "source": source, "content": None, "reason": None,
        }
        try:
            if fact.cycle_ref != cycle.cycle_ref or (
                request is not None and (
                    request.cycle_ref != cycle.cycle_ref
                    or request.accepted_question.quest_ref != quest_ref
                    or request.accepted_question.question_ref != cycle.question_ref
                    or (commit is not None and (
                        commit.request_ref != request.request_ref
                        or commit.stage != request.stage or commit.epoch != request.epoch
                    ))
                )
            ):
                raise OwnerConflict("research_overview_artifact_binding_invalid")
            if commit is not None and commit.disposition != "completed":
                artifact["status"] = commit.disposition
                artifact["source"]["basis_ref"] = commit.basis_ref
                return artifact
            if request is None:
                raise OwnerConflict("research_overview_stage_request_missing")
            if fact.stage == "bundle":
                value = self._bundle_stage_value(request, commit)
                if value is None or value.get("report") is None:
                    return None
                report = value["report"]
                content = report["report"]
                source.update({
                    "run_ref": report["run_ref"],
                    "outcome_ref": report["report_ref"],
                    "content_ref": report["report_ref"],
                    "content_hash": report["report_hash"],
                })
                artifact["status"] = "report_accepted" if commit is None else "accepted"
            else:
                value = (
                    self._reasoning_stage_value(request, commit)
                    if fact.stage == "reasoning" else self._stage_value(request, commit)
                )
                result = value["result"]
                source.update({"content_ref": result["content_ref"], "content_hash": result["content_hash"]})
                if fact.stage == "plan":
                    content = result["plan_document"]
                elif fact.stage == "reasoning":
                    content = result["outcome"]["scientific_outcome"]
                else:
                    content = result["outcome"]
                if content.get("question_ref") != cycle.question_ref:
                    raise OwnerConflict("research_overview_content_binding_invalid")
                if fact.stage == "reasoning" and (
                    content.get("cycle_ref") != cycle.cycle_ref
                    or content.get("quest_ref") != quest_ref
                    or content.get("outcome_ref") != commit.outcome_ref
                ):
                    raise OwnerConflict("research_overview_content_binding_invalid")
                artifact["status"] = "accepted"
            artifact["content"] = content
            return artifact
        except (OwnerConflict, KeyError, TypeError, AttributeError) as error:
            artifact["status"] = "unavailable"
            artifact["content"] = None
            artifact["reason"] = {"code": (
                error.code if isinstance(error, OwnerConflict)
                else "research_overview_artifact_shape_invalid"
            )}
            return artifact

    @staticmethod
    def _finding(quest_ref: str, cycle: Any, ordinal: int, artifact: dict[str, Any], scope: str) -> dict[str, Any] | None:
        content = artifact["content"]
        synthesis = content.get("research_synthesis")
        section_name, field, identity_name, identity = {
            "quest": ("quest", "impact", "quest_ref", quest_ref),
            "question": ("current_question", "progress", "question_ref", cycle.question_ref),
            "cycle": ("cycle", "impact", "cycle_ref", cycle.cycle_ref),
        }[scope]
        section = synthesis.get(section_name) if isinstance(synthesis, dict) else None
        text_field = "claim"
        text = content.get("claim")
        if isinstance(section, dict):
            if section.get(identity_name) != identity:
                return None
            if isinstance(section.get(field), str) and section[field].strip():
                text = section[field]
                text_field = f"research_synthesis.{section_name}.{field}"
        if not isinstance(text, str) or not text.strip():
            return None
        return {
            "text": text, "text_field": text_field,
            "disposition": content["disposition"],
            "quest_ref": quest_ref, "question_ref": cycle.question_ref,
            "cycle_ref": cycle.cycle_ref, "cycle_ordinal": ordinal,
            "stage": "reasoning", "epoch": artifact["epoch"],
            "source": dict(artifact["source"]),
        }
