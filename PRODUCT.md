# Meta-research vNext

Meta-research vNext is a local-first research runtime. This initial production
slice installs an isolated data root, starts a detached per-user daemon, and
serves an authenticated loopback Web shell backed by SQLite and a managed
object store.

For this trusted local deployment, truthful forward progress and a low-friction
user experience take priority. Final confirmation of a Quest activates that
Quest's broad research authorization under the installed deployment policy:
packaged Skills may use the ordinary workspace, file, network, process, and
tool capabilities that the installed Codex can actually provide. The daemon
then advances the Quest automatically; the research lead is not expected to
understand or approve capabilities one by one or repeat authorization for each
Run. Only an expansion beyond the confirmed Quest policy, an external publish
or send, an irreversible effect, or a destructive user-data action interrupts
the user for another decision. A deployment may deliberately narrow its
default policy, and a capability that is absent in reality must remain an
explicit blocker rather than being simulated or silently waived.

The Idea Harness gives Codex non-interactive full local execution because the
supported hosts cannot reliably provide the narrower workspace sandbox. Codex
starts in a dedicated research workspace, separate from the durable provider
spool, and the packaged Skill forbids treating filesystem access as Owner
authority. Authoritative state is still changed only through Owner Interfaces;
new Web findings remain observations until an Owner accepts them as Evidence.

Broad execution does not remove the boundaries that make progress real. Owner
receipts, currentness checks, Execution Fences, and concrete time, process, and
resource ceilings remain enforced, normally without interrupting the user.
Failures preserve their last durable facts and expose a specific blocker so the
system can resume from the first missing boundary. Hardening is proportional to
credible product risk: common failures and plausible recovery paths must be
handled, but extremely unlikely theoretical tails do not justify an unbounded
series of patches that makes normal progress slower or less reliable. Residual
tail risk should be recorded and bounded instead.

## 2026-08-21 Session-topology clarification

Short-lived review, retrieval, and verification work stays inside the current
managed Harness Session. A packaged Skill uses the native Codex or Claude agent
tree to spawn a focused child agent, then the root Agent owns every disposition
and the final revision. Independence means a separate task context and an
advisory-only role; it does not require Agent Runtime to create another Run,
Attempt, Fence, or top-level Codex/Claude Session.

Idea therefore keeps one managed native Session. Its primary draft is durably
checkpointed, then the child-review turn resumes that same Session and returns
the review, root dispositions, and final Outcome together. Child-agent
provenance may be shown for audit, but the child is internal Harness topology,
not an Owner or a durable domain identity. Only work that needs an independently
managed long-lived lifecycle, pause/resume, monitoring, resources, or fencing
gets another top-level Session; a formally scheduled Target is the canonical
example. This keeps ordinary research fast and understandable without weakening
the review or Owner-acceptance boundaries.

From the empty research space, the Web product now supports the direct
Quest-initialization path. A research lead defines the Goal, completion
criteria, key configuration, literature scope, and first-question direction;
the system produces an editable six-field QuestionProposal, aggregates a
deterministic per-Owner Impact Preview, and binds one final confirmation to the
exact immutable Quest draft revision, Proposal, and Preview. Human confirmation,
Quest/Goal acceptance, immutable question-content custody, root-Question
identity, and initial-Cycle activation remain separate durable receipts and
resume from the first missing receipt after a daemon restart.

After the first Question is accepted, the installed daemon now runs the Idea
Stage through its packaged production Skill. Advancement Engine freezes the
exact AcceptedQuestionBinding and Idea ContextPack; Agent Runtime admits one
durable Run with Attempt, root Session, native Codex Session, and current
Execution Fence. The Skill may return either a reviewed `IdeaSet` or a reviewed
`NoViableCandidate`, but it cannot accept that result or advance the Stage.
Research Memory separately accepts the immutable content, Research Graph makes
the independent domain decision, Agent Runtime closes the accepted Run, and
only then may Advancement Engine form `StageCommit(Completed)`. Rejection stays
in the same root Session and creates a materially changed successor Attempt with
the exact rejection lineage.

The authenticated Snapshot and active Lumen workspace expose Idea eligibility,
StageRunRequest, Run/Attempt, content and domain acceptance, and StageCommit as
separate facts. Durable checkpoints let a daemon restart continue from the
first missing Owner boundary; idempotent replay and stale Execution Fences
cannot duplicate a formal submission or advance the Stage early.
Agent Runtime persists both provider turns before launch. The primary turn
creates and checkpoints the native Session; the child-review turn resumes that
same Session instead of creating a reviewer Session. The Harness seals thread
events and structured results in a durable transport spool, so a response lost
before the Owner transaction is reconciled rather than being invoked again. An
incomplete, unverifiable spool fails closed instead of guessing whether the
external effect happened.

The generic `stage_execution` capability remains unavailable until the later
Plan, Bundle, and Reasoning execution slices are delivered; the Idea execution
implemented here is reported only through the five explicit `idea_stage` facts.

Research Asset is now a production vertical slice. The authenticated Web can
submit text, uploaded files, directories, local paths, repositories, links,
and system artifacts through Research Memory's public Command Interface. Each
successful intake forms one immutable AssetVersion with an exact MemoryRef,
hashes, provenance, custody facts, and Asset Accepted Receipt. Managed custody
is accepted only after the content-addressed object is verified; linked-local
custody freezes its manifest and reports later source drift as availability,
without rewriting historical integrity or receipts. Async jobs, interrupted
processing, pre-existing object bytes, replay, and daemon restart reconcile
through the same durable intake record.

The Research Asset Inventory is a read-only Projection over those public Owner
Queries. It keeps integrity and availability separate, materializes only an
exact MemoryRef, and includes durable Hold and ReleaseEligibility receipts.
Custody handoff verifies managed bytes before it issues the target receipt and
never moves or deletes the user's original. ReleaseEligibility is an assessment,
not a delete command, and fails closed when an RG reference, Hold, stale
reference revision, or uncertain state remains. Research Graph separately
accepts Evidence and Quest Source Material roles; those semantic roles do not
transfer content or custody ownership out of Research Memory. Existing formal
Question and Idea content is migrated into the unified inventory without
changing its stable ref, hash, object path, or historical receipt.

Quest creation may select exact accepted AssetVersion bindings for its material
basis, including `provided_only`; raw browser files and paths still cannot enter
that basis directly. First-question DeepFetch is available as the explicit
`deepfetch` route: HC freezes and authorizes one request, AR executes a fenced
live Web Search/Fetch Run, and RM accepts an exact LiteratureSnapshot before the
same six-field Proposal and final confirmation continue. The `direct` path does
not create a snapshot, use a CreationSeed, or treat DeepFetch failure,
cancellation, timeout, or unavailability as a waiver.

Follow-up questions use a separate `manual_question_creation` context entered
from an accepted Question in the Web question tree. Human Collaboration first
freezes the user's exact CreationSeed, then requires either a completed
DeepFetch LiteratureSnapshot or a distinct explicit waiver before an exact
six-field Proposal can be confirmed. Research Memory accepts the immutable
content before Research Graph atomically accepts the child identity, present
parent, and content binding. A restart resumes from the first missing Owner
receipt and reuses the same content ref; closing the browser does not cancel the
context, and neither a failed nor a cancelled DeepFetch is treated as a waiver.
The resulting stable QuestionAnchor does not activate a cycle or advance a
Stage.

An accepted Quest can also authorize the packaged micro-experiment vertical
slice from the Web. Research Graph keeps Baseline, Variant, EvaluationProtocol,
ProtocolVersion, Evaluation, VariantRun, and EvaluationAttempt as independent
semantic identities and freezes separate state-formation and measurement input
bindings. Agent Runtime alone owns the local Run, technical Attempt, root
Session, current Execution Fence, observations, and execution receipt. A
transport replay reconciles the same provider operation; a replacement keeps
the Run and root Session while issuing only a successor Attempt/Fence. A new
provider-operation generation is allowed only after a verified safe terminal
failure, and it still preserves every experiment domain identity. An explicit
retrain creates a VariantRun and EvaluationAttempt, while an explicit remeasure
may reuse one VariantRun and select an ordered zero-to-many set of its accepted
CheckpointArtifact roles.

Experiment definitions, checkpoints, logs, analyses, and result bytes enter
Research Memory as immutable AssetVersions before Research Graph accepts their
semantic roles. Formal Measurement is a separate atomic Research Graph decision
over exactly one EvaluationAttempt: the exact fenced execution and result asset
must verify, and that Attempt must independently contain every required Metric.
Complete negative and zero results remain valid; partial Attempts never combine
and execution or asset existence alone never creates MetricResult. The Lumen Web
therefore projects execution, asset acceptance, and Formal Measurement as three
different facts. Its fixed Execution Fence window observes bounded durable raw
stdout and scoped hardware telemetry, marks stale or historical data honestly,
and never promotes live observations into RM or RG truth.

Writing Report is a separate long-lived capability, not a fifth Stage and not a
gate on Quest advancement. Human Collaboration freezes an exact report Intent,
Research Snapshot, Owner revision cut, and bounded execution budget before
Agent Runtime admits one managed root/native Session. The packaged Writing
Skill may draft, self-review through a focused child context, revise, and use
ordinary read-only research capabilities covered by the Quest policy, but it
cannot publish, send, accept its own deliverable, or claim formal citation
correctness. Pause and resume retain the Session lineage while retiring stale
Execution Fences; terminal cancellation requires a separate exact impact
preview and human confirmation. The default budget bounds content revisions
and output bytes, not elapsed wall time; an idle or paused Session has no hidden
24-hour expiry.

Each content revision is an immutable Agent Runtime Attempt with its own
predecessor, feedback, decision, provider operation, and Fence. Research Memory
first accepts the exact Markdown bytes as an AssetVersion; Research Graph then
independently verifies the frozen admission, execution receipt, deliverable
provenance, source roles, citation anchors, and supported claims before issuing
an acceptance or rejection. A formally supported claim is exact source wording
that RM can read back at its locator; translations, paraphrases, and synthesis
remain visibly classified inference or uncertainty unless a separate verifier
can prove them. Rejection creates a materially changed successor
within the same Writing Session until the frozen revision/resource budget is
exhausted. A revoked authorization, stale cut, unverifiable receipt, timeout,
or exhausted budget persists a typed run-local blocker without making the core
Quest unavailable or starving another Writing Run.

The Web reads Writing only through the same consistent Public Projection. It
keeps unadmitted intents recoverable, displays execution, RM deliverable, RG
citation, and renderer as separate facts, and exposes immutable version history
with content/evidence/citation comparison and stale-state reporting. Any
currently available RM deliverable remains viewable with its RG status, including
rejected versions; formal rendering remains gated on RG acceptance. Rendering an
accepted version also requires currently verified, available RM custody and is
deterministic: repeated reads do not create a new AssetVersion or formal
decision. Historical acceptance remains visible during a custody outage while
the current deliverable and renderer expose a typed unavailable state. Durable
operation identities,
provider spools, stop markers, checkpoints, and retired Fences make lost ACK,
daemon restart, pause, and late worker results reconcile from the first missing
boundary without duplicating delivery or citation acceptance.

The supported public host operations are exposed by the `meta-research`
command. Research state is observed through the authenticated Snapshot and SSE
interfaces; the Web client does not read persistence directly.
