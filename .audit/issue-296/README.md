# #296 stable TargetRun workspace

Status: implementation and component validation are available. Final integrated
checks, native sandbox CI and actual Claude Code deep review are merge gates.
Independent review also found that reboot evidence lacked an original-operation
consumer. Its isolated Linux reproduction is recorded as
`process-review-reboot-red01`; a repair is in progress and this finding blocks
completion until verified.
This report does not establish #274 scientific-chain acceptance or deployment.

## Scope and baseline

The baseline is `v1-test@c2392b4b37964d963ce1477a65025e252c7802b7`, the merged
#297 implementation and stability fixes for #295. Its complete CI run
42969218821 passed. GitHub was rechecked on 2026-11-11 at 14:00 UTC. The base
had not moved. #296 remained open, unassigned and without native blocked-by
dependencies. #262, #295 and #274 remained open. Branch protection and rules
were absent; no protection setting was changed and that absence does not waive
the user's review and validation gates.

Work uses `feat/296-stable-target-workspace`. The original clean
`fix/295-stability-review@1b78578f` branch was preserved. Implementation commit
`069cdab4c75f775bcd7d4281ebb2e059f3c56b8b` has tree
`78d8a7c87a104fbbbdf6e9d059ce02aade36af33`.

## Decisions and preserved contracts

One private Linux subreaper helper owns native and adopted children for one
provider operation. Only it calls waitpid on those children. The outer Popen
owns the helper, reads its private non-inherited pipe and signs the existing
transport receipt after positive ECHILD closure. The actual native code is
retained, including negative signal codes. pidfds prevent signalling a reused
PID. Actual tests used x86_64 Linux 5.4.250-2-velinux1u1-amd64 and Python
3.12.13. Other architectures are not claimed supported by this helper.

Missing closure, failed cleanup, helper/supervisor loss and a crash before
durable receipt remain unknown. PID/PGID, marker, cwd and fd absence do not
establish closure. The existing signed request/recovery envelope can attest a
different boot on the same host, proving disappearance of the old local tree
without inventing a native return code or an external-effect outcome. Changed
boot tests inject the boot reader; no host was actually rebooted.

Every abnormal restart verifies the original operation, invocation, attempt
and fence. Successor reservation retains only the failed operation's transport
index or exact never-issued preparation fact, with no old capabilities. An
earlier fallback was deleted after negative tests showed that it could accept
missing or malformed same-attempt evidence. Healthy next turns remain valid.
Unknown external effects are reconciled by their original identity, never
replayed because local processes ended.

An explicit stop can end unprovable work as a failed Target with a
TechnicalBlocker and revoked execution authority. It does not acknowledge a
mechanical stop. The original operation/effect stays unknown and holds reuse
and cleanup. A later authenticated receipt settles that same operation through
the existing daemon sweep; it cannot reopen the failed Target or clear an
unrelated external responsibility. The real Bundle consumer acknowledges a
blocked report, without manufacturing a scientific TargetCommit or completed
Bundle stage.

Guidance uses the live native steering path and does not end the operation's
workers. Actual operation end and explicit stop reap local descendants and
retain checkpoint files. Independently owned external experiments are neither
cancelled nor resubmitted by this local event. Target role instructions now
state this ownership boundary and the checkpoint/reconciliation requirement.

## Stable workspace and removed work

Phase two began only after the phase-one gate recorded below. A TargetRun now
reserves one workspace. At least two legal recoveries retain its workspace_ref,
physical cwd, inode and changed work bytes while rotating root/session,
attempt, fence and grants. Reservation identity fields are immutable provenance,
not current execution permission. Both root recovery and the existing generic
same-TargetRun Owner recovery entry point retain the stable workspace.

Removed from the running product are successor directory allocation, recovery
copy staging and publication, full-tree source/destination hash revalidation,
copy locking, continuity payload/manifest decoding, acceptance/query APIs and
the dedicated continuity table/recovery column. Necessary input freezing,
managed content, selected exact versions, provenance, Owner acceptance,
operation/effect reconciliation and revocation checks remain. Finalize hashes
attest frozen byte identity, not arbitrary external pollution detection.

The former two-recovery fixture has three directory reservations and two
accepted copies. The new fixture has one reservation, the same cwd and the
same inode. This is observed removed work, not a claimed percentage speedup.

Migration 0085 validates the already accepted old copy metadata before removing
the obsolete schema. It does not read, move or copy research files. Historical
retired directory rows remain for original HR paths, linked content and GC.
Malformed or unfinished old copy metadata, including no active workspace,
rejects atomically and preserves the old database. Arbitrary incomplete older
databases are not claimed upgradeable. There is no runtime copy/stable dual path.

An early in-task 0084 fixture predating the final phase-one profile contract
opens for historical reading but correctly cannot start a new provider. No
compatibility path was added for it. A fresh fixture made from the exact final
phase-one source performs two old-model recoveries, upgrades at the original
absolute paths, reopens historical HR and linked originals, continues its third
calculation segment and performs another real recovery with stable cwd/inode.

Late HumanRequest material retains the original request/root/work/delivery
and deduplication. Issuer history and strong recovery evidence authenticate a
historical root within the same TargetRun; other runs and old grants remain
rejected. Startup delivery/reconciliation now runs after the necessary proof
verifiers are bound. A historical waiter may consume its actual response after
legal recovery without resuming its former provider operation. An unknown
operation cannot use an HR response as a restart bypass.

linked_local originals remain at their original exact version. Editing uses a
new working file and normal selection/custody. Tests retain old-version reads,
accept the selected derived file, reject original-byte drift and exclude
unselected drafts. GC retains the current and previous two formal Cycles,
regardless of turns or recoveries. Pending HR/upload/intake/read/recovery and
unfinished execution responsibilities block deletion. Reconciled failed work
also requires the exact Bundle notice ACK before scratch can be removed.
Linked originals and necessary parents remain, with repeated cleanup and
cross-Quest isolation tested.

## Verification and limits

See [verification.json](verification.json) for source archive hashes and run
results, and [acceptance.tsv](acceptance.tsv) for the contract mapping. Full
source archives, per-file hashes, JUnit, raw logs and controlled fixture data
remain in the task-specific evidence directory
`D:/meta_research/optimier_with_claude/issue-296-evidence-20261111/env/runs/`.
The tests themselves are committed and can run with the locked dependencies.
All Linux data is newly created under isolated temporary paths. Existing
services and research data were not modified.

The phase-one gate was composite, not a claimed single green snapshot.
`phase1-gate01` ran 686 tests with 677 passes, seven failures and two skips.
The seven failures were resolved: one observation fixture field, two native
guidance transport-root fixtures, and four profile-gate regressions. The
corrected final profile matrix passed 108/108; native guidance passed 17/17.
The phase-two red probe then completed two legitimate recoveries and failed
only the old model's stable-path/inode assertions. This authorized phase two
at 13:18 UTC on 2026-11-11.

Two existing adapter tests require unavailable fixed-version Codex 0.147
child-session ledgers. Their skips remain explicit coverage gaps, not process
tree proof. New Linux process-tree acceptance has no skipped cases. Actual
Codex 0.159.0 workspace-write fails on the SSH host's namespace policy. A
dedicated Ubuntu 24.04 CI job runs the actual native sandbox at both recovered
cwds, requires both native cases and rejects any skipped acceptance test.
That job must pass before claiming native sandbox acceptance.

Controlled-provider integration executes actual finite Python work, retains
its checkpoint across a legal recovery, publishes through the real
Finalizer/RM/RG, and lets Bundle consume the exact version. It does not use a
research model and is not #274's real scientific workflow. GC tests construct
ended Cycle history only to exercise the permitted retention window; they do
not turn a blocked Bundle into scientific completion.

Existing failures were investigated, not waived. Migration tests had stale
head revisions, removed historical tables or old call signatures; their
fixtures now check the actual current contract while retaining exact upgrade,
foreign-key, idempotence and rejection assertions. The harness migration tests
verify the intermediate schema before checking the current head. Browser
control fixtures were updated to the current workspace route and controls;
responsive containment/focus and exact raster thresholds remain unchanged.
No performance threshold was relaxed.

## Review and delivery gates

The actual Claude Code design attempt used `claude-opus-5-5`, maximum effort,
read-only tools, and eight Read calls. It exited with an HTTP 403 quota error
before a verdict ($0.39 available, $1.16 requested). Source bytes were unchanged.
It is not a successful review. Final-increment Claude Code review remains
required; independent Codex review cannot substitute for it.

Before merge, require final affected backend/browser, npm tests, type checks,
build, the full response-time CI and native sandbox job; independent review
of the exact patch; actual Claude Code deep review with all blocking findings
resolved; and a fresh target-branch, CI, protection and tested-tree check.
No auto-merge is armed. No deployment is authorized. #295 and #274 remain open.

The #274 handoff must name the delivered code commit and CI/merge version,
preserve all review and environment limitations, and independently establish
the real scientific chain on the version containing both #295 and #296.

Poteto choices followed Model the Domain (existing operation/lifecycle
authorities), Laziness Protocol (delete relocation-only machinery), Sequence
Work into Verifiable Units (prove phase one before stable cwd), and Prove It
Works (real Linux fault injection and physical file assertions).
