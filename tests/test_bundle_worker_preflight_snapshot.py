"""The worker's two preflight readers reuse proofs, then release all effects."""
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from meta_research.bundle_stage import BundleStageWorker
from meta_research.database import Database
from meta_research.owners.common import OwnerConflict
from meta_research.read_snapshot_cache import snapshot_cached


class _ProofOwner:
    def __init__(self, database):
        self._database = database
        self.observed_revisions = []

    @snapshot_cached
    def verify_upstream(self):
        with self._database.read() as connection:
            revision = connection.execute(text("SELECT revision FROM proof_source")).scalar_one()
        self.observed_revisions.append(revision)
        return revision


def _worker(tmp_path, request_exists=True):
    database = Database(tmp_path / "preflight.sqlite3")
    with database.write() as connection:
        connection.exec_driver_sql("CREATE TABLE proof_source (revision INTEGER)")
        connection.exec_driver_sql("INSERT INTO proof_source VALUES (1)")
    proof = _ProofOwner(database)
    question_binding = NS(as_dict=lambda: {"question_ref": "question"})
    current = NS(cycle_ref="cycle", question=NS(
        quest_ref="quest", as_binding=lambda: question_binding
    ))
    formal_plan = NS(plan_document={"gap_set": []}, as_dict=lambda: {"formal_plan_ref": "plan"})
    eligible = NS(accepted_formal_plan=lambda: formal_plan, accepted_idea_set=None)
    request = NS(request_ref="request", accepted_formal_plan=formal_plan)
    foreground = {"cycle_ref": "cycle", "stage": "bundle", "status": "active", "epoch": 1}

    def qualify(_current):
        proof.verify_upstream()
        return eligible, None, "Bundle"

    def read_request(_cycle_ref):
        # The real AE reader opens its own nested snapshot. This is the caller
        # seam where the same upstream proof was previously repeated.
        with database.read_snapshot():
            proof.verify_upstream()
            return request if request_exists else None

    def after_preflight(*_args, **_kwargs):
        assert database._read_cut.get() is None
        assert database._read_cache.get() is None

    def ensure(**_kwargs):
        after_preflight()
        with database.write() as connection:
            connection.exec_driver_sql("UPDATE proof_source SET revision = revision + 1")

    engine = NS(
        query_foreground=Mock(return_value=foreground),
        query_bundle_stage_request=Mock(side_effect=read_request),
        query_bundle_stage_commit=Mock(return_value=object()),
        ensure_bundle_stage_request=Mock(side_effect=ensure),
    )
    worker = object.__new__(BundleStageWorker)
    worker._database = database
    worker._provider = object()
    worker._worker_cursor_cycle_ref = None
    worker._agent_runtime = NS(reconcile_pending_provider_cleanup=Mock(return_value=False))
    worker._advancement_engine = engine
    worker._discover_active_cycles = Mock(return_value=(current,))
    worker._qualify_from_current = qualify
    worker._assert_request = after_preflight
    return worker, database, proof, engine


def test_worker_shares_proof_within_boundary_and_rechecks_next_tick(tmp_path):
    worker, database, proof, _engine = _worker(tmp_path)
    try:
        assert worker._process_boundary_once() is False
        assert proof.observed_revisions == [1]
        with database.write() as connection:
            connection.exec_driver_sql("UPDATE proof_source SET revision = 2")
        assert worker._process_boundary_once() is False
        assert proof.observed_revisions == [1, 2]
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        database.close()


def test_missing_request_keeps_ensure_write_outside_read_snapshot(tmp_path):
    worker, database, proof, engine = _worker(tmp_path, request_exists=False)
    try:
        assert worker._process_boundary_once() is True
        assert proof.observed_revisions == [1]
        engine.ensure_bundle_stage_request.assert_called_once()
        with database.read() as connection:
            assert connection.execute(text("SELECT revision FROM proof_source")).scalar_one() == 2
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        database.close()


def test_ineligible_cycle_does_not_read_request_or_keep_snapshot(tmp_path):
    worker, database, _proof, engine = _worker(tmp_path)
    worker._qualify_from_current = lambda _current: (None, "not_eligible", "Plan")
    try:
        assert worker._process_boundary_once() is False
        engine.query_bundle_stage_request.assert_not_called()
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        database.close()


@pytest.mark.parametrize("failing_read", ["qualification", "request"])
def test_preflight_rejection_is_preserved_and_releases_snapshot(tmp_path, failing_read):
    worker, database, _proof, engine = _worker(tmp_path)
    def reject(*_args):
        raise OwnerConflict("upstream_proof_invalid")
    if failing_read == "qualification":
        worker._qualify_from_current = reject
    else:
        engine.query_bundle_stage_request.side_effect = reject
    try:
        with pytest.raises(OwnerConflict, match="upstream_proof_invalid"):
            worker._process_boundary_once()
        engine.ensure_bundle_stage_request.assert_not_called()
        assert database._read_cut.get() is None and database._read_cache.get() is None
    finally:
        database.close()
