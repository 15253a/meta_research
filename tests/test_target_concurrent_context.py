"""A concurrent accepted Bundle head cannot rewrite an admitted Target's input."""
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import event, text

from meta_research.bundle_target_contract import FORMAL_STRATEGY_UPDATE_SCHEMA_REF
from meta_research.owners.common import canonical_hash
from test_public_bundle_stage import (
    _TwoGapPlanSkill, _bundle_runtime, _confirm_direct_quest,
    _finish_idea_stage, _finish_plan_stage, _formal_candidate,
)
from test_target_root_finalizer import _CurrentBindingBundleSkill


def test_target_context_pins_real_read_cut_while_another_owner_accepts_new_generation(tmp_path):
    runtime = _bundle_runtime(tmp_path / "concurrent-target-context",
        plan_skill_provider=_TwoGapPlanSkill(), bundle_skill_provider=_CurrentBindingBundleSkill())
    try:
        _confirm_direct_quest(runtime)
        _finish_idea_stage(runtime)
        _finish_plan_stage(runtime)
        graph = None
        for _ in range(12):
            assert runtime.bundle_stage.process_once()
            current = runtime.bundle_stage.query_current()
            if current["stage_run_request"] is not None:
                request_ref = current["stage_run_request"]["request_ref"]
                graph = runtime.owners.research_graph.query_target_graph(request_ref)
                if graph is not None:
                    break
        assert graph is not None
        run = runtime.owners.agent_runtime.query_bundle_stage_run(request_ref)
        rg, ar = runtime.owners.research_graph, runtime.owners.agent_runtime
        key = "compare-artifact-boundary"
        spec = _formal_candidate(completion_document=graph.target_plan["completion_contract"],
            label="concurrent-context-followup", experiment_key=key,
            cell=f"measurement:{key}", depends_on=())

        def propose(base, candidates, notes, complete, idempotency_key):
            return ar.record_bundle_target_proposal(run_ref=run.run_ref,
                attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
                native_session_ref=run.native_session_ref, graph_ref=base.graph_ref,
                base_generation=base.head_generation, base_head_receipt=base.head_receipt,
                strategy_update={"schema_ref": FORMAL_STRATEGY_UPDATE_SCHEMA_REF,
                    "revision": base.head_generation + 2, "candidates": candidates,
                    "requires_accepted_labels": [], "strategy_complete": complete, "notes": notes},
                inbox_checkpoint=ar.query_bundle_inbox_checkpoint(run.run_ref),
                idempotency_key=idempotency_key)

        def append(proposal):
            return rg.append_target_batch(graph_ref=graph.graph_ref,
                proposal_ref=proposal.proposal_ref, proposal=proposal.proposal,
                proposal_hash=proposal.proposal_hash, proposal_receipt=proposal.receipt)

        first = propose(graph, [spec], "ADMITTED: retain failed controls and unmeasured limits.",
                        False, "admitted-context-proposal")
        assert append(first).generation == 1
        graph = rg.query_target_graph(request_ref)
        target = next(target for target in graph.targets
                      if target.spec["candidate"]["local_label"] == "concurrent-context-followup")
        rg.accept_formal_plan_content(formal_plan_ref=graph.formal_plan_ref,
                                      idempotency_key="concurrent-context-plan-content")
        # This is a real AR-issued proposal, still unaccepted when the read starts.
        later = propose(graph, [], "LATER: unrelated current strategy must stay outside this Target.",
                        True, "later-context-proposal")
        owner, database = runtime.target_run_authorities.research_graph, runtime._database
        reading_directory = tmp_path / "target-reading"
        expected = owner.query_target_reading_context(target_ref=target.target_ref,
            research_note_directory=reading_directory)
        entered, committed = threading.Event(), threading.Event()
        reader_thread = threading.get_ident()
        observed_cut = []
        nested_reads = []

        def writer():
            assert entered.wait(30), "reader did not enter its first actual SELECT"
            try:
                head = append(later)
                assert head.generation == 2
                return head
            finally:
                committed.set()

        def pause_after_first_read(_connection, _cursor, statement, _parameters, _context, _many):
            if threading.get_ident() == reader_thread and entered.is_set() and any(
                    table in statement for table in ("rg_target_input_asset_role_proofs",
                        "rm_target_research_notes", "rm_target_root_completion_manifests")):
                generation = _connection.execute(text(
                    "SELECT MAX(generation) FROM rg_target_graph_appends WHERE graph_ref=:ref"
                ), {"ref": graph.graph_ref}).scalar_one()
                nested_reads.append((statement, _connection, generation))
            if (threading.get_ident() == reader_thread and not entered.is_set()
                    and "SELECT t.graph_ref, t.spec_json" in statement):
                observed_cut.append(database._read_cut.get())
                assert observed_cut[-1] is not None
                entered.set()
                assert committed.wait(30), "concurrent writer could not commit under WAL"

        event.listen(database._engine, "after_cursor_execute", pause_after_first_read)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                accepted = pool.submit(writer)
                actual = owner.query_target_reading_context(target_ref=target.target_ref,
                    research_note_directory=reading_directory)
                new_head = accepted.result(timeout=30)
        finally:
            event.remove(database._engine, "after_cursor_execute", pause_after_first_read)
        assert observed_cut and new_head.generation == 2
        assert all(any(table in statement for statement, _, _ in nested_reads) for table in (
            "rg_target_input_asset_role_proofs", "rm_target_research_notes", "rm_target_root_completion_manifests"))
        assert all(connection is observed_cut[0] and generation == 1
                   for _, connection, generation in nested_reads)
        assert rg.query_target_graph(request_ref).head_generation == 2
        assert actual == expected
        assert actual["bundle_notes"] == "ADMITTED: retain failed controls and unmeasured limits."
        assert actual["bundle_notes_source_ref"] == first.proposal_ref
        assert actual["bundle_notes_source_hash"] == canonical_hash(first.proposal)
        assert actual["bundle_notes_text_hash"] == hashlib.sha256(actual["bundle_notes"].encode()).hexdigest()
        assert owner.query_target_reading_context(target_ref=target.target_ref,
            research_note_directory=reading_directory) == expected
        assert database._read_cut.get() is None
    finally:
        runtime.close()
