# Bundle contract

## Frozen input

Use the exact Bundle ContextPack named by `context_pack_ref` and
`context_pack_hash`. Verify that its Question binding, FormalPlan identity,
PlanDocument hash, content receipt, domain receipt, and Plan StageCommit receipt
agree. Do not replace any binding with a latest lookup.

If the frozen GapSet is empty, do not run this Skill. Advancement Engine records
the typed `no Bundle Run / skipped` disposition.

## TargetPlan

Return these top-level fields only:

- `schema_ref`: `meta-research/target-plan/v1`
- `kind`: `TargetPlan`
- exact `formal_plan_ref` and `context_pack_ref`
- `targets`: one or more complete TargetSpec candidates
- `source_bindings`: exact FormalPlan, PlanDocument, and ContextPack hashes

Each TargetSpec must name a unique `target_key`, one source
`experiment_key`, its exact gap obligations, dependency keys, goal,
hypothesis, numeric variant parameter, sample count, boundary constraints,
semantic delta, contributing Idea refs, and risk class. Preserve the source
brief's goal, gaps, constraints, semantic delta, and Idea refs verbatim.

The union of TargetSpec gap obligations must equal the Plan GapSet. Every
ExperimentBrief must be represented. Dependencies must refer to targets in the
same plan and form a DAG.

## Semantics

Target is a durable Research Graph identity. TargetRun is a durable Agent
Runtime execution identity. Root and child Agent Sessions are Harness
implementation details. These three identity spaces never substitute for one
another.

Negative, zero, nonsignificant, denied, and uncertain valid results are realized
outcomes, not technical failures. Authentication, authorization, resources,
currentness, or missing receipts block only the affected target. Request
`replan_required` only when frozen semantics must change.
