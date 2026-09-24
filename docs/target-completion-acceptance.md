# Target completion acceptance

An executed Target must be able to publish its original result without rebuilding
the admission history of every already accepted upstream Commit. Accepted reads
authenticate the persisted closure, exact identities, hashes and Owner receipts;
the first acceptance of new research still validates its research context.

Running-frontier discovery uses the accepted launch and dispatch receipts and
retains current Stage, Bundle lineage and exact completion-transition checks.
It does not repeat candidate admission on each background wake.

The frozen input tree remains the exact accepted input material. Its bytes and
shape, and the regular-file `inputs/manifest.json` pointer, are checked before a
new completion snapshot is accepted. Agent-created analysis copies or caches
beside that pointer are local workspace content, not additions to frozen custody.

An exact completion with an accepted RM manifest reuses that immutable snapshot.
Replay must still match the handle, Harness operation and evidence, and preserve
issuer receipts; it does not reopen current input/output files. An AR-only retry
without an accepted manifest still checks inputs before accepting a new snapshot.

The September 16 Target 3 repair reproduced both repeated graph admission and a
workspace-only cache incorrectly rejected as frozen-input drift. An isolated
replay of the original Harness completion, 19.6 GB of frozen inputs and 3.15 GB of
outputs produced one completion, manifest and Commit in 48.16 seconds. Exact
replay returned the same identities in 4.21 seconds, with Provider subprocesses
forbidden and the original result bytes unchanged. These timings are from the
private memory-backed replay, not a production storage throughput guarantee.

Regression coverage includes accepted Commit read cost and corrupt receipts,
admitted frontier discovery, completion replay, pointer and frozen-input
tampering, and legitimate local workspace files. Existing formal-entity,
checkpoint, large-artifact, completion and handoff checks cover the downstream
boundaries. Historical stale test expectations are recorded in deployment
evidence, not counted as successful checks.

The subsequent Target 4 launch exposed a separate storage failure: the live
filesystem rejected an exact upstream asset export with EDQUOT. The same real
inputs reached the first execution dispatch in an isolated filesystem. A
destination write failure must not be relabeled as corrupt accepted evidence or
trigger retries of alternative source copies. RM reports
`asset_export_destination_unavailable`; Target input delivery reports
`target_run_workspace_input_storage_unavailable`, retaining the original error
cause. A source that becomes unavailable still permits another receipt-bound
source to supply the exact verified bytes.

Both the frozen metadata and workspace pointer are published only after a
complete staged write. An interrupted write leaves no partially published
manifest; the same launch can resume once storage is available. Published
metadata must still match exactly. Worker health retains a failed Target's
error while its retry is pending, regardless of another Target's progress. It
clears that error only after the same Target returns normally or leaves the
authoritative work inventory.

Accepted native artifact readback batches rows by execution subject and reads
checkpoint order once per evaluation, then performs the same exact field checks.
This prevents query amplification; it does not solve storage exhaustion. Neither
process readiness nor a successful memory-backed replay proves live workflow
recovery: the original Target must actually enter execution on persistent storage.
