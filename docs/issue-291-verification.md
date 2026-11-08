# Issue 291 verification

Baseline: `v1-test@b01d6c43851267ea090a1f952f1e307a85ef06f3`.
Scope: [#291](https://github.com/15253a/meta_research/issues/291), under
[#262](https://github.com/15253a/meta_research/issues/262), read from GitHub on
2026-11-08. This is a local implementation and verification record, not a
deployment or #274 integrated acceptance claim.

## Changes and retained checks

- Human input reading reuses `ExactContextPages`. Its identity includes the
  verified body version, receipt hash (covering the envelope), and the current
  material projection hash. Every call still reads and verifies the original;
  the adoption leaf comes from that same verified record. Mutable materials
  are still obtained on every call and cannot be cached under the body hash.
- A semantic gateway owns its registration snapshot, including nested schema
  and error dictionaries. Contract updates take effect through a new gateway
  registration. Public contract schemas and bindings are returned as copies. Stable operation bindings are
  computed once per registration and reused when issuing a channel. Each page
  still checks authorization and availability; its identity covers the
  registration, grant, filters and current available operation IDs.
- Selected material reuse combines the two topology walks. It still verifies
  every selected file's actual bytes and rejects changed files, topology,
  symbolic links and paths outside the selected directory.

There is no global cache framework. Human pages keep the existing bounded LRU
(8 MiB / 128 entries by default); registered bindings scale only with the
gateway's existing operation catalog. A missing cache rebuilds the requested
representation, without a full-store verification or external operation replay.

## Comparable work measurements

Both versions use the same probe and synthetic inputs through actual production
functions, isolated Linux source/data directories, Python 3.12.13, the pinned
project dependencies, and real `fcntl`. The ordinary test suite's existing OS
power-inhibitor substitute is used for the human input fixture; no Owner,
database, file-integrity or authorization implementation is replaced.

| Path / measured work | Baseline | Changed |
| --- | ---: | ---: |
| Human input: pages / response bytes | 24 / 195,462 | 24 / 195,462 |
| Full response JSON encoding calls / bytes | 24 / 4,691,088 | 1 / 195,462 |
| Owner body + receipt hashes / input bytes | 96 / 9,371,184 | 48 / 4,685,592 |
| All SHA256 calls / input bytes | 120 / 14,062,272 | 73 / 4,881,102 |
| SQLAlchemy SQL hook calls | 288 | 120 |
| Human page read elapsed time | 89.99 ms | 31.77 ms |
| Registration (100 synthetic + 2 discovery operations): hashes / input bytes | 307 / 80,612 | 307 / 80,612 |
| Issue channel (all 102 operations): hashes / input bytes | 612 / 64,298 | 0 / 0 |
| 100 tools, one page: canonical hashes / input bytes | 301 / 90,386 | 1 / 2,094 |
| 100 tools, five pages: canonical hashes / input bytes | 1,505 / 451,930 | 5 / 10,470 |
| Five tool pages: all SHA256 calls, including tokens | 1,510 | 10 |
| Five tool pages: live availability checks | 500 | 500 |
| Five tool pages: elapsed time | 14.07 ms | 3.49 ms |
| Registration elapsed time (including new copies) | 2.77 ms | 4.71 ms |
| 256-file directory reuse: rglob / visited entries | 2 / 512 | 1 / 256 |
| Directory reuse: file digests / input bytes | 256 / 18,432 | 256 / 18,432 |
| Directory reuse: SQL hook calls | 14 | 14 |
| Directory reuse: elapsed time | 47.37 ms | 46.08 ms |

Human measurement includes the public `read_content` wrapper and its adoption
resolution; SQL hooks include snapshot/transaction SQL. The ticket's earlier
probe used a different scope and source fixture, so its numbers are not the
before-column here. Directory measurements use RM intake, `writing_message`,
and `selected_material_path`; tool measurements use actual gateway dispatch.
The tool pages select the 100 synthetic operations with `owning_module="example"`.
The synthetic tool probe performs no SQL or filesystem traversal; the human probe performs no
directory traversal. Times are single isolated samples, not production speedup
claims. The directory byte checks deliberately do not decrease.

## Tests and review

- Human pages: 6 passed, including existing representation bounds/eviction and
  rebuild tests. New cases cover exact multi-page roundtrip, original corruption
  after warming and recovery, late material arrival under the same body hash,
  incorrect version and stale scope. The initial older fixture failed setup;
  the corrected fixture demonstrated a genuine baseline encoding failure
  (23 encodings instead of 1), then passed after the implementation.
- Tools: 30 passed across seven files. Coverage includes nested registration
  isolation, returned-value isolation, new contract invalidating old pages,
  live HTTP revocation, HumanRequest wait/resume, and a lost effect response
  reconciled before replay without duplicate effects.
- Materials: 26 passed across four files. Coverage includes same-size content
  replacement with restored mtime, addition/deletion, file/directory replacement,
  symbolic links, temporary traversal failure, recovery without overwriting
  user changes, and existing export failure/retry behavior. Baseline failed
  only the expected duplicate-walk count assertion; changed code passed.
- Two independent code-review axes examined all five product files and three
  new test files against the fixed baseline: Standards 0 findings; Spec 0
  findings. `git diff --check` and Linux Python 3.12 `compileall` for the frozen
  source and tests passed. This Python project has no configured static typecheck
  command; no typecheck pass is claimed. Frontend types are unaffected.

The final backend suite ran all 4,756 unique nodes across 528 files, using
24 independent file workers: **4,271 passed / 483 failed outcomes (469 call
failures, 14 setup errors) / 2 skipped / 0 incomplete**, 1,152.56 seconds, zero
watchdog timeouts. The skipped cases require a real Codex fixture. This is not a green suite.
All 483 failing nodes were replayed on exact baseline `b01d6c4`: 480 also failed
on baseline, and three initially passed there. Matching failures limit
attribution, not the remaining product obligations. Detailed outcomes and
original messages are retained in `full-final` and `baseline-batch1` through
`baseline-batch5`; message differences include random identities/timestamps and
absolute installation paths used in prompt/profile hashes. Of the 480 baseline
failures, 432 matched phase/message; the other 48 comprise 25 installation-path
identities, 22 dynamic IDs/times/object representations, and one earlier Bundle
failure point. See `full-validation-summary.json`, `full-failure-comparison.json`
and `failure-signature-differences.json` for the uncollapsed evidence.

One full-suite failure initially appeared only in the candidate:
`test_web_lifespan_stops_provider_before_waiting_for_drafting_worker` recorded
an extra `drafting_stopped`. Both versions subsequently passed the entire
14-test file and three independent serial runs of that node. An identical
out-of-tree probe delaying `RuntimeProtection.interrupt_active` completion by
0.3 seconds reproduced the same extra event on both versions. The unchanged
shutdown code releases drafting before awaiting the stop request, and the worker
can loop again before cancellation. This supports attribution to an existing
shutdown race; it does not remove the original full-suite failure or fix that
unrelated behavior. Evidence: `lifecycle-*-recheck`, `lifecycle-*-three`, and
`lifecycle-*-delay` in the local runs directory.

`test_pause_resume_preserves_exact_root_human_wait_until_retry[cycle]` initially
failed with `command_preview_stale`. Both versions' whole-file reruns passed
both tests. The original observation remains recorded; a passing rerun alone
does not prove that the intermittent preview failure is fixed. The original
trace reaches the later `post-wait-pause` confirmation after the exact-root
HumanRequest retry/resume assertions passed. HC rejects changed AR/AE Owner
revisions between separate preview and confirmation calls while Web workers are
active. These state-control files are unchanged. The specific writer advancing
that revision was not identified, and timing effects are not ruled out; the
necessary stale-preview protection remains intact.

The third initially candidate-only failure was Bundle
`test_hc_resume_stopped_bundle_primary_preserves_native_and_sealed_history[reject-tampered-stop]`,
with `stage_provider_hard_ceiling_stale`. Its sibling also failed earlier than
baseline in the full run. A baseline with only the changed `semantic_mcp.py`
passed the tampered-stop case. More decisively, the same event-barrier probe
(no sleeps, no forged state/receipts) reproduced the exact stale error in both
versions: verified real stopped checkpoint, then committed pause, then ceiling
admission with `run=suspended`, `unit=completed`, and unchanged attempt/fence.
The unchanged AR guard correctly rejects that stale admission; the worker does
not absorb this pre-existing stop-callback interleaving. Evidence:
`probe_bundle_pause_order.py`, `bundle-semantic-only`,
`bundle-order-baseline-exact`, `bundle-order-candidate-exact`. Optimization may
change arrival timing; its effect on the race's frequency is unmeasured. The
original failure remains a failure, and this ticket does not fix pause recovery.

## Evidence and limits

Local evidence directory:
`D:/meta_research/test/Funtation_model/.issue-291/`.
It contains remote issue snapshots, `probe_human_pages.py`, `probe_tools.py`,
the isolated Linux runner and per-run source manifests, dependency versions,
logs and JUnit results. Relevant runs: `human-red-valid`, `human-green`,
`human-count-before-valid`, `human-count-after-valid`, `tool-red`,
`tools-count-before`, `tools-count-after`, `tools-regression`, `material-red`,
`material-green`, `material-regression`, and `full-final`.

The final checkout's 936 source/test files and its eight changed source/test
Git index blobs match the frozen full-suite snapshot byte for byte
(`frozen-to-final-hashes.json`). The two pre-existing dirty checkouts retained
their branch, HEAD, porcelain state, index hash and all 400 fingerprinted
modified/untracked files (`before-preserved-checkouts.json` and
`after-preserved-checkouts.json`). This preservation snapshot was taken during
task preparation, before implementation; it is not a claim about older history.

Not covered: production-sized or long-running measurements, concurrent
filesystem replacement races beyond the existing contract, live model research,
frontend/browser acceptance, deployment, or #274's final same-version integrated
research cycle. Failed or unexecuted full-suite cases remain failures or gaps;
matching the baseline limits attribution and does not establish correctness.
