# #296 stable TargetRun workspace

Status: the user's 2026-11-11 update selects explicitly configured local
full-access for this ticket. The final native execution, recovery, process-tree
and review-policy matrix passes 51 cases without skips. GitHub response-time
acceptance remains affected by Ubuntu dependency DNS failure. Actual Claude
Code final review is deferred at the user's request. The PR remains unmerged.
The reboot-consumer finding is fixed in `0ef5e3d`, including the actual late
caller. Independent Linux verification covers both stop/reboot orderings.
This report does not establish #274 scientific-chain acceptance or deployment.

## Scope and baseline

The baseline is `v1-test@c2392b4b37964d963ce1477a65025e252c7802b7`, the merged
#297 implementation and stability fixes for #295. Its complete CI run
42969218821 passed. GitHub was rechecked on 2026-11-11 at 15:08 UTC. The base
had not moved. #296 remained open, unassigned and without native blocked-by
dependencies. #262, #295 and #274 remained open. Branch protection and rules
were absent; no protection setting was changed and that absence does not waive
the user's review and validation gates.

Work uses `feat/296-stable-target-workspace`. The original clean
`fix/295-stability-review@1b78578f` branch was preserved. Implementation commit
`069cdab4c75f775bcd7d4281ebb2e059f3c56b8b` has tree
`78d8a7c87a104fbbbdf6e9d059ce02aade36af33`. The prior validated delivery source was
`debbbd2`, including final test synchronization, dynamic GC coverage and the
exact frontend build used by the browser and response-time runs. The complete
commit and tree are in [source-equivalence.json](source-equivalence.json).
The subsequent full-access policy increment is verified separately and does
not retroactively change the scope of that source-equivalence record.

## Decisions and preserved contracts

For this ticket's local execution scope, the operator selects
`META_RESEARCH_TARGET_SANDBOX_MODE=danger-full-access` using the existing
configuration entry. The project default for other environments is unchanged.
An assigned workspace is a cwd and work-ownership convention, not an OS
permission boundary. No cross-directory write-denial claim is made for
full-access. This user decision supersedes the prior workspace-write sandbox
acceptance requirement. Old sandbox failures remain historical evidence.

The configured policy now reaches the Owner review consumer as well as the
adapter. Existing code/result review consumers had retained workspace-write
assumptions, an older code-review lineage shape and a result-only actor field.
The fix reuses the existing allowed-policy function, includes the verified
policy in exact code-child lineage, and selects the already verified actor by
review kind. All parent/child, cwd, operation, authorization and hash checks
remain. Unconfigured full-access and corrupt policy, actor, cwd or parent data
are rejected. [Full-access verification](full-access-verification.json) records
the exact frozen files and 51-case result. Independent review passed after
checking every original JUnit result and all six changed execution/test/CI
files against the frozen manifest. This controlled review-evidence chain
does not claim a real model's semantic review or full scientific acceptance.

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

The completed runs have distinct scopes:

- `final-backend02` collected 949 cases across 83 affected files. It passed 947
  with zero failures and two existing fixed-ledger skips. Its 1,358 source files
  match `0ef5e3d` exactly. Non-generated product source remained unchanged through
  `041b21d`. The later Owner review-policy correction has its own affected matrix.
- `gc-reading-dependency01` adds one passing case with no skips. It pauses a
  real RM intake after the first MiB, observes `processing`, verifies GC holds
  scratch, then completes exact managed readback and actually deletes scratch.
  The isolated negative control fails at premature deletion when processing
  protection is removed. No new lease or production state was introduced.
- `final-browser03` passes all 112 functional cases with zero skips or flaky
  results. `material-browser05` separately passes its one real-material case.
  npm passes 45 tests; application and E2E types and production build pass.
  The fixed Windows-font raster project is not claimed tested on Linux.
- The pagination baseline control fails on offset 0 instead of 50. The revised
  test binds to a completed snapshot request and preserves compact/hydrated
  revision, item-count, offset and exact-selection assertions. Five repetitions
  pass before the full browser run. No threshold was relaxed.
- `response-time-local01` passes the original npm response-time command with
  three cases and zero retries, skips or flaky results. The wrapper's exit 1
  is retained: its final immutability assertion incorrectly included generated
  build files. An independent post-run comparison found no non-generated-file
  changes. Original metrics and separate command exit codes are preserved in
  [verification.json](verification.json). This does not replace GitHub CI.

The committed frontend bundle previously predated the #295/#296 source changes.
The packaged build is now byte-identical to the four files actually served in
the 112-case browser run and the performance run. See
[packaged-frontend.json](packaged-frontend.json). The new bundle is
`index-tpvNNB6h.js`; the obsolete JS bundle is removed.

CI runs 43036588107 and 43040924994 failed while installing Ubuntu dependencies.
Run 43045744741 tested direct official Ubuntu repositories and also failed DNS
resolution before native tests. The ineffective source rewrite was reverted.
After the user's full-access decision, the native recovery job no longer needs
bubblewrap and runs the actual configured full-access test instead. Chromium
system dependencies, original budgets and the no-skips native acceptance check
remain required for their respective jobs.

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
tree proof. New Linux process-tree acceptance has no skipped cases. The earlier
Codex 0.159.0 workspace-write attempt failed on the SSH host's namespace policy.
The current full-access run invokes the same pinned native CLI successfully
before recovery and after each of two recoveries, with checkpoint segments
1, 2 and 3 in the same cwd. It retains old-grant rejection and checks that its
normal operations leave the parallel original unchanged. It does not assert
that the OS prevents a full-access process from writing that original.

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

Independent workspace/consumer review passed at `0ef5e3d`. The final increment
review is `PASS_WITH_DELIVERY_GATES` at `debbbd28d15751077c1b6e1eed46880109663f7e`,
tree `73922c9c490948c42775c8d126bd405301b6fac3`. It found no blocking code issue
in the two tests and packaged build. Its independent comparison proves all four
packaged files match the actual browser/performance build and the CI workflow
matches `0ef5e3d`. These are independent agent reviews, not Claude Code.

The actual Claude Code design attempt used `claude-opus-5-5`, maximum effort,
read-only tools, and eight Read calls. It exited with an HTTP 403 quota error
before a verdict ($0.39 available, $1.16 requested). Source bytes were unchanged.
It is not a successful review. The latest user instruction defers CC review for
this continuation. No new CC invocation was made and no independent review is
reported as CC. Deferral does not establish the earlier merge prerequisite.

Before merge, require final affected backend/browser, npm tests, type checks,
build, the full response-time CI and configured native full-access job; independent review
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
