---
name: bundle-stage
description: Turn an accepted FormalPlan GapSet into a reviewed Target DAG while preserving Owner boundaries and durable target lineage.
---

# Bundle Stage

Consume only the exact `AcceptedFormalPlanBinding` in the supplied Bundle
ContextPack. Treat the Plan's GapSet, ExperimentBriefs, AnswerContract, and
Evidence reuse decisions as frozen inputs.

Produce one complete `TargetPlan` whose targets close every gap through the
smallest useful acyclic graph. Deduplicate equivalent work, state dependencies,
and distinguish normal from high-risk targets. Keep each TargetSpec executable
by the declared micro-experiment capability.

Use one managed native root Session for strategy. Spawn one short-lived,
fresh-context child reviewer inside that Session to check lineage, DAG validity,
deduplication, feasibility, and Owner boundaries. Dispose every finding as
`revised` or `not_adopted`; the review is advisory.

Do not create Target identities, TargetRuns, TargetCommits, StageCommits,
receipts, or acceptance decisions. Never use Agent Session identities as Target
or TargetRun identities. Return only the structured TargetPlan and review
envelope requested by the adapter.

Read [references/contract.md](references/contract.md) before drafting. Consult
[references/owner-operations.md](references/owner-operations.md) whenever an
operation might cross an Owner boundary.

Completion means the final TargetPlan passes the output schema, closes the
frozen GapSet, preserves exact ExperimentBrief semantics, is acyclic, and has a
complete independent-review disposition ledger.
