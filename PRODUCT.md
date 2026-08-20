# Meta-research vNext

Meta-research vNext is a local-first research runtime. This initial production
slice installs an isolated data root, starts a detached per-user daemon, and
serves an authenticated loopback Web shell backed by SQLite and a managed
object store.

From the empty research space, the Web product now supports the direct
Quest-initialization path. A research lead defines the Goal, completion
criteria, key configuration, literature scope, and first-question direction;
the system produces an editable six-field QuestionProposal, aggregates a
deterministic per-Owner Impact Preview, and binds one final confirmation to the
exact immutable Quest draft revision, Proposal, and Preview. Human confirmation,
Quest/Goal acceptance, immutable question-content custody, root-Question
identity, and initial-Cycle activation remain separate durable receipts and
resume from the first missing receipt after a daemon restart.

Accepted-material basis and first-question DeepFetch remain explicitly typed
as unavailable until their own production slices are delivered. The direct
path does not use a CreationSeed or treat unavailable material intake as a
waiver.

The supported public host operations are exposed by the `meta-research`
command. Research state is observed through the authenticated Snapshot and SSE
interfaces; the Web client does not read persistence directly.
