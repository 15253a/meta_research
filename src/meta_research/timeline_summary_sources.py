"""Verified, read-only inputs for the timeline's separate summarizing agent.

Research facts come from Owner history, never from the bounded conversation
index. Public root messages are optional observations, not accepted results.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from types import SimpleNamespace

from sqlalchemy import text

from meta_research.cycle_position import validate_cycle_position
from meta_research.owners.common import OwnerConflict, canonical_hash
from meta_research.research_notes import manifest_research_notes, read_note_body
from meta_research.research_overview import ResearchOverviewReader, STAGES
from meta_research.root_session_observations import RootSessionObservations


_PUBLIC_BYTES = 65536


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
        actors = {
            value
            for value in (
                event.get("thread_id"),
                event.get("sender_thread_id"),
                item.get("thread_id"),
                item.get("sender_thread_id"),
            )
            if value is not None
        }
        if actors - {root}:
            continue
        if any(
            value.get("parent_thread_id") not in (None, "")
            or value.get("channel") in {"analysis", "reasoning"}
            or value.get("visibility") == "hidden"
            for value in (event, item)
        ):
            continue
        body = item.get("text", item.get("content"))
        if not isinstance(body, str) or not body.strip():
            continue
        digest = canonical_hash(body)
        key = item.get("id") if isinstance(item.get("id"), str) else digest
        messages.pop(key, None)
        messages[key] = {"text": body, "content_hash": digest, "native_session_ref": root}
    return list(messages.values())[-2:]


def _source(ref, label, kind, **facts):
    return {"ref": ref, "label": label, "binding": {"kind": kind, **facts}}


def _node(key, kind, quest, question, sources, children=(), **position):
    # Each ancestor can open the same exact child sources even in a later batch.
    by_ref = {source["ref"]: source for source in sources}
    for child in children:
        for source in child["sources"]:
            by_ref.setdefault(source["ref"], source)
    node = {
        "node_key": key,
        "kind": kind,
        "quest_ref": quest,
        "question_ref": question,
        **position,
        "sources": list(by_ref.values()),
    }
    # Changing the byte cursor with a private/tool event does not change
    # the public work observed. Ancestors inherit only their children's revisions.
    revisions = [(s["ref"], {k: v for k, v in s["binding"].items() if k not in {"start", "end"}}) for s in sources]
    node["source_hash"] = canonical_hash([key, position, revisions, [child["source_hash"] for child in children]])
    return node


class TimelineSummarySourceReader:
    def __init__(self, runtime):
        self.runtime = runtime
        self.database = runtime._database
        self.owners = runtime.owners
        self.overview = ResearchOverviewReader(
            self.owners.research_graph,
            self.owners.advancement_engine,
            self.owners.research_memory,
            self.owners.agent_runtime,
        )
        self.observations = RootSessionObservations(runtime)
        self._public_cuts = {}  # Identities and cursors, never public bodies.

    def quest_refs(self) -> list[str]:
        with self.database.read_snapshot() as connection:
            return list(
                connection.execute(text("SELECT quest_ref FROM rg_quests ORDER BY accepted_at, quest_ref")).scalars()
            )

    def read(self, quest_ref: str) -> list[dict]:
        """Locate the cut from immutable Owner identities and revisions. No RM body reads."""
        if not isinstance(quest_ref, str) or not quest_ref or len(quest_ref) > 256:
            raise OwnerConflict("timeline_summary_quest_invalid")
        with self.database.read_snapshot() as db:
            if (
                db.execute(text("SELECT 1 FROM rg_quests WHERE quest_ref=:quest"), {"quest": quest_ref}).first()
                is None
            ):
                raise OwnerConflict("timeline_summary_quest_not_found")
            cycles = _rows(
                db,
                "SELECT cycle_ref,question_ref,cycle_position_json FROM ae_cycles "
                "WHERE quest_ref=:quest ORDER BY created_at,cycle_ref",
                quest_ref,
            )
            foreground = self.owners.advancement_engine.query_foreground(quest_ref)
            if foreground and (
                foreground["quest_ref"] != quest_ref
                or not any(
                    (c["cycle_ref"], c["question_ref"]) == (foreground["cycle_ref"], foreground["question_ref"])
                    for c in cycles
                )
            ):
                raise OwnerConflict("timeline_summary_foreground_binding_invalid")
            artifacts = _artifact_bindings(db, quest_ref)
            targets = _target_bindings(db, quest_ref)
            progress = self._progress(quest_ref, foreground) if foreground else {}
            question_sources = _question_bindings(db, quest_ref)
            grouped = defaultdict(list)
            for ordinal, cycle in enumerate(cycles, 1):
                c, q = cycle["cycle_ref"], cycle["question_ref"]
                position = None
                if cycle["cycle_position_json"] is not None:
                    try:
                        position = json.loads(cycle["cycle_position_json"])
                        validate_cycle_position(position, cycle_ref=c)
                    except (ValueError, TypeError) as error:
                        raise OwnerConflict("timeline_summary_cycle_position_invalid") from error
                identity = _source(c, "Cycle identity and focus", "cycle", cycle_ref=c,
                                   question_ref=q, ordinal=ordinal, cycle_position=position)
                target_nodes = []
                for target, sources in targets[c]:
                    target_nodes.append(
                        _node(
                            f"target:{c}:{target}",
                            "target",
                            quest_ref,
                            q,
                            [*sources, *progress.get(("target", target), [])],
                            cycle_ref=c,
                            stage="bundle",
                            target_ref=target,
                        )
                    )
                stage_nodes = []
                for stage in STAGES:
                    current = None
                    if foreground and (foreground["cycle_ref"], foreground["stage"]) == (c, stage):
                        current = {k: foreground.get(k) for k in ("stage", "epoch", "status", "grant_status")}
                    key = f"stage:{c}:{stage}"
                    refs = artifacts[c, stage]
                    stage_cut = _source(
                        key,
                        f"{stage} stage observation",
                        "stage",
                        cycle_ref=c,
                        question_ref=q,
                        stage=stage,
                        artifacts=[s["ref"] for s in refs],
                        current=current,
                    )
                    stage_nodes.append(
                        _node(
                            key,
                            "stage",
                            quest_ref,
                            q,
                            [identity, stage_cut, *refs, *progress.get(("stage", stage), [])]
                            if current is not None
                            else [identity, stage_cut, *refs],
                            target_nodes if stage == "bundle" else (),
                            cycle_ref=c,
                            stage=stage,
                        )
                    )
                cycle_node = _node("cycle:" + c, "cycle", quest_ref, q, [identity], stage_nodes, cycle_ref=c)
                grouped[q].append([cycle_node, *stage_nodes[:3], *target_nodes, stage_nodes[3]])
            if grouped.keys() - question_sources.keys():
                raise OwnerConflict("timeline_summary_question_binding_invalid")
            questions = {
                q: _node("question:" + q, "question", quest_ref, q, [source], [g[0] for g in grouped[q]])
                for q, source in question_sources.items()
            }
            nodes = []
            by_cycle = {g[0]["cycle_ref"]: g for groups in grouped.values() for g in groups}
            for cycle in cycles:
                question = questions.pop(cycle["question_ref"], None)
                if question:
                    nodes.append(question)
                nodes.extend(by_cycle[cycle["cycle_ref"]])
            nodes.extend(questions.values())  # Accepted Questions that have no Cycle yet.
            return nodes

    def _progress(self, quest, foreground):
        result = {}
        for key, session, status, operations in self.observations.research_sessions(quest, foreground):
            sources = [
                _source(
                    session,
                    "Public research Session",
                    "session",
                    session_ref=session,
                    status=status if status in {"completed", "failed", "paused"} else None,
                )
            ]
            for op_ref in operations:
                try:
                    binding = self._public_binding(quest, session, op_ref)
                    if binding is not None:
                        sources.append(_source(op_ref, "Public root explanation", "public", **binding))
                except (KeyError, ValueError, OwnerConflict, OSError):
                    sources.append(
                        _source(
                            op_ref,
                            "Public root output unavailable",
                            "gap",
                            reason="timeline_public_messages_unavailable",
                        )
                    )
            result[key] = sources
        return result

    def _page(self, quest, session, operation, start, limit):
        p = self.observations.query_output(quest, session, operation_ref=operation, after=start, limit=limit)
        if (
            (p.get("quest_ref"), p.get("session_ref"), p.get("operation_ref")) != (quest, session, operation)
            or not p.get("exact")
            or not p.get("root_native_session_ref")
            or p.get("transport_caught_up") is False
        ):
            raise OwnerConflict("timeline_public_message_binding_invalid")
        return p

    def _public_binding(self, quest, session, operation):
        first = self._page(quest, session, operation, 0, 4)
        end = int(first["source_bytes"])
        revision = (first.get("stream_ref"), first["root_native_session_ref"], end, first.get("source_change_ns"))
        # On Linux ctime changes even if a same-size edit restores mtime. If
        # that witness is unavailable, verify the active root's public tail.
        reusable = sys.platform.startswith("linux") and bool(revision[0]) and revision[-1] is not None
        identity = (quest, session, operation)
        previous = self._public_cuts.get(identity)
        if reusable and previous and previous[0] == revision:
            return previous[1]
        start = max(0, end - _PUBLIC_BYTES)
        args = dict(
            session_ref=session,
            operation_ref=operation,
            native=first["root_native_session_ref"],
            stream_ref=first.get("stream_ref"),
            start=start,
            end=end,
        )
        raw = self._public_text(quest, args)
        # Complete the line crossing the tail boundary if it may be one of
        # the last two messages; a single explanation may exceed one page.
        original_lines = len(raw.splitlines())
        tail = _public_messages({"root_native_session_ref": args["native"], "offset": start, "text": raw})
        while start and len(tail) < 2 and len(raw.splitlines()) <= original_lines:
            start = max(0, end - 2 * (end - start))
            args["start"] = start
            raw = self._public_text(quest, args)
        messages = _public_messages({"root_native_session_ref": args["native"], "offset": start, "text": raw})
        bound = {**args, "hashes": [m["content_hash"] for m in messages]} if messages else None
        if reusable:
            if len(self._public_cuts) >= 256:
                self._public_cuts.pop(next(iter(self._public_cuts)))
            self._public_cuts[identity] = revision, bound
        return bound

    def _read_public(self, quest, b):
        raw = self._public_text(quest, b)
        return _public_messages({"root_native_session_ref": b["native"], "offset": b["start"], "text": raw})

    def _public_text(self, quest, b):
        start, end = b["start"], b["end"]
        parts = []
        cursor = start
        while cursor < end:
            try:
                limit = max(4, min(_PUBLIC_BYTES, end - cursor))
                p = self._page(quest, b["session_ref"], b["operation_ref"], cursor, limit)
            except ValueError:
                # A byte-addressed tail can start in the middle of UTF-8.
                if start and cursor < start + 3:
                    cursor += 1
                    continue
                raise
            if (
                p["root_native_session_ref"] != b["native"]
                or p["source_bytes"] < end
                or p.get("stream_ref") != b["stream_ref"]
                or p["offset"] != cursor
                or not cursor < p["next_offset"] <= cursor + limit
            ):
                raise OwnerConflict("timeline_public_output_drift")
            if p["next_offset"] > end:
                # query_output requires at least four bytes. An append after the cut
                # may fill that last page; discard it, even if its UTF-8 is multibyte.
                parts.append(p["text"].encode("utf-8")[: end - cursor].decode("utf-8", errors="ignore"))
                break
            parts.append(p["text"])
            cursor = p["next_offset"]
        return "".join(parts)

    def read_source(self, quest_ref: str, node: dict, source_ref: str) -> dict:
        """Resolve only a source of the requested node, against its frozen binding."""
        source = next((s for s in node["sources"] if s["ref"] == source_ref), None)
        if node.get("quest_ref") != quest_ref or source is None:
            raise OwnerConflict("timeline_summary_source_not_granted")
        b = source["binding"]
        with self.database.read_snapshot() as db:
            if not db.execute(text("SELECT 1 FROM rg_quests WHERE quest_ref=:q"), {"q": quest_ref}).first():
                raise OwnerConflict("timeline_summary_quest_not_found")
            kind = b["kind"]
            if kind in {"cycle", "stage"}:
                if not db.execute(
                    text("SELECT 1 FROM ae_cycles WHERE quest_ref=:q AND cycle_ref=:c AND question_ref=:r"),
                    {"q": quest_ref, "c": b["cycle_ref"], "r": b["question_ref"]},
                ).first():
                    raise OwnerConflict("timeline_summary_source_not_granted")
                return {k: v for k, v in b.items() if k != "kind"}
            if kind == "question":
                fact = self.owners.research_graph.query_question_history_by_ref(b["question_ref"])
                if fact is None or (fact.quest_ref, fact.content_ref, fact.content_hash) != (
                    quest_ref,
                    source_ref,
                    b["hash"],
                ):
                    raise OwnerConflict("timeline_summary_question_binding_invalid")
                return self.owners.research_memory.read_question_content(source_ref, b["hash"])
            if kind == "artifact":
                return self._read_artifact(quest_ref, b, db)
            if kind in {"target", "result", "note"}:
                return self._read_target(quest_ref, source_ref, b)
            if kind == "public":
                messages = self._read_public(quest_ref, b)
                if [m["content_hash"] for m in messages] != b["hashes"]:
                    raise OwnerConflict("timeline_public_output_drift")
                return {"public_messages": messages}
            if kind == "session":
                return {"session_ref": b["session_ref"], "status": b["status"]}
            raise OwnerConflict(b.get("reason", "timeline_summary_source_unavailable"))

    def _read_artifact(self, quest, b, db):
        ae = self.owners.advancement_engine
        request = None if b["request_ref"] is None else ae.query_stage_request_by_ref(b["request_ref"])
        if request is None and b["request_ref"] is not None:
            raise OwnerConflict("timeline_summary_request_unavailable")
        if request and (
            request.accepted_question.quest_ref,
            request.accepted_question.question_ref,
            request.cycle_ref,
            request.stage,
        ) != (quest, b["question_ref"], b["cycle_ref"], b["stage"]):
            raise OwnerConflict("timeline_summary_request_binding_invalid")
        commit = None
        if b["commit_ref"]:
            # A typed route skip can have no StageRunRequest. Keep the exact verified
            # commit from this cut, including its disposition and original basis.
            row = db.execute(
                text("SELECT * FROM ae_stage_commits WHERE commit_ref=:ref AND cycle_ref=:cycle"),
                {"ref": b["commit_ref"], "cycle": b["cycle_ref"]},
            ).first()
            if row is None or row.receipt_hash != b["receipt_hash"]:
                raise OwnerConflict("timeline_summary_commit_binding_invalid")
            commit = ae._stage_commit_from_row(row)
        artifact = self.overview._artifact(
            quest, SimpleNamespace(cycle_ref=b["cycle_ref"], question_ref=b["question_ref"]), request, commit
        )
        if artifact is None or artifact["status"] == "unavailable":
            raise OwnerConflict(
                ((artifact or {}).get("reason") or {}).get("code", "timeline_stage_artifact_unavailable")
            )
        if artifact["source"]["outcome_ref"] != b["outcome_ref"] or (
            b.get("report_hash") and artifact["source"]["content_hash"] != b["report_hash"]
        ):
            raise OwnerConflict("timeline_stage_artifact_drift")
        return artifact

    def _read_target(self, quest, source_ref, b):
        target = b["target_ref"]
        with self.database.read() as db:
            row = db.execute(
                text(
                    "SELECT t.spec_hash FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref "
                    "JOIN ae_cycles c ON c.cycle_ref=g.cycle_ref WHERE g.quest_ref=:quest AND c.quest_ref=:quest "
                    "AND c.question_ref=:question AND t.target_ref=:target"
                ),
                {"quest": quest, "question": b["question_ref"], "target": target},
            ).first()
        if row is None or row.spec_hash != b["spec_hash"]:
            raise OwnerConflict("timeline_summary_target_binding_invalid")
        if b["kind"] == "note":
            with self.database.read() as db:
                stored = db.execute(
                    text(
                        "SELECT note_hash FROM rm_target_research_notes "
                        "WHERE target_ref=:target AND version_ref=:version"
                    ),
                    {"target": target, "version": source_ref},
                ).scalar_one_or_none()
            if stored != b["note_hash"]:
                raise OwnerConflict("timeline_summary_target_note_drift")
            return self.owners.research_memory.read_question_research_note(
                quest_ref=quest, question_ref=b["question_ref"], version_ref=source_ref
            )
        if b["kind"] == "target":
            context = self.runtime.target_run_authorities.research_graph.query_target_research_context(
                target_ref=target
            )
            if (
                context["target_ref"],
                context["question_ref"],
                context["plan_document_hash"],
                context["graph_ref"],
                context["stage_request_ref"],
            ) != (target, b["question_ref"], b["plan_hash"], b["graph_ref"], b["request_ref"]):
                raise OwnerConflict("timeline_summary_target_context_binding_invalid")
            # Bundle notes are fixed at Target admission. The Owner selects from
            # the original graph or one of the admitted append's predecessors.
            notes = (context["bundle_notes_source_ref"], context["bundle_notes_source_hash"])
            if notes != (b["initial_ref"], b["initial_hash"]):
                with self.database.read() as db:
                    match = db.execute(
                        text(
                            """SELECT 1 FROM rg_target_graph_appends a
                        JOIN rg_target_graph_appends admitted ON admitted.graph_ref=a.graph_ref
                        WHERE admitted.graph_ref=:graph_ref AND admitted.append_ref=:append_ref
                        AND admitted.receipt_hash=:append_hash AND a.generation<=admitted.generation
                        AND a.proposal_ref=:ref AND a.proposal_hash=:hash"""
                        ),
                        {**b, "ref": notes[0], "hash": notes[1]},
                    ).first()
                if match is None:
                    raise OwnerConflict("timeline_summary_target_notes_drift")
            return {
                k: context[k]
                for k in (
                    "target_ref",
                    "question",
                    "formal_plan_ref",
                    "obligations",
                    "experiment_briefs",
                    "plan_notes",
                    "bundle_notes",
                    "selection_boundary",
                )
            }
        transition = self.owners.research_graph.query_target_root_commit_transition(target)
        if transition is None or (
            transition.target_ref,
            transition.target_commit_ref,
            transition.canonical_terminal.asset_manifest_ref,
        ) != (target, source_ref, b["manifest_ref"]):
            raise OwnerConflict("timeline_summary_target_commit_binding_invalid")
        manifest = self.runtime.target_run_finalizer._memory.query(b["manifest_ref"])
        if (
            manifest is None
            or manifest.target_ref != target
            or manifest.completion_ref != transition.target_execution_closure_ref
            or manifest.target_run_ref != transition.target_run_ref
            or manifest.result_document_hash != b["result_hash"]
        ):
            raise OwnerConflict("timeline_summary_target_result_binding_invalid")
        result = {
            "commit_ref": source_ref,
            "manifest_ref": b["manifest_ref"],
            "result_document": manifest.result_document.as_dict(),
            "research_notes": [],
        }
        for note in manifest_research_notes(manifest):
            result["research_notes"].append(
                {"version_ref": note["version_ref"], "body": read_note_body(self.owners.research_memory, note)}
            )
        return result


def _rows(db, sql, quest):
    return db.execute(text(sql), {"quest": quest}).mappings().all()


def _question_bindings(db, quest):
    columns = "question_ref,content_ref,content_hash"
    union = " UNION ALL ".join(
        f"SELECT {columns} FROM {table} WHERE quest_ref=:quest"
        for table in ("rg_questions", "rg_manual_questions", "rg_autonomous_questions")
    )
    return {
        r["question_ref"]: _source(
            r["content_ref"], "Accepted Question", "question", question_ref=r["question_ref"], hash=r["content_hash"]
        )
        for r in _rows(db, union, quest)
    }


def _artifact_bindings(db, quest):
    result = defaultdict(list)
    rows = _rows(
        db,
        """SELECT s.cycle_ref,c.question_ref,s.stage,s.epoch,s.commit_ref,s.receipt_hash,
        s.request_ref,s.outcome_ref,s.disposition FROM ae_stage_commits s JOIN ae_cycles c ON c.cycle_ref=s.cycle_ref
        WHERE c.quest_ref=:quest ORDER BY c.created_at,s.epoch,s.stage,s.commit_ref""",
        quest,
    )
    for row in rows:
        ref = row["outcome_ref"] if row["disposition"] == "completed" else row["commit_ref"]
        result[row["cycle_ref"], row["stage"]].append(_source(ref, row["stage"] + " outcome", "artifact", **row))
    rows = _rows(
        db,
        """SELECT r.cycle_ref,r.question_ref,r.stage,r.request_ref,p.report_ref AS outcome_ref,p.report_hash
        FROM ar_bundle_reports p JOIN ae_stage_run_requests r ON r.request_ref=p.request_ref
        WHERE r.quest_ref=:quest AND NOT EXISTS (SELECT 1 FROM ae_stage_commits s WHERE s.request_ref=r.request_ref)
        AND p.ordinal=(SELECT MAX(p2.ordinal) FROM ar_bundle_reports p2 WHERE p2.run_ref=p.run_ref)
        ORDER BY r.epoch""",
        quest,
    )
    for row in rows:
        result[row["cycle_ref"], "bundle"].append(
            _source(row["outcome_ref"], "Accepted Bundle report", "artifact", commit_ref=None, **row)
        )
    return result


def _target_bindings(db, quest):
    rows = _rows(
        db,
        """SELECT t.target_ref,t.spec_hash,t.append_ref,a.receipt_hash AS append_hash,g.plan_document_hash,g.graph_ref,g.request_ref,
        g.submission_ref AS initial_ref,g.target_plan_hash AS initial_hash,c.cycle_ref,c.question_ref,
        tc.commit_ref,m.manifest_ref,m.result_document_hash
        FROM rg_targets t JOIN rg_target_graphs g ON g.graph_ref=t.graph_ref
        JOIN ae_cycles c ON c.cycle_ref=g.cycle_ref
        LEFT JOIN rg_target_graph_appends a ON a.append_ref=t.append_ref AND a.graph_ref=g.graph_ref
        LEFT JOIN rg_target_commits tc ON tc.target_ref=t.target_ref
        LEFT JOIN rg_target_root_measurements x ON x.target_ref=t.target_ref AND x.target_run_ref=tc.target_run_ref
        LEFT JOIN rm_target_root_completion_manifests m ON m.manifest_ref=x.manifest_ref AND m.completion_ref=x.completion_ref
        WHERE c.quest_ref=:quest AND g.quest_ref=:quest ORDER BY c.created_at,t.ordinal,t.target_ref""",
        quest,
    )
    notes = defaultdict(list)
    for r in _rows(
        db,
        """SELECT n.target_ref,n.version_ref,n.note_hash FROM rm_target_research_notes n
        JOIN rg_targets t ON n.target_ref=t.target_ref JOIN rg_target_graphs g ON t.graph_ref=g.graph_ref
        WHERE g.quest_ref=:quest ORDER BY n.accepted_at,n.version_ref""",
        quest,
    ):
        notes[r["target_ref"]].append(r)
    result = defaultdict(list)
    for r in rows:
        t = r["target_ref"]
        scope = {"target_ref": t, "spec_hash": r["spec_hash"], "question_ref": r["question_ref"]}
        admission = {
            k: r[k] for k in ("graph_ref", "request_ref", "initial_ref", "initial_hash", "append_ref", "append_hash")
        }
        sources = [
            _source(t, "Target research context", "target", plan_hash=r["plan_document_hash"], **admission, **scope)
        ]
        if r["commit_ref"]:
            sources.append(
                _source(
                    r["commit_ref"],
                    "Accepted Target result and research notes",
                    "result",
                    manifest_ref=r["manifest_ref"],
                    result_hash=r["result_document_hash"],
                    **scope,
                )
            )
        for note in notes[t]:
            sources.append(
                _source(note["version_ref"], "Target research note", "note", note_hash=note["note_hash"], **scope)
            )
        result[r["cycle_ref"]].append((t, sources))
    return result
