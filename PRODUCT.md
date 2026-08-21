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
Agent Runtime persists each primary/review provider invocation before launch;
the Harness seals thread events and structured results in a durable transport
spool, so a response lost before the Owner transaction is reconciled rather
than invoking Codex a second time. An incomplete, unverifiable spool fails
closed instead of guessing whether the external effect happened.

The generic `stage_execution` capability remains unavailable until the later
Plan, Bundle, and Reasoning execution slices are delivered; the Idea execution
implemented here is reported only through the five explicit `idea_stage` facts.

Accepted-material basis and first-question DeepFetch remain explicitly typed
as unavailable until their own production slices are delivered. The direct
path does not use a CreationSeed or treat unavailable material intake as a
waiver.

The supported public host operations are exposed by the `meta-research`
command. Research state is observed through the authenticated Snapshot and SSE
interfaces; the Web client does not read persistence directly.
