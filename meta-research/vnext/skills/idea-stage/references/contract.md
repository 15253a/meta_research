# Idea Stage contract reference

This file is an execution projection for the Skill, not a second source of domain truth. Follow the linked Resolutions when this projection conflicts with them. The Python shapes below are explicit fixtures for testing behavior; they are not production schemas or transport signatures.

## Authority context pointers

| Concern | Authoritative decision |
| --- | --- |
| Stage eligibility, request binding, Foreground Epoch, and `StageCommit` | [#58 Resolution](https://github.com/15253a/meta_research/issues/58#issuecomment-5231669898) |
| Run, root Session, Execution Fence, Owner feedback, recovery, and execution receipt | [#71 Resolution](https://github.com/15253a/meta_research/issues/71#issuecomment-5275550157) |
| Frozen Question lineage and `QuestionLiteratureRevisionRef | none` | [#85 Resolution](https://github.com/15253a/meta_research/issues/85#issuecomment-5280019952) |
| Frozen ContextPack navigation view and Idea input binding | [#100 Resolution](https://github.com/15253a/meta_research/issues/100#issuecomment-5296001855) |
| Exhaustion gates and technical-blocker separation | [#90 Resolution](https://github.com/15253a/meta_research/issues/90#issuecomment-5278826075) |
| Idea purpose, full-set handoff, advisory review, and four-Stage collaboration | [#100 Resolution](https://github.com/15253a/meta_research/issues/100#issuecomment-5296001855) |
| Content custody and formal research semantics | [#66 Resolution](https://github.com/15253a/meta_research/issues/66#issuecomment-5238753918) and [#83 Resolution](https://github.com/15253a/meta_research/issues/83#issuecomment-5227752361) |
| Delivery specification and HITL acceptance criteria only; not a domain Resolution | [#101 delivery issue](https://github.com/15253a/meta_research/issues/101) |

Issue #101 specifies this artifact's delivery and HITL evidence. Until it records its own Resolution and verdict, it does not override or extend the closed domain decisions above.

## Required semantic bindings

A valid invocation binds all of the following without `latest` aliases:

```text
contract_id
stage_run_request_ref
quest_ref
cycle_ref
question_ref
foreground_epoch_ref
run_ref
root_session_ref
execution_fence_ref
context_pack_ref
context_pack_hash
question_literature_revision_ref | none
```

The request, epoch, Run, root Session, fence, Question, and ContextPack hash must still be current when a submission is made. ContextPack is an immutable navigation view over exact Owner refs; it is neither an Owner nor a writable workspace.

The MVP adds explicit booleans such as `current` and `root_fence_current` only to make deterministic fixtures possible. Production code must verify actual receipts and currentness instead of trusting caller booleans.

## IdeaOutcome shapes

### IdeaCandidate

Each candidate carries semantic content, not a score:

```text
candidate_key                   stable only within this submitted outcome
direction                       research claim or investigatory direction
rationale                       mechanism or reason it could answer the Question
assumptions[]
risks[]
evidence_boundary:
  accepted_evidence_refs[]      exact refs
  supported                     what those refs establish
  inferred                      Agent conclusion not accepted as evidence
  unknown                       unresolved boundary
falsification_hint:
  test
  would_refute
material_difference:
  from_history
  from_peers
  plan_commitment_change        why Plan would make a different commitment
```

At least one of accepted evidence, inference, or unknowns must be explicit. `candidate_key` is not a Question identity, selected-Idea identity, or cross-Stage authority.

### IdeaSet

```text
kind = IdeaSet
candidates[1..N]              unique candidate_key; materially distinct
recommendation?:
  note                        optional comparison
  binding = false
```

No global count, rank, score, winner, or selected-candidate field exists. Plan consumes the accepted set as a whole.

### NoViableCandidate

```text
kind = NoViableCandidate
exploration_scope
candidate_families_considered[1..N]:
  family
  why_not_viable
  evidence_refs[]
evidence_boundary:
  accepted_evidence_refs[]
  supported
  inferred
  unknown
overturn_conditions[1..N]
why_plan_cannot_proceed
```

This is an evidence-bounded negative IdeaOutcome. It does not claim that no future Idea can exist, and it does not stand for an empty IdeaSet, reviewer rejection, technical failure, or Stage exhaustion.

## Advisory review record

The initial formal submission of either outcome kind carries one independent review record:

```text
review_ref
reviewer_session_ref
reviewed_draft_hash
findings[]:
  finding_id
  category = question_alignment | material_duplicate | evidence_boundary
           | falsifiability | plan_usability
  message
dispositions[]:
  finding_id
  action = revised | not_adopted
  rationale
```

Every finding has exactly one disposition. If any disposition is `revised`, the bound final outcome hash must differ from the reviewed draft hash; relabeling an unchanged draft as revised fails closed. The Reviewer emits findings; the root Agent owns dispositions and the final revision. A review record has no approval, pass/fail, score, selection, Owner receipt, or submission authority. Internal WildIdea audit remains capability-local and cannot replace this set-level review.

An Owner-rejected outcome does not automatically require a second advisory review. Its successor may instead carry a root-owned revision record:

```text
revision_ref
prior_review_ref
predecessor_submission_ref
owner_rejection_receipt_ref
final_outcome_hash
root_revision_rationale
supplemental_review_ref?       only when the root Agent chose to reconsult
root_owned = true
```

This record is mandatory for a rejection successor and proves revision lineage, not reviewer approval. A fresh `AdvisoryReviewRecord` cannot replace it. A supplemental reviewer remains advisory and appears only as the optional supplemental ref; it cannot replace the root Agent's rationale or the new submission identity.

## Named semantic operations

These names state required meaning and call timing. Their production signatures, transport, persistence, receipt generation, and side effects remain implementation work.

| Semantic operation | Call timing and required result | Implementation marker |
| --- | --- | --- |
| `observe_idea_stage_run(stage_run_request_ref)` | Before reading inputs and immediately before each content, domain, or exhaustion formal write, ask Advancement Engine for the exact current request/epoch/input bindings or a typed stale status. It does not attest Runtime facts. | `TODO-IMPL(AdvancementEngine.observe_stage_run; source=#58,#100)` |
| `observe_run(run_ref, root_session_ref, execution_fence_ref)` | Before each formal write or exhaustion proposal, and after recovery, ask Agent Runtime for exact Run/root Session/current-fence, blocker, and execution facts. It does not attest Stage currentness or domain acceptance. | `TODO-IMPL(AgentRuntime.observe_run; source=#71,#90)` |
| `read_frozen_context_pack(stage_run_request_ref, context_pack_ref, expected_hash)` | Load the exact read-only navigation refs bound to the request and verify the hash; never refresh to a newer pack. Interpret any exact literature-revision ref under #85 without making literature mandatory. | `TODO-IMPL(ContextPackProjection.read_frozen; source=#58,#85,#100)` |
| `accept_idea_outcome_content(submission_id, stage_run_request_ref, run_ref, root_session_ref, execution_fence_ref, outcome, review_record)` | Through the current root fence, ask Research Memory to accept immutable content and provenance. Persist an accepted content ref/receipt immediately as the same-identity domain checkpoint. Preserve its decision receipt for `accepted | rejected | stale | needs_input`; `outcome_unknown` requires a typed observation receipt bound to the original identity. | `TODO-IMPL(ResearchMemory.accept_idea_outcome_content; source=#66,#71,#100)` |
| `reconcile_idea_outcome_content(submission_id, stage_run_request_ref, run_ref, execution_fence_ref)` | After a Research Memory `outcome_unknown`, query the exact original side effect under the same submission identity before any content retry. Return its known decision receipt/status or remain typed unknown. | `TODO-IMPL(ResearchMemory.reconcile_idea_outcome_content; source=#66,#71,#100)` |
| `submit_idea_outcome(submission_id, stage_run_request_ref, run_ref, root_session_ref, execution_fence_ref, content_ref, content_receipt_ref)` | Through the same current root fence, ask Research Graph to accept `IdeaSet | NoViableCandidate` semantics. Preserve its decision receipt for `accepted | rejected | stale | needs_input`; `outcome_unknown` requires a typed observation receipt bound to the original identity. | `TODO-IMPL(ResearchGraph.submit_idea_outcome; source=#71,#83,#100)` |
| `reconcile_idea_outcome(submission_id, stage_run_request_ref, run_ref, execution_fence_ref)` | After `outcome_unknown`, query the exact original side effect and return its known decision receipt/status or remain typed unknown. Never change payload or allocate a replacement identity before reconciliation. | `TODO-IMPL(ResearchGraph.reconcile_idea_outcome; source=#71,#100)` |
| `report_execution_blocker(stage_run_request_ref, run_ref, root_session_ref, execution_fence_ref, blocker)` | Immediately report a typed Runtime/tool/Provider/resource failure through the current Runtime operation and retain its blocker ref/receipt; never translate it into an IdeaOutcome or exhaustion evidence. | `TODO-IMPL(AgentRuntime.transact_run.report_execution_blocker; source=#71,#90)` |
| `submit_exhaustion_proposal(stage_run_request_ref, run_ref, root_session_ref, execution_fence_ref, evidence)` | Only after both IdeaOutcome kinds are unavailable and all closure gates pass; submit from the current root fence and return the Advancement Engine decision/proposal receipt. | `TODO-IMPL(AdvancementEngine.submit_exhaustion_proposal; source=#90)` |
| `reconcile_exhaustion_proposal(proposal_submission_ref, stage_run_request_ref, run_ref, execution_fence_ref, expected_proposal_hash)` | After an Advancement Engine `outcome_unknown`, query that exact proposal side effect before any replacement or resubmit. Require the original request/Run/root Session/fence/ContextPack binding, proposal hash, and non-empty unknown-receipt history. Preserve the unknown receipt and any resolved receipt; a technical reconciliation failure remains a Runtime blocker with the same binding and Advancement reconciliation phase intact. | `TODO-IMPL(AdvancementEngine.reconcile_exhaustion_proposal; source=#71,#90)` |

Call `observe_idea_stage_run` and `observe_run` independently: neither Owner may aggregate or attest the other's currentness. Pass their exact stable bindings to every formal write. The two outcome operations preserve the Research Memory/Research Graph ownership split. A facade may compose them for the Agent, but it must expose both receipts and cannot create a third Owner or aggregate success fact.

## Submission identity and receipt retention

One `submission_id` identifies exactly one immutable normalized closure: its request/Run/root-fence bindings, outcome or content hash, and review-record hash. Apply these rules mechanically:

- reuse the same identity and identical normalized payload only for exact transport replay or reconciliation;
- persist a Research Memory `accepted` content ref and receipt before the domain write; a same-identity resume continues from that checkpoint and must fail closed if the accepted ref or receipt drifts;
- after `outcome_unknown`, reconcile that identity before retrying or allocating another identity;
- if reconciliation itself hits a technical blocker, retain the original unknown observation, its receipt history, and the required reconciliation phase; after Runtime recovery, reconcile before any content/domain resubmit;
- after Owner rejection, preserve the rejected Submission ref, its actual decision receipt, and its review lineage; require a genuinely changed final outcome in the same current Run/root Session, then allocate a new identity linked to all three;
- after `needs_input`, retain its decision and HumanRequest/dependency refs. Resume an unchanged payload under the same identity only after one exact Owner recovery disposition; a changed outcome uses a new identity linked to the predecessor, needs-input receipt, and recovery receipt;
- after a technical failure proven to have produced no external effect, with no pending reconciliation, reuse the identity for unchanged replay or allocate an unlinked new identity for a changed payload; never invent rejection lineage;
- once a stale, needs-input, or definite-no-write technical submission has a replacement feedback-loop head, the older identity remains historical and cannot replay another write;
- allocate a new identity whenever any normalized payload or binding changes; if the frozen contract is no longer current, stop and wait for its Owner rather than rebasing locally;
- retain every content and domain Owner decision receipt, including `rejected`, `stale`, `needs_input`, and resolved unknown outcomes. Keep the two Owners' receipts distinct.
- retain every HumanRequest, Owner recovery disposition receipt, and Runtime blocker ref across recovery; later results append rather than erase those histories.
- treat accepted and rejected results for an identity as immutable. Recoverable stale, needs-input, unknown, and technical states may transition only through their stated replay, recovery, or reconciliation gates, while retaining every earlier receipt.

## Feedback routing

| Observed result | Skill action | Formal effect |
| --- | --- | --- |
| Content and domain `accepted` | Preserve both exact refs and both Owner receipts; offer them to Runtime/Advancement observation without aggregating them. | Candidate for `StageCommit(Completed)` only after separate Runtime and AE gates. |
| Content or domain `rejected` | Preserve the decision receipt and feedback; revise in the same Run/root Session under a new submission identity linked to the rejected Submission. | No Stage outcome. |
| Submission `stale` | Preserve the decision receipt and revalidate the exact request, Run, fence, and inputs. Reuse the identity only for an unchanged exact replay; otherwise allocate a new identity while the frozen contract remains current. | No implicit rebase and no Stage outcome. |
| Request, input, epoch, or fence stale | Stop the old request and surface exact stale bindings. | A new request must come from its Owner; no Stage outcome. |
| `needs_input` | Preserve the decision and dependency/HumanRequest refs; require one exact Owner recovery disposition before unchanged replay or a recovery-linked changed identity. | No Stage outcome. |
| `outcome_unknown` | Require a typed receipt, then reconcile the same submission identity. Preserve both the unknown-observation receipt and any later resolved receipt; a reconciliation blocker does not erase the required reconciliation phase. | No blind repeat and no Stage outcome. |
| Runtime/tool/Provider/resource failure | Report and preserve the typed blocker ref/receipt, then observe Runtime recovery. Definite-no-write recovery permits unchanged same-identity replay or a changed unlinked identity; uncertain effects remain reconciliation-only. | Never an IdeaOutcome or exhaustion basis. |

## ExhaustionProposal evidence

An exhaustion submission contains:

```text
exploration_records[1..N]
prior_submission_refs[]           cite when present
owner_rejection_receipt_refs[]    cite when present
cannot_form_idea_set_reason
cannot_form_no_viable_reason
pending_submission_refs[]         must be empty
accepted_unconsumed_outcome_refs[] must be empty
human_request_refs[]              must be empty
technical_blocker_refs[]          must be empty
outcome_unknown_refs[]            must be empty
existing_stage_commit_ref         must be none
defensible_idea_set_available     must be false
defensible_no_viable_available    must be false
run_reconciled                    must be true
```

No submission, rejection, attempt, or score count is a prerequisite or trigger. Existing Submission refs and rejection receipts are independently mandatory citations only when each exists, and the proposal must match the current Owner/Runtime history observation rather than Agent self-report; a resolved stale Submission does not require or justify a fabricated rejection receipt. Preserve Advancement Engine rejection feedback, stale receipt, or HumanRequest exactly. An unknown proposal result must retain the original request/Run/root Session/fence/ContextPack binding, normalized proposal hash, proposal submission identity, and unknown receipt before reconciliation; it cannot cross an execution binding or manufacture a receipt-free reconciliation state. An Advancement transport failure becomes a typed Runtime blocker. The root Agent proposes; Advancement Engine decides whether the evidence and currentness permit `StageCommit(Exhausted)`.

## Fixture boundary

`scripts/idea_stage_mvp.py` provides deterministic fake ports and booleans solely to demonstrate routing. A fake receipt is labeled as fixture data and has no production authority. The reference proves that the Skill path itself creates no Question, Run, domain acceptance, or `StageCommit`; real authority must arrive through the named operations above.
