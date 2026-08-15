---
name: idea-stage
description: Form a reviewed, evidence-bounded IdeaOutcome for a current typed Idea StageRunRequest.
---

# Idea Stage

Form a Plan-consumable `IdeaSet` or an evidence-bounded `NoViableCandidate` from one current, frozen Idea `StageRunRequest`. Act as the root Idea Agent: own the research synthesis and every review disposition while leaving content custody, domain acceptance, execution, and Stage advancement with their actual Owners.

Read [the contract reference](references/contract.md) before validating an invocation, shaping an outcome, interpreting Owner feedback, or proposing exhaustion. Treat its fixture shapes as executable examples rather than production signatures.

## Validate the run envelope

1. Require an explicit skill invocation carrying a typed Idea `StageRunRequest`. Bind the exact request, Quest, Cycle, Question, Foreground Epoch, Run, current root Execution Fence, ContextPack ref/hash, contract revision, and `QuestionLiteratureRevisionRef | none`.
2. Verify the request and fence are current through their named semantic operations. Verify the ContextPack is immutable and matches its bound hash. Resolve every consumed input by exact ref; retain `none` when no literature revision exists.
3. Fail closed with the returned typed status when any identity, hash, authority, currentness, or fence is unknown. Ordinary research chat, mutable input, and `latest` aliases never establish an Idea run.

The envelope is ready only when every input is exact, frozen, readable under the current binding, and traceable to the request.

## Form the IdeaOutcome

1. Read the frozen Question first, then progressively load only relevant ContextPack refs: prior candidate outcomes and dispositions, applicable evidence and conclusions, literature observations, constraints, and known failure boundaries.
2. Explore autonomously inside the current Run. Synthesize directly, use ordinary tools, and invoke WildIdea or child agents when useful. Keep their products advisory until the root Agent incorporates and submits them.
3. Separate accepted evidence, Agent inference, and unresolved unknowns. Preserve exact evidence refs and state what each ref does and does not support.
4. Merge semantic duplicates. Keep candidates separate only when the difference would cause Plan to make a different research commitment; names, prose, and ordinary parameter changes do not establish distinctness.
5. Form exactly one outcome kind:
   - `IdeaSet`: one or more materially distinct candidates. Let Plan consume the full set and independently select, combine, or discard candidates. A recommendation may be advisory only.
   - `NoViableCandidate`: a bounded negative outcome that explains why the frozen Question and constraints currently leave no responsible candidate for Plan. State explored scope, candidate families, failure reasons, evidence/inference/unknown boundaries, overturn conditions, and why Plan cannot proceed.

Use the complete shapes in the contract reference. Do not turn an empty list, a rejected batch, a score threshold, or an execution problem into either outcome.

## Run the independent challenge

Before the first formal submission of either outcome kind:

1. Give one independent advisory reviewer the frozen Question bindings and the complete deduplicated draft.
2. Ask only for findings about Question alignment, material duplication, evidence boundaries, falsifiability, and Plan usability.
3. Disposition every finding as `revised` or `not_adopted`, with a reason. Revise the outcome for every adopted finding.
4. Submit the root Agent's final revision together with the review record. Completion is full disposition coverage, not reviewer approval.

After Owner rejection, continue in the same Run and root Session while the frozen contract remains current. Require a root-owned `OwnerFeedbackRevisionRecord` bound to the prior review, rejected Submission, decision receipt, and new final-outcome hash; a fresh advisory review cannot replace that lineage. Consult an advisory reviewer again when useful and record only its supplemental review ref; do not require a second review or create a fixed review loop or approval gate.

## Submit and consume feedback

Submit only through the current root Execution Fence and the named content/domain operations in the contract reference. Immediately before each content or domain formal write, re-observe Advancement Engine and Agent Runtime currentness independently and pass the same exact Run/root Session/fence bindings to that Owner. Allocate one submission identity for one immutable normalized payload and binding set. Reuse that identity only for exact replay, reconciliation, or an unchanged payload after a verified `needs_input` recovery; when Owner feedback changes the payload, allocate a new identity with the applicable rejection or recovery lineage while remaining in the same current Run/root Session. Once a replacement becomes the feedback-loop head, an older recoverable identity is historical and cannot write again.

Route the Owner result without reinterpretation:

- `accepted`: persist an accepted Research Memory content ref and receipt as a checkpoint before attempting the Research Graph write. Retain that checkpoint, the immutable outcome ref, and separate content and domain Owner receipts for Runtime completion and Advancement Engine observation.
- `rejected`: retain the exact decision receipt and structured feedback, revise in the same Run/root Session, and submit the revised immutable payload under a new identity linked to the rejected Submission.
- `stale`: retain the exact decision receipt and revalidate the request, Run, fence, and input bindings. Reuse the identity only when the normalized payload and bindings are unchanged; otherwise allocate a new identity. Stop the old request when its frozen contract is no longer current.
- `needs_input`: retain the decision receipt and the Owner's precise dependency or HumanRequest ref, then wait for one exact Owner recovery disposition. Preserve the HumanRequest and recovery receipts in history. After recovery, reuse the identity only when the payload is unchanged; a changed payload uses a new identity linked to the prior Submission, needs-input receipt, and recovery receipt.
- `outcome_unknown`: require and retain a typed unknown-observation receipt, then call the reconciliation operation with the same submission identity before any retry or replacement identity. If reconciliation itself hits a technical blocker, preserve the original unknown observation and resume with reconciliation—not a new write—after Runtime recovery.
- `technical_blocker`: call the named Runtime blocker-report operation, retain every typed blocker ref/receipt, and use Runtime observation for recovery. When the failure is proven to have produced no external effect and no reconciliation remains, exact unchanged replay uses the same identity while a changed payload uses a new identity without fabricated rejection lineage.

Content acceptance alone does not establish domain acceptance. Domain acceptance alone does not advance the Stage.

## Propose exhaustion

Propose `ExhaustionProposal` only after all of these are true:

- neither an `IdeaSet` nor a defensible `NoViableCandidate` can be formed or accepted under the frozen contract;
- exploration records explain why no materially distinct, potentially acceptable outcome remains;
- existing Submission refs and Owner-rejection receipts are each cited when they exist and match the live Owner/Runtime history observation; a resolved stale history does not invent a rejection receipt;
- no Submission awaits judgment, no HumanRequest or technical blocker remains, and no external outcome is unknown;
- the Run is reconciled and ready for Runtime completion.

Submit the proposal through the Advancement Engine operation in the contract reference. Preserve typed `rejected`, `stale`, and `needs_input` details; reconcile an `outcome_unknown` only under its exact proposal submission identity, original request/Run/root/fence/ContextPack binding, proposal hash, and unknown receipt; report a technical failure to Runtime without converting it to exhaustion. Advancement Engine alone verifies currentness and may form `StageCommit(Exhausted)`. A proposal never proves that the Question has no answer.

## Finish at the authority boundary

Finish the Idea work with one of these observable states:

- an accepted `IdeaSetRef` or `NoViableCandidateRef` plus the actual Owner receipt;
- an `ExhaustionProposalRef` submitted for Advancement Engine judgment;
- a typed `rejected | stale | needs_input | outcome_unknown | technical_blocker` state that keeps the Run honest and recoverable.

Preserve the invariant:

```text
execution completed != domain accepted != Stage advanced
```

The Skill does not create or modify a Question, issue a Run, accept its own outcome, select a canonical Idea, or form a `StageCommit`.

## Executable reference

- Run `python vnext/skills/idea-stage/scripts/idea_stage_mvp.py --scenario idea-set` for a deterministic accepted-path trace.
- Run `python vnext/skills/idea-stage/scripts/test_idea_stage_mvp.py` for the fixture contract tests.
- Read [the contract reference](references/contract.md) for semantic shapes, feedback routing, context pointers, and production implementation stubs.
