# 8768 research process specification

Baseline: exact installed 8767 package, archived on 2026-09-15; git base
`65ed30eb3aafef300ccc2efa800dac8f96171908`. Work happens in a new worktree,
then deploys to an independent 8768 backend and a newly initialized `data/`.
Stop 8767 after 8768 verification. Preserve old data as a stopped archive.

1. Quest expresses the project goal. Questions can remain broad, overlap, have
   subquestions and change focus. Reasoning decides continuation, decomposition,
   revisiting and adjacent questions across Cycles. A Cycle is a period of actual
   research and reassessment, not a promise to solve a Question.
2. Record actual attempts, failures, adjustments, observations and unresolved
   matters. Obligations express this Cycle's investment and review responsibility.
   Each Target carries relevant work; whole-Question or Quest completion is not a
   common prerequisite. Idea proposes, Plan selects investment, Bundle organizes,
   Target investigates, Reasoning synthesizes.
3. Retain the five existing human_request kinds. Ask for substantive judgment,
   assistance or resources with prior attempts/results, uncertainty and concrete
   needs. Carry human responses into subsequent work and handoff. Independent
   work may proceed while the relevant work waits.
4. Baseline, Variant, ProtocolVersion and Evaluation are definitions. VariantRun
   is actual execution with raw observations/data/checkpoints/execution logs.
   EvaluationAttempt evaluates a specified Run/artifact and owns MetricResults,
   evaluation reports and logs. Execution and evaluation may interleave; completed
   execution does not require completed evaluation.
5. RM owns contents/files/versions; RG owns research identity, relations,
   provenance and acceptance. Minimize duplicate wrappers and validation. Keep
   exact version/content consistency, traceable sources, truthful execution and
   evaluation, correct critical links, stable submission and retries.
6. Every entry point exposes why work continues, prior attempts, failures/open
   matters, current scope, exact selected assets and relevant human guidance.
   Use concise notes, precise references and readers; expand history on demand.
7. Target progress uses cursor-based incremental reads with preserved raw logs,
   bounded new fragments and anomalies. Bound graph, outcomes, notes and Session
   rendering/context growth. Display input, execution, artifacts, evaluation,
   acceptance, handoff and help distinctly, including research setbacks,
   technical failure, human waiting and completion.

Validation uses public Owner/semantic tool behavior, focused regressions, frontend
typechecking/build and browser scenarios. Run the complete backend suite once
after integration; classify inherited failures against this fixed baseline.
Use separate disposable data for workflow tests so production `data/` remains
empty. Do not interpret a deterministic provider fixture as new scientific work.
