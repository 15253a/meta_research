# Stable research identities and formal execution registration

This protocol keeps root-agent research autonomous. Owners register accepted work;
agents do not write individual RG tables or reproduce the retired phase workflow.

## Identity and authority

| Entity | Identity and contents |
| --- | --- |
| Baseline | An explicitly selected `baseline_ref`, or immutable `method_key` + `method_version` + `method_contract` |
| Variant | The selected Baseline and exact recipe/parameters |
| VariantRun | One actual execution, its Variant, exact input bindings, implementation and produced assets |
| ProtocolVersion | Immutable evaluation rules and metric definitions |
| Evaluation / EvaluationAttempt | The accepted Variant/Protocol pairing, and an actual assessment of an existing run |
| MetricResult | Metrics from the actual assessment and its immutable result asset |
| Research note | Free-form interpretation stored as a versioned RM asset; excluded from method identity |

New methods require an explicit identity. Hashes check the consistency of the
selected immutable version. They never decide that independently named methods
should be merged. Discovery returns reuse candidates; selection remains explicit.
Run-specific upstream Commits, temporary paths and research notes belong outside
`method_contract`. An unchanged method can select its existing reference when its
data, implementation or upstream evidence changes.

Historical Baseline bytes and hashes remain unchanged. Their original exact
contract may be replayed, or they may be explicitly selected by `baseline_ref`.
An unseen legacy-style contract cannot register another anonymous Baseline.
Correctable identity errors use the existing Bundle correction mechanism for
both initial graphs and appended proposals. Integrity and stale-source faults
remain errors.

## Root completion

The root completion Owner verifies AR completion, RM manifest, the selected
method/evaluation authority, immutable asset receipts and current bindings.
It then uses the same transaction-local native entity writers as other execution
adapters. Runs, assessments, metrics, completion links and TargetCommit are written
in one fenced RG transaction. A failure rolls back that whole acceptance.

The result document may declare `formal_runs`. Each actual run has a `run_key`;
each actual assessment has an `attempt_key`, its exact accepted evaluation
reference and metrics. Omitting the inventory retains the single-study adapter.
Multiple comparisons produce separate native entities. Tool calls, debugging,
cancelled work and unstarted assessments are not formal runs or results.

An executed run with no completed assessment is still registered. Its exact
completion/manifest association is retained in `rg_target_root_unassessed_runs`.
No EvaluationAttempt, MetricResult or TargetCommit is manufactured. The normal
root correction path returns the registered run references so a later assessment
can reuse the same actual execution. Cross-Target reuse retains the original run
binding while recording the new Target's association.

Checkpoint ownership is explicit: with several actual runs, each selects declared
`checkpoint_paths` (possibly empty). An assessment may select a subset of its own
run's checkpoints. Reusing a run uses its original immutable checkpoint roles;
the current completion cannot add outputs to an older execution.

Repeated submissions return the same Commit or pending-assessment receipt.
Existing run/evaluation references reuse actual facts without incrementing their
counters. Reads reconstruct and verify source facts; existence constraints do not
replace Owner checks of contracts, provenance, receipts, hashes or currentness.

## Historical migration

Migration 0047 adds the explicit method-version registry and accepted correction
codes. Migration 0048 registers actual accepted root work and adds relationship
constraints. Migration 0049 supports versioned historical research statements.

The old root measurement and Commit anchor columns are immutable history.
New `formal_*` references identify realized native entities, and the hashed
`execution_registration_json` records the exact execution status and evidence.
An old accepted result explicitly stating that neither a run nor an assessment
started keeps its assets and historical Commit, with no realized execution refs.
The migration never infers this solely from a metric such as `not_runnable`.
New unexecuted completions do not become measured completion Commits.

## Content and handoff

RM owns immutable code, results, reports and free-form research notes. RG owns
formal definitions and execution/evaluation/metric facts. The completion manifest
and TargetCommit bind the precise versions adopted in that completion.

New root closures use v2. They retain required identities, hashes, receipts,
bindings and summaries, and reference the single immutable result body. Raw-byte
SHA-256 and structured-content hashes remain distinct. Historical v1 closures and
receipts are not rewritten. Verification can reconstruct the same metrics,
inputs, checkpoints and disposition from either representation.

The root skill recommends `outputs/analysis/research-note.md`; its body stays free.
Verified final statements are also saved as RM assets. Successor notes preserve
older versions. Later readers receive bounded summaries and authenticated paths
to exact bodies. Research interpretation cannot substitute for inputs or metrics.

Current contracts and selected inputs are never removed by optional context
budgets. Idea, Plan, Bundle and Reasoning optional views have a 64 KiB budget,
selection rules and paging. Target notes bound the selected sources and summary
lengths. These are not limits on the entire prompt or native model history.

## Observability

Signed Provider observation sidecars record the actual sent prompt size, section
sizes, reported usage and cache usage, observed compression markers, and Owner
query timing. A reported cumulative Provider turn is labelled as such. It is not
presented as a single model call. The invocation's trusted Codex home and exact
native session ID can locate that session's rollout; its metadata must agree.
Signed observations retain a bounded prefix hash, source offsets, unique response
usage records and separate last-usage snapshots. They describe the sampled session
prefix, not the current invocation's total usage. Retained compaction events survive
temporary stdout cleanup; encrypted replacement blocks are not readable summaries.
Missing native records retain the stdout fallback. SQL execution counts are not
row counts.

Deployment and real-data evidence are kept in the workspace's
`data-protocol-8767-20260914` directory. Service 8767 and its current data root are
the only active deployment target for this change.
