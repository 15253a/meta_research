# Research guidance confirmation

Implements [#233](https://github.com/15253a/meta_research/issues/233), against
`v1-test@a22a73d6f0785f676f01af83ac18d12488ae2906`. The right-sidebar UI in
[#228](https://github.com/15253a/meta_research/issues/228) must use the confirmation
contract below.

## Reviewed content

An explanatory Companion reply creates no research input. When the person wants
to influence research, the proposal preserves these fields together:

```json
{
  "proposal_kind": "soft_constraint",
  "text": "本次实验严格只换学习率，总目标不变。",
  "assistant_understanding": "只调整所选实验的学习率，保留整体目标。",
  "applies_to": ["仅所选实验"],
  "semantic_scope": {
    "kind": "target",
    "quest_ref": "<quest>",
    "question_ref": "<question>",
    "cycle_ref": "<cycle>",
    "target_ref": "<target>"
  },
  "strength": 5,
  "preserve_conditions": ["总目标不变"],
  "work_materials": null
}
```

`work_materials`, when present, is part of the same reviewed proposal. Selecting
materials does not establish that they were read, adopted, or retained in RM.
Original text and assistant understanding remain separately readable.

The Quest archive, Companion reading context, exact material receiver and
`semantic_scope` have separate meanings. The confirmed scope uses existing
research identities: Quest, Question, Cycle or Target. Question scope continues
within that Question's subsequent Cycles; Cycle scope stays in that Cycle;
Target scope stays with that Target. The assistant proposes these identities
from the person's words and context. An ambiguous scope needs clarification.
The person need not fill in a new hierarchy form.

## Explicit confirmation

Confirmation uses the existing proposal conversion operation with the exact
`proposal_ref`, `expected_scope_ref`, `expected_proposal_hash`, strength and a
stable idempotency key. Editing original text, understanding, scope, strength,
preserved conditions or materials creates a newly reviewed version and retires
the previous proposal. A stale confirmation cannot authorize that new content.
An exact retry returns the original effect.

HTTP integration points (all require the existing authenticated session):

| Operation | Request |
| --- | --- |
| `POST /api/v1/companion/messages` | Existing `message` and `view_context`, plus optional `guidance_options: {strength, work_materials}`. The turn retains the person's selection; strength defaults to 3. |
| `POST /api/v1/human-collaboration/agent-proposals/{ref}/revisions` | `expected_scope_ref`, `expected_proposal_hash`, and the complete revised `proposal`. Returns the new proposal reference/hash; the old proposal becomes dismissed. |
| `POST /api/v1/human-collaboration/agent-proposals/{ref}/soft-constraint` | `expected_scope_ref`, `expected_proposal_hash`, and matching `strength`. Returns the reviewed proposal and formal soft constraint. |

Use the existing snapshot/Companion projection to display the complete proposal
and its hash. Every mutation uses the existing `Idempotency-Key` header. A
mismatched confirmed strength returns `guidance_confirmation_stale`; missing
understanding or scope returns `guidance_confirmation_required`; a retired
proposal returns `agent_proposal_stale` (HTTP 409). These are conflicts to show to
the person, not permission to resubmit changed content under an old confirmation.

The direct guidance submission endpoint also requires the reviewed proposal
reference and hash; a free-text submission alone cannot bypass confirmation.
HumanRequest responses continue through their original, separate formal
submission contract.

## Research consumption and target alignment

Operation snapshots retain the confirmed scope. Other research work may read a
local requirement as background; it cannot declare that it applied that
requirement. Stage changes, historical browsing and recovery do not enlarge the
scope. A pending input remains available for applicable subsequent work.

Guidance-linked material references and retained treatments expose
`source_guidance`, including original text, understanding, confirmed scope,
preserved conditions and confirmation provenance. Subsequent work can therefore
distinguish a local requirement from material evidence it independently finds
useful. The existing receiver, on-demand reads and selective RM retention remain
the material contract; the source scope is not a new gate on independent evidence
adoption.

Guidance feedback reports the actual understanding, changes, continuing work and
reasons. `goal_impact` distinguishes `none`, `requires_evolution` and
`undetermined`. Strength controls adherence within scope; it does not decide
whether the Quest target changes. A local strength-5 instruction therefore needs
no empty goal revision and creates no global target-alignment obligation.

A confirmed Quest-wide instruction remains subject to target-impact assessment.
Omitting `goal_impact` is not a negative assessment: Quest-wide and legacy
strength-5 feedback remains `undetermined` unless it explicitly reports the
impact (or declares `goal_alignment_pending`). Local scope does not create a
Quest-wide obligation merely because its strength is 5.

Feedback may explicitly name `resolves_receipts` to resolve earlier uncertain
assessments of the exact guidance version. Use `goal_assessments[].receipt_ref` exposed by a fresh complete guidance read. The original effect receipts remain immutable; a new
effect records the resolution. Stale, foreign or unread resolutions are rejected.
An explicit `none` conclusion cannot erase an existing `requires_evolution`
assessment. This allows uncertainty to be resolved without an empty goal version
or last-writer-wins treatment of parallel research.

An actual or unresolved target change stays pending until handled. Actual target
evolution continues to update completion criteria, preserve explicit conditions,
invalidate stale completion candidates and assess running work. A local
instruction cannot authorize a Quest-wide change by itself. Research agents may
still evolve a target from this Quest's accepted evidence, informed by research
style, without a new blanket human-confirmation gate.

Older guidance records remain readable with their original provenance. Missing
scope confirmation is identified as legacy data; it is never reconstructed as
historical human consent. Existing legacy strength-5 alignment obligations are
preserved. New confirmed guidance follows the scope and impact rules above.

## Delivery boundary

Companion and Writing remain independent of research Cycles. This change retains
the existing Cycle architecture, forward stage progression and Reasoning-led
successor decisions. It adds no UI implementation, generic intent router or
research scheduling layer. Short isolated engineering and Agent samples establish
their recorded behavior; long-term scientific value remains an observation made
through real research.
