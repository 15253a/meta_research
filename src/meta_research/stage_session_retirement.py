"""Former native roots of completed Stage work, selected from its Owner records."""
from sqlalchemy import bindparam, text

from meta_research.owners.agent_runtime import FORMAL_STAGES, verified_retired_stage_session
from meta_research.owners.common import OwnerConflict


def retired_stage_ids(connection, work) -> set[str]:
    runs = [run for run, kind in work.items() if kind in FORMAL_STAGES]
    if not runs:
        return set()
    # A genuine native replacement records the retired attempt, fence revocation
    # and exact provider checkpoint together. Other safe points are not evidence
    # that this run owned a particular native thread.
    records = connection.execute(text("""
        SELECT old.run_ref, old.root_session_ref, old.attempt_ref, old.fence_ref,
               point.checkpoint_json, point.checkpoint_hash
        FROM ar_stage_attempt_replacements x
        JOIN ar_stage_runs r ON r.run_ref=x.run_ref AND r.status='completed'
        JOIN ar_stage_attempts old ON old.attempt_ref=x.retired_attempt_ref
            AND old.run_ref=r.run_ref AND old.root_session_ref=r.root_session_ref
        JOIN ar_fence_revocations v ON v.fence_ref=old.fence_ref
            AND v.run_ref=r.run_ref AND v.attempt_ref=old.attempt_ref
            AND v.reason_code='native_session_unavailable'
        JOIN ar_safe_points point ON point.operation_ref=v.operation_ref AND point.run_ref=r.run_ref
            AND point.attempt_ref=old.attempt_ref AND point.fence_ref=old.fence_ref
            AND point.root_session_ref=r.root_session_ref
        WHERE x.run_ref IN :runs AND x.reason_code='native_session_unavailable'
    """).bindparams(bindparam("runs", expanding=True)), {"runs": runs})
    retired = set()
    for row in records:
        try:
            retired.add(verified_retired_stage_session(row))
        except OwnerConflict:
            continue
    if retired:
        # If another root adopted the ID it still belongs to that root.
        attached = connection.execute(text("SELECT native_session_ref FROM ar_stage_sessions "
            "WHERE native_session_ref IN :ids").bindparams(bindparam("ids", expanding=True)), {"ids": tuple(retired)})
        retired.difference_update(attached.scalars())
    return retired
