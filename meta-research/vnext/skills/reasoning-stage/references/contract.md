# Reasoning Stage fixture contract

Use this only for the MVP and its tests. The field names, nesting, fake facts and fake receipts below are one nonbinding prototype encoding; they are not a production schema, an Owner API, or a substitute for Resolutions owned by #63, #64, or #89.

## Frozen request

`stage_run_request` must contain a current Reasoning request, its accepted Question binding, a complete upstream Stage path, explicit Plan and literature input unions, complete accepted TargetCommit closures, any Bundle-owned replan candidates, and a frozen analysis context:

```json
{
  "type": "StageRunRequest",
  "ref": "stage-run-request/…",
  "foreground_epoch_ref": "foreground-epoch/…",
  "stage": "Reasoning",
  "is_current": true,
  "question_ref": "question/…",
  "root_session_ref": "agent-session/…",
  "accepted_question_binding": {
    "kind": "AcceptedQuestionBinding",
    "ref": "accepted-question-binding/…",
    "currentness_fact_ref": "graph-currentness-fact/…",
    "question_anchor": {
      "kind": "QuestionAnchor",
      "ref": "question-anchor/…",
      "question_ref": "question/…",
      "formal_question_content_ref": "formal-question-content/…",
      "content_hash": "sha256:…",
      "schema_ref": "formal-question-schema/…",
      "rm_acceptance_receipt_ref": "rm-receipt/…",
      "question_accepted_receipt_ref": "rg-receipt/…"
    }
  },
  "frozen": {
    "upstream_stage_closure": [
      {
        "stage": "Idea",
        "stage_commit_ref": "stage-commit/idea/…",
        "outcome": "Completed"
      },
      {
        "stage": "Plan",
        "stage_commit_ref": "stage-commit/plan/…",
        "outcome": "Completed"
      },
      {
        "stage": "Bundle",
        "stage_commit_ref": "stage-commit/bundle/…",
        "outcome": "Completed"
      }
    ],
    "plan_evidence_input": {
      "kind": "accepted",
      "formal_plan_ref": "formal-plan/…",
      "evidence_reuse_set_ref": "evidence-reuse-set/…",
      "evidence_reuse_leaves": [
        {
          "ref": "reuse-evidence/…",
          "role": "MetricResult",
          "asset_version_ref": "asset-version/prior-metric/…",
          "target_commit_root_ref": "target-commit/prior/…",
          "source_evaluation_attempt_ref": "evaluation-attempt/prior/…",
          "source_variant_run_ref": "variant-run/prior/…",
          "source_subject_kind": "EvaluationAttempt",
          "source_subject_ref": "evaluation-attempt/prior/…",
          "provenance_closure_refs": [
            "reuse-evidence/…",
            "asset-version/prior-metric/…",
            "target-commit/prior/…",
            "evaluation-attempt/prior/…",
            "variant-run/prior/…",
            "rg-receipt/target-commit/prior/…",
            "rg-receipt/formal-measurement/prior/…",
            "rg-receipt/role/prior-metric/…"
          ],
          "capabilities": ["supports metric comparison"],
          "eligibility_token_ref": "rg-eligibility/prior/…",
          "integrity_receipt_ref": "rm-integrity/prior/…",
          "availability_receipt_ref": "rm-availability/prior/…",
          "currentness_receipt_ref": "rg-currentness/prior/…",
          "source_target_commit_acceptance_receipt_ref": "rg-receipt/target-commit/prior/…",
          "source_formal_measurement_acceptance_receipt_ref": "rg-receipt/formal-measurement/prior/…",
          "source_role_acceptance_receipt_ref": "rg-receipt/role/prior-metric/…",
          "supported_claim": "the accepted metric under the frozen protocol",
          "support_boundary": "only that accepted attempt"
        }
      ]
    },
    "question_literature_input": {
      "kind": "revision",
      "revision_ref": "question-literature-revision/…",
      "records": [
        {
          "ref": "literature-record/…",
          "evidence_basis": "abstract",
          "evidence_basis_ref": "literature-evidence-basis/…",
          "reading_result_ref": "reading-result/…"
        }
      ]
    },
    "accepted_target_commit_closures": [
      {
        "accepted": true,
        "experiment_key": "experiment-key/…",
        "target_commit_ref": "target-commit/…",
        "semantic_chain": {
          "target_ref": "target/…",
          "baseline_ref": "baseline/…",
          "variant_ref": "variant/…",
          "variant_run_ref": "variant-run/…",
          "evaluation_ref": "evaluation/…",
          "protocol_version_ref": "protocol-version/…",
          "evaluation_attempt_ref": "evaluation-attempt/…"
        },
        "comparison_semantics": {
          "changed_axis_fact_refs": ["changed-axis-fact/…"],
          "held_fixed_fact_refs": ["held-fixed-fact/…"],
          "provenance_refs": ["provenance/…"]
        },
        "execution_input_bindings": [
          {
            "subject_kind": "VariantRun",
            "subject_ref": "variant-run/…",
            "binding_ref": "execution-input-binding/variant-run/…",
            "causal_inputs": [
              {
                "input_ref": "implementation-revision/…",
                "asset_version_ref": "asset-version/implementation/…",
                "rm_asset_receipt_ref": "rm-receipt/implementation/…"
              },
              {
                "input_ref": "data-revision/…",
                "asset_version_ref": "asset-version/data/…",
                "rm_asset_receipt_ref": "rm-receipt/data/…"
              }
            ],
            "rg_binding_receipt_ref": "rg-receipt/binding/variant-run/…",
            "ar_execution_receipt_ref": "ar-receipt/execution/variant-run/…"
          },
          {
            "subject_kind": "EvaluationAttempt",
            "subject_ref": "evaluation-attempt/…",
            "binding_ref": "execution-input-binding/evaluation-attempt/…",
            "causal_inputs": [
              {
                "input_ref": "protocol-version/…",
                "asset_version_ref": "asset-version/protocol/…",
                "rm_asset_receipt_ref": "rm-receipt/protocol/…"
              },
              {
                "input_ref": "evaluation-data/…",
                "asset_version_ref": "asset-version/evaluation-data/…",
                "rm_asset_receipt_ref": "rm-receipt/evaluation-data/…"
              }
            ],
            "rg_binding_receipt_ref": "rg-receipt/binding/evaluation-attempt/…",
            "ar_execution_receipt_ref": "ar-receipt/execution/evaluation-attempt/…"
          }
        ],
        "asset_roles": {
          "metric_result": {
            "role_ref": "metric-result/…",
            "memory_ref": "memory/metric/…",
            "evaluation_attempt_ref": "evaluation-attempt/…",
            "rm_asset_receipt_ref": "rm-receipt/asset/metric/…",
            "rg_role_receipt_ref": "rg-receipt/role/metric/…"
          },
          "checkpoint_artifacts": [
            {
              "role_ref": "checkpoint-artifact/…",
              "memory_ref": "memory/checkpoint/…",
              "produced_by_variant_run_ref": "variant-run/…",
              "selected_by_evaluation_attempt_ref": "evaluation-attempt/…",
              "selected_by_target_commit_ref": "target-commit/…",
              "rm_asset_receipt_ref": "rm-receipt/asset/checkpoint/…",
              "rg_role_receipt_ref": "rg-receipt/role/checkpoint/…"
            }
          ],
          "selected_logs": [
            {
              "role_ref": "log-asset/…",
              "memory_ref": "memory/log/…",
              "selected_by_target_commit_ref": "target-commit/…",
              "source_subject_kind": "VariantRun",
              "source_subject_ref": "variant-run/…",
              "rm_asset_receipt_ref": "rm-receipt/asset/log/…",
              "rg_role_receipt_ref": "rg-receipt/role/log/…"
            }
          ],
          "selected_analyses": [
            {
              "role_ref": "analysis-asset/…",
              "memory_ref": "memory/analysis/…",
              "selected_by_target_commit_ref": "target-commit/…",
              "source_subject_kind": "EvaluationAttempt",
              "source_subject_ref": "evaluation-attempt/…",
              "rm_asset_receipt_ref": "rm-receipt/asset/analysis/…",
              "rg_role_receipt_ref": "rg-receipt/role/analysis/…"
            }
          ]
        },
        "formal_measurement_acceptance": {
          "receipt_ref": "rg-receipt/formal-measurement/…",
          "evaluation_attempt_ref": "evaluation-attempt/…"
        },
        "target_commit_acceptance": {
          "receipt_ref": "rg-receipt/target-commit/…",
          "target_commit_ref": "target-commit/…"
        }
      }
    ],
    "bundle_replan_candidates": [
      {
        "kind": "BundleReplanRequiredCandidate",
        "ref": "bundle-replan-candidate/…",
        "source_bundle_stage_commit_ref": "stage-commit/bundle/…",
        "experiment_key": "experiment-key/…",
        "experiment_brief_ref": "experiment-brief/…",
        "accepted_partial_target_commit_refs": ["target-commit/…"],
        "unrealized_item_refs": ["experiment-item/…"],
        "semantic_change_basis": [
          {
            "frozen_slot": "SemanticDelta",
            "basis_refs": ["basis/…"],
            "required_change": "why the frozen semantic slot must change"
          }
        ]
      }
    ],
    "research_context": {
      "ref": "reasoning-research-context/…",
      "hash": "sha256:…",
      "current_cycle_ref": "research-cycle/…",
      "current_question_ref": "question/…",
      "prior_question_outcome_refs": ["scientific-outcome/…"],
      "parent_question_refs": ["question/…"],
      "active_graph_snapshot_ref": "active-graph-snapshot/…",
      "quest_ref": "quest/…",
      "goal_revision_ref": "goal-revision/…"
    }
  }
}
```

`foreground_epoch_ref` is mandatory and `is_current` must be exactly `true`; unknown is unsafe. For compact fixture feedback, the object carries `root_session_ref`. Production obtains that identity from the validated Agent Runtime binding and does not add it to Advancement Engine's immutable StageRunRequest.

`accepted_question_binding` models the binding frozen by Advancement Engine. Its Anchor and QuestionRef must match the request, and its receipt/currentness refs must be present. The Stage reuses it throughout the Run; it neither queries live Question currentness again nor revalidates the six-field Formal Question schema.

## Upstream Stage and optional-input unions

`upstream_stage_closure` has exactly three unique commits in `Idea → Plan → Bundle` order. Outcomes are `Completed | Skipped | Exhausted`; every `Skipped` has non-empty `typed_basis_refs`. At most one Stage is `Exhausted`; every later optional Stage is then `Skipped` and its basis includes that exhausted commit. Reasoning itself is not in this closure and never has an Exhausted outcome.

In this fixture an Exhausted entry also carries its accepted closure references:

```json
{
  "stage": "Plan",
  "stage_commit_ref": "stage-commit/plan/…",
  "outcome": "Exhausted",
  "exhaustion_proposal_ref": "exhaustion-proposal/…",
  "exhaustion_evidence_refs": ["exploration-record/…"]
}
```

Only `Skipped` carries `typed_basis_refs`; only `Exhausted` carries these exhaustion fields. They establish the Stage path but do not become scientific evidence.

`plan_evidence_input` is exactly one of:

```json
{
  "kind": "accepted",
  "formal_plan_ref": "formal-plan/…",
  "evidence_reuse_set_ref": "evidence-reuse-set/…",
  "evidence_reuse_leaves": []
}
```

```json
{
  "kind": "none",
  "basis_stage_commit_refs": ["stage-commit/plan/…"]
}
```

`Plan=Completed` requires the `accepted` branch. `Bundle=Completed | Exhausted` also requires real accepted Plan input when the current Cycle's Plan commit is Skipped, because Bundle consumed such input. An `Idea=Exhausted` or `Plan=Exhausted` path cannot simultaneously carry an invented accepted Plan branch. The `none` branch is valid only when Plan is `Skipped | Exhausted` and Bundle is `Skipped`; it carries exact upstream basis StageCommit refs, including that Plan commit, and must not contain placeholder Plan, Reuse Set, or reuse leaves. An accepted Reuse Set may honestly have zero selected leaves.

Every `evidence_reuse_leaf` preserves the canonical Plan EvidenceRef closure (`asset_version_ref`, TargetCommit root, provenance, capabilities, eligibility, integrity, availability and currentness), its use boundary, plus the source VariantRun, selected EvaluationAttempt, precise role source and role-specific acceptance receipts needed by Reasoning. `role` is exactly `MetricResult | CheckpointArtifact | LogAsset | AnalysisAsset`; `source_subject_kind` is `VariantRun | EvaluationAttempt` and its ref must be the corresponding frozen source. MetricResult is Attempt-sourced, CheckpointArtifact is Run-sourced, and Log／Analysis may use either real source. Provenance includes the leaf, AssetVersion, TargetCommit, Run, Attempt and the three source acceptance receipts. The fixture rejects missing or untyped closure fields, duplicate leaves, a role/source mismatch, and any caller attempt to relabel a frozen reuse leaf when citing it. The receipt refs are a prototype projection of already frozen, accepted input: production must validate them through their Owners rather than trusting their spelling or letting Reasoning mint them.

`question_literature_input` is exactly one of:

```json
{
  "kind": "revision",
  "revision_ref": "question-literature-revision/…",
  "records": []
}
```

```json
{
  "kind": "none"
}
```

An empty `records` list means an accepted, honest empty revision; it is not `none`. Every record retains its real `evidence_basis`, `evidence_basis_ref`, and optional `reading_result_ref`. The fixture accepts only `title_lead | citation_context | abstract | verified_fulltext` as the copied basis label and never upgrades one to another; snippet, metadata, inaccessible text, or a failed reading cannot be represented as stronger evidence. The `none` branch is explicit and has no literature evidence leaves. `QuestionLiteratureRevision` is context, not an evidence whitelist.

These unions allow all legal Skipped/Exhausted paths to enter mandatory Reasoning without invented scientific inputs. If Plan, literature, and accepted TargetCommit evidence are all absent, an honest `insufficient_evidence` outcome with explicit missing evidence remains runnable; an upstream Exhausted commit is not itself evidence for `affirmed` or `denied`.

## Accepted TargetCommit closure

Each element of `accepted_target_commit_closures` is independent and must satisfy all of the following:

1. It is accepted and preserves one exact semantic chain from Target through one selected EvaluationAttempt to that Attempt's one MetricResult. Multiple accepted closures may be compared, but each remains independent: the fixture never queries a latest/best Attempt to fill a gap or merges leaves from different Attempts into one measurement.
2. `asset_roles.metric_result` is one object, not a list, and its Attempt matches `semantic_chain.evaluation_attempt_ref`.
3. `checkpoint_artifacts` is `0..N`. Every selected artifact was produced by the chain's VariantRun and explicitly selected by the same EvaluationAttempt and TargetCommit. Zero artifacts is valid.
4. `execution_input_bindings` contains exactly two distinct entries: one for that VariantRun and one for that EvaluationAttempt. Every causal input is a typed triple of input ref, immutable AssetVersion ref and RM asset receipt; each binding also preserves its RG binding receipt and AR execution receipt. A naked code/config/data/protocol ref is insufficient.
5. Every MetricResult, CheckpointArtifact, selected LogAsset, and selected AnalysisAsset preserves a Research Memory content ref/acceptance receipt separately from its Research Graph role ref/acceptance receipt. Every selected Log／Analysis also binds `source_subject_kind + source_subject_ref` to this closure's VariantRun or selected EvaluationAttempt; selection by TargetCommit alone cannot erase or replace that source. File existence, an AR execution receipt, or another asset role cannot replace either acceptance.
6. `formal_measurement_acceptance` binds the selected Attempt, and `target_commit_acceptance` binds the current TargetCommit.
7. `comparison_semantics` preserves all known changed-axis facts, held-fixed facts, and provenance. Their cardinality does not select or downgrade a scientific disposition.
8. Refs across the TargetCommit, semantic chain and all asset roles are role-disjoint. Logs and analyses may explain a measurement, failure, or limitation, but cannot substitute for MetricResult or Formal Measurement Acceptance.

An empty `accepted_target_commit_closures` list is valid. Evidence proposed from this source uses `kind=TargetClosureLeaf`, identifies its exact source TargetCommit/Attempt, and names one of `TargetCommit | Baseline | Variant | VariantRun | Evaluation | ProtocolVersion | EvaluationAttempt | MetricResult | CheckpointArtifact | LogAsset | AnalysisAsset`. A ref present under another role is rejected. Reuse and literature evidence use their own `EvidenceReuseLeaf` and `LiteratureRecord` kinds rather than pretending to be current Target closure leaves.

## Scientific candidate boundary

The MVP emits `ScientificOutcomeCandidate` with one of `affirmed`, `denied`, `uncertain`, or `insufficient_evidence`. It contains role-preserving evidence with `supporting | negative | partial | context` findings, `support_scope`, `limitations`, disposition-specific `missing_evidence` and `uncertainty_basis`, `causal_interpretation`, optional Bundle replan interpretations, and an open-form multi-scale synthesis bound to `research_context`.

Reasoning first decides, from the accepted Question's answer shape and applicability scope plus the frozen inputs, whether the minimum evidence obligations needed to answer are covered. The prototype does not decide that scientific question; it only enforces a disjoint, auditable output shape:

| Disposition | Required shape |
| --- | --- |
| `affirmed \| denied` | non-empty bounded `claim` whose positive/negative direction follows the frozen Question's `unknown_statement` and `answer_shape`, role-valid substantive evidence, `missing_evidence=[]`, `uncertainty_basis=[]` |
| `uncertain` | minimum obligations covered, non-empty bounded `claim`, role-valid substantive evidence, `missing_evidence=[]`, and non-empty `uncertainty_basis` explaining why the accepted results themselves do not resolve the answer |
| `insufficient_evidence` | `claim=null`, non-empty `missing_evidence` naming unresolved required obligations, and `uncertainty_basis=[]`; any available partial or contextual evidence keeps its real role |

`missing_evidence` therefore means evidence required to make the current bounded decision, not evidence that would merely strengthen confidence. Optional follow-up, residual caveats, and scope limits belong in `limitations`. This primary-barrier rule keeps `uncertain` distinct from `insufficient_evidence` without letting the fixture pre-write scientific sufficiency.

The fixture can verify identity, roles, closure, required field shape and exact reconstruction, but it cannot parse an open-form claim to prove that `affirmed` or `denied` is scientifically correct. The candidate must align that direction to the frozen Question; Answer/Evidence acceptance remains the semantic authority rather than a string heuristic in this prototype.

`causal_interpretation` explicitly preserves every frozen TargetCommit ref, all associated changed-axis, held-fixed and provenance refs, attribution-basis refs, claim scope, a bounded statement, a sufficiency rationale, and explicit confounders. The fixture checks exact coverage, requires attribution basis to stay in the frozen/evidence closure, and rejects silently dropped comparison facts. It does not infer scientific sufficiency from the number of changed axes: a multi-axis result may support any disposition when Reasoning states the appropriate joint or bounded attribution; a single-axis result is not automatically sufficient.

```json
{
  "causal_interpretation": {
    "target_commit_refs": ["target-commit/…"],
    "changed_axis_fact_refs": ["changed-axis-fact/…"],
    "held_fixed_fact_refs": ["held-fixed-fact/…"],
    "provenance_refs": ["provenance/…"],
    "attribution_basis_refs": ["metric-result/…"],
    "claim_scope": "joint effects in the tested comparison",
    "statement": "bounded causal interpretation",
    "sufficiency_rationale": "why this evidence supports only that scope",
    "confounders": ["known remaining ambiguity"]
  }
}
```

`affirmed`, `denied`, and `uncertain` require substantive role-valid scientific evidence: a frozen LiteratureRecord, a reused `MetricResult`, or the `MetricResult` of a complete accepted TargetCommit closure. Reused or current `CheckpointArtifact`, `LogAsset`, or `AnalysisAsset` may explain, limit, trace, or reproduce a result, but cannot alone satisfy that gate. A number printed in a log, a derived analysis, or an available checkpoint remains that asset role until the appropriate Owner accepts a formal MetricResult. StageCommits, execution completion, or Bundle replan candidates alone are not scientific evidence. Evidence roles come from the frozen closure; the candidate cannot relabel one ref, repeat it under another role, or attach conflicting findings through caller-supplied role text.

`research_context` is a frozen analysis projection, not a new Owner. The synthesis explains the material effect on the current Cycle, the current Question across Cycles, available parent Questions, and the current Quest Goal/milestones. Its scope refs stay within the frozen context; it does not change Question state, parentage, Goal, or Quest.

## Bundle replan interpretation

`bundle_replan_candidates` is frozen input owned by Bundle. In this fixture's mechanical encoding, every candidate's `source_bundle_stage_commit_ref` must identify the one Bundle commit in `upstream_stage_closure`, and that copied commit outcome must be `Completed`; this is a prototype validation choice, not a production Resolution field contract. Each candidate also binds its ExperimentKey/Brief, accepted partial TargetCommits from that same ExperimentKey, unrealized items, and at least one evidence-backed change to a fixture-recognized frozen slot (`Goal | Characteristics | BoundaryConstraints | SemanticDelta | HeldFixed`). Provider/resource errors, missing MetricResult, missing receipt, or unknown currentness are not semantic-change basis.

Reasoning may only explain these candidates. Its `bundle_replan_interpretations` has exactly one entry per frozen candidate and preserves the exact candidate ref, source Bundle StageCommit ref, and complete flattened source basis refs while adding an interpretation. With no frozen candidate the output list is empty. A local or top-level `replan_required: bool`, an invented candidate, a changed basis, or an interpretation of an unknown candidate fails closed. Scientific disposition remains independent of whether a Bundle candidate exists.

```json
{
  "bundle_replan_interpretations": [
    {
      "source_candidate_ref": "bundle-replan-candidate/…",
      "source_bundle_stage_commit_ref": "stage-commit/bundle/…",
      "source_basis_refs": ["basis/…"],
      "interpretation": "how the frozen candidate constrains this outcome or a successor Cycle"
    }
  ]
}
```

## Unique transition candidate

Every normal outcome has exactly one `ReasoningTransitionCandidate`: `NextCycleProposal | CandidateCompletion`. It binds the same ScientificOutcomeCandidate's StageRunRequest, Foreground Epoch, root Session, source Question, and Quest. The fixture rebuilds it from an allowlist so old proposal forms, dependency routes, internal creation details, or a writable `active` flag cannot leak outward.

`NextCycleProposal` binds a complete `QuestionAnchor`, exact `QuestionRef`, entry Stage, skip basis grouped by every skipped Stage, and two current facts from the same Quest and graph revision:

```json
{
  "graph_presence_fact": {
    "kind": "GraphPresenceFact",
    "ref": "graph-presence-fact/…",
    "question_ref": "question/…",
    "quest_ref": "quest/…",
    "graph_revision_ref": "graph-revision/…",
    "value": "present",
    "is_current": true
  },
  "question_research_state_fact": {
    "kind": "QuestionResearchStateFact",
    "ref": "question-research-state-fact/…",
    "question_ref": "question/…",
    "quest_ref": "quest/…",
    "graph_revision_ref": "graph-revision/…",
    "value": "open",
    "is_current": true
  }
}
```

For example, `entry_stage=Bundle` carries typed skip basis for Idea and Plan. The fixture checks exact earlier-Stage coverage and stable refs; Advancement Engine resolves their types, bindings, receipts, and currentness before it creates Skipped StageCommits. This target binding contains no Question dependency/blocking route. `active` is derived only from the Foreground Cycle.

The two fact object names above are nonbinding fixture conveniences. The exact production schema/currentness, acceptance and transitions of `QuestionResearchState=open | resolved | dead_end` remain `TODO-CONTRACT(#64)`; the MVP must not make that unresolved Owner contract appear settled.

Before calling `create_question`, the fixture validates the current Reasoning source, requested entry Stage, all typed skip-basis groups, creation mode and decomposition inputs. Only then does it construct an `AutonomousQuestionDirection` with `creation_mode=AutonomousCreation`, exact source Cycle, StageRunRequest, Foreground Epoch, AcceptedQuestionBinding, Question, and Quest. Thus an invalid transition cannot create a Question before failing. `mode=new|decompose` selects the operation; decomposition also requires a parent Question and basis. ManualCreation is human-started only and is unreachable from this path.

A fake `create_question` response models completion of AutonomousCreation, mandatory DeepFetch, and QuestionCommit only when it returns an RG-accepted `QuestionAnchor` plus current selectable facts. Recoverable waiting resumes the same AutonomousCreation flow. Draft refs, local ids, QuestionProposal, creation details, and free text stay internal and cannot bind a Cycle. A newly accepted Question may use a graph revision newer than the frozen analysis snapshot. Advancement Engine revalidates it and freezes the successor Run's new AcceptedQuestionBinding; the fake starts nothing.

`CandidateCompletion` binds the current Quest, exact Goal revision, rationale, and completion-milestone basis. User confirmation is required before an external collaboration layer may submit it to Goal/Completion Owner; Owner acceptance still does not end the Quest without a separate Advancement Engine ending transition. Exact confirmation, receipt, rejection/reopen and ending semantics remain `TODO-CONTRACT(#89)`. Scientific disposition does not automatically change QuestionResearchState.

### Proposed #104 supersession of #100 Reasoning output

This prototype intentionally exercises an outward contract that is not authoritative until #104 publishes a Resolution:

- `NextCycleProposal` replaces the separate `SelectProposal + CycleStartProposal` shapes for Reasoning.
- `QuestionProposal` is internal to `create_question`; it is not a Reasoning transition.
- `QuestProposal` is no longer a Reasoning outward transition. This ticket does not replace or invent any separate lifecycle for creating an independent Quest.
- selection eligibility is current-Quest `present + open`, and each normal outcome has exactly one `NextCycleProposal | CandidateCompletion`.

Until that Resolution exists, tests prove only the prototype's internal consistency and must not describe this supersession as already effective production authority.

## Named integration seams

| Semantic call | Current status | Rule |
| --- | --- | --- |
| `submit_answer_candidate(candidate)` | `TODO-CONTRACT(#64: unresolved Answer/Evidence lifecycle)` | Before the first side effect, rebuild the candidate from its original StageRunRequest, require exact canonical equality, and verify the port, identity, currentness, and expected reply contract. After the call, require its feedback receipt to bind the same request, Question, and root Session. A rejection revision receives an isolated copy, must bind that receipt, freeze request/Epoch/Question/Quest/Goal/input/evidence identity, and revalidate disposition plus scientific fields before a second submission. If that revision passes locally but the retry port is unavailable, retain it as the final non-authoritative local candidate and report only the submission-side-effect blocker. |
| `submit_confirmed_completion_candidate(candidate, user_confirmation_receipt_ref)` | `TODO-CONTRACT(#89: exact confirmation, Goal/Completion Owner and AE ending lifecycle)` | Fail before Owner submission without user confirmation. Owner acceptance remains distinct from AE's Quest-ending transition. |
| `create_question(direction)` | `TODO-IMPL(question-creation.create_question; source=#85,#105)` | Enter AutonomousCreation from the exact current Reasoning source; ManualCreation is unreachable, and no NextCycleProposal exists until the full lifecycle returns a selectable accepted Question. |
| `start_successor_cycle(proposal)` | `TODO-IMPL(advancement-engine.start_successor_cycle; proposed source=#58,#104,#105)` | AE revalidates presence/state, entry and skip basis, then creates the Cycle and freezes a new AcceptedQuestionBinding. |

#63 still owns unresolved production Target creation/acceptance seams, #64 owns Answer/Evidence and QuestionResearchState semantics, and #89 owns Goal/completion lifecycle. This fixture validates references at those boundaries; it does not manufacture their production operations or receipts.

Unknown currentness, a stale request, a non-canonical or mutably altered candidate, incomplete or cross-role evidence closure, conflicting feedback, an unhealed technical blocker, an unknown promised external result, or inability to produce exactly one transition candidate prevents normal candidate completion and fails closed. A missing Answer or Goal submission port, or a missing user-confirmation receipt, blocks only that external side effect and any acceptance claim; an already valid local candidate and its non-authoritative transition remain deliverable. A missing Question/DeepFetch port keeps Reasoning inside the current Run. Fake outcomes prove neither Owner acceptance, AE Quest completion, Cycle creation, nor Stage advancement.
