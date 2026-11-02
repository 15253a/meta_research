"""Locate retired Target threads from the eligible Run's recorded Harness events."""
import json
import re

from sqlalchemy import bindparam, text

from meta_research.owners.agent_runtime_harness import (
    AgentRuntimeHarnessError, _target_root_scope_matches, _validated_target_root_event,
)
from meta_research.owners.common import OwnerConflict, canonical_hash, canonical_json


def _matches_event(row, expected_scope):
    event = json.loads(row.summary_json)
    if row.kind == "thread.started":
        # These are ordinary Harness evidence events, separate from command observations.
        if (event.get("kind") != "thread.started" or event.get("native_session_ref") != row.native
                or type(event.get("sequence")) is not int or event["sequence"] < 1
                or not isinstance(event.get("event_ref"), str)
                or not re.fullmatch(r"harness_evidence:[0-9a-f]{64}", event["event_ref"])
                or not _target_root_scope_matches(event.get("target_run_scope"), expected_scope)):
            return False
        identity = (event["event_ref"], event["sequence"], canonical_json(event), canonical_hash(event))
    else:
        identity = _validated_target_root_event(event, expected_scope=expected_scope,
                                                expected_native_session_ref=row.native)[:4]
    return identity == (row.event_ref, row.sequence, row.summary_json, row.summary_hash)


def replaced_target_sessions(connection, work, target_agent) -> set[str]:
    targets = [run for run, kind in work.items() if kind == "target"]
    if not targets:
        return set()

    # A turn may perform no shell work. A root start and a command observation
    # are both durable bindings. Any disagreement within the operation disqualifies it.
    rows = connection.execute(text("""
        WITH events AS (
            SELECT p.run_ref, p.operation_ref, e.event_ref, e.sequence, e.summary_json, e.summary_hash,
                json_extract(e.summary_json,'$.kind') AS kind,
                CASE WHEN json_extract(e.summary_json,'$.kind')='thread.started'
                     THEN json_extract(e.summary_json,'$.target_run_scope')
                     ELSE json_extract(e.summary_json,'$.target_root_observation.scope') END AS scope,
                CASE WHEN json_extract(e.summary_json,'$.kind')='thread.started'
                     THEN json_extract(e.summary_json,'$.native_session_ref')
                     ELSE json_extract(e.summary_json,'$.target_root_observation.root_native_session_ref') END AS native
            FROM ar_harness_evidence_events e JOIN ar_harness_provider_operations p USING(operation_ref)
            JOIN ar_harness_runs h ON h.run_ref=p.run_ref AND h.harness_family='codex'
            WHERE p.run_ref IN :runs AND p.status IN ('executed','failed')
              AND (json_extract(e.summary_json,'$.kind')='thread.started'
                   OR json_type(e.summary_json,'$.target_root_observation')='object')
        ), bindings AS (
            SELECT * FROM events GROUP BY operation_ref
            HAVING count(scope)=count(*) AND count(native)=count(*)
               AND count(DISTINCT scope)=1 AND count(DISTINCT native)=1
        )
        SELECT b.*, w.* FROM bindings b JOIN ar_target_run_workspaces w
          ON w.target_run_ref=b.run_ref AND w.root_session_ref=json_extract(b.scope,'$.root_session_ref')
           AND w.target_attempt_ref=json_extract(b.scope,'$.attempt_ref')
           AND w.target_fence_ref=json_extract(b.scope,'$.fence_ref')
        WHERE NOT EXISTS (SELECT 1 FROM ar_harness_runs attached WHERE attached.native_session_ref=b.native
                          AND attached.run_ref!=b.run_ref)
        """).bindparams(bindparam("runs", expanding=True)), {"runs": targets})
    ids = set()
    for row in rows:
        try:
            target_agent._stored_target_workspace(row, require_current=False)
            scope = json.loads(row.scope)
            if (scope.get("target_run_ref") != row.run_ref or type(scope.get("attempt_generation")) is not int
                    or scope["attempt_generation"] < 1 or scope.get("target_workspace_ref") not in (None, row.workspace_ref)):
                continue
            if _matches_event(row, {**scope, "native_session_ref": row.native}):
                ids.add(row.native)
        except (AgentRuntimeHarnessError, OwnerConflict, KeyError, TypeError, ValueError):
            continue
    return ids
