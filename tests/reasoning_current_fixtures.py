"""Current Reasoning candidate fixture: accepted summary then native decision."""
from copy import deepcopy

from meta_research.owners.common import AcceptanceReceipt, canonical_hash
from meta_research.reasoning_contract import REASONING_REVIEW_SCHEMA_REF


def summary_candidate_values(runtime, request, checkpoint, *, revised=False):
    for _ in range(30):
        runtime.autonomous_creation.process_once()
        view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)
        if view and view["deepfetch"]["status"] == "queued":
            assert runtime.deepfetch.process_once()
        if view and view["status"] == "awaiting_reasoning_decision":
            break
    view = runtime.autonomous_creation.query(checkpoint.checkpoint_ref)["deepfetch"]
    assert view["status"] == "succeeded"
    output = deepcopy(checkpoint.checkpoint)
    if revised:
        output["scientific_outcome"].update(
            disposition="denied", claim="The accepted summary does not justify the proposed direction.",
            missing_evidence=[],
            evidence=[{"kind": "LiteratureSnapshot", "ref": view["literature_snapshot_ref"], "finding": "negative"}],
        )
    facts = {
        "request_ref": view["request_ref"], "run_ref": view["run_ref"],
        "attempt_ref": view["attempt_ref"], "attempt_generation": view["attempt_generation"],
        "status": view["status"], "snapshot_ref": view["literature_snapshot_ref"],
        "snapshot_hash": view["snapshot_hash"], "context_basis_hash": view["context_basis_hash"],
        "failure_code": view.get("failure_code"),
    }
    ar = runtime.owners.agent_runtime
    run = ar.query_reasoning_stage_run(request.request_ref)
    decision = ar.record_reasoning_autonomous_decision(
        run_ref=run.run_ref, attempt_ref=run.attempt_ref, fence_ref=run.fence_ref,
        native_session_ref=checkpoint.native_session_ref, checkpoint_ref=checkpoint.checkpoint_ref,
        facts=facts, decision={"action": "create", "final_output": output},
    )
    review = {
        "schema_ref": REASONING_REVIEW_SCHEMA_REF,
        "reviewed_draft_hash": checkpoint.checkpoint_hash,
        "final_output_hash": canonical_hash(output),
    }
    receipt = decision["receipt"]
    return {
        "request_ref": request.request_ref, "cycle_ref": request.cycle_ref,
        "foreground_epoch": request.epoch, "context_pack_ref": request.context_pack_ref,
        "context_pack_hash": request.context_pack_hash, "context_pack": request.context_pack,
        "stage_request_receipt": request.receipt, "run_ref": run.run_ref,
        "attempt_ref": run.attempt_ref, "fence_ref": run.fence_ref,
        "submission_ref": "scientific-candidate:" + canonical_hash(checkpoint.checkpoint_ref)[:24],
        "checkpoint_ref": checkpoint.checkpoint_ref, "checkpoint": output, "review": review,
        "checkpoint_receipt": AcceptanceReceipt(
            issuer=receipt["issuer"], kind=receipt["kind"], receipt_ref=receipt["receipt_ref"],
            subject_ref=receipt["subject_ref"], payload_hash=receipt["payload_hash"],
        ),
    }
