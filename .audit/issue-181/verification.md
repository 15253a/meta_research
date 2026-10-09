# #181 verification

The implementation passes the required ticket-local Agent, transport, browser, restart and affected regression checks. Publication and final trail closure are finishing.

Base: `546bfa88b6796d1d510e20f7c819ecad118e97e4`, remote `v1-test` at claim time. Product commit: `e85197c7c31b653624614b907eb33d69074abb90`. Product tree: `72ba9df7b7a821d222218a63f8ed259fe395d95a`. Route: pstack 0.15.9 Feature. No Matt workflow or extra review round was added.

## Implemented behavior

[Quest goal ownership](../../src/meta_research/owners/quest_goals.py) commits the goal and complete completion criteria in one immutable revision. Idea, Plan, Bundle, Target, and Reasoning use signed Owner context and frozen inputs. A shared SQLite writer fence governs evolution, completion and public runtime-condition changes. Accepted-effect replay precedes current-head rejection. Changed replay payloads conflict.

Historical feedback remains unchanged. Current alignment follows accepted ancestry. Historical detail retains sourced conditions, assessment, inactive membership and supersession sources. Current cuts exclude superseded conditions. Ordinary notes retain canonical RM custody and exact AR provenance. Cancellation acknowledgment gates noncompletion handoff. Goal changes reopen Reasoning at a higher epoch while preserving old candidates, Plan facts and stage progression. [WorkspaceMain](../../web/src/main.tsx) renders goal, full criteria, alignment, work and history. [Bundle continuation](../../src/meta_research/bundle_skill.py) reaches existing verified recovery after completed-input drift and requests an ordinary Owner successor. Pending, foreign-root and damaged records cannot authorize correction.

## Selected backend regression

Pinned Linux Python 3.12.13 run `run-20261009T064126.301359Z-08945688` collects and passes **188 tests**, with zero collection errors, failures, errors or skips. Its independent checkout verifies 1,024 source files before and after with an intact seal. Production writes, service controls and model calls are zero. This is the affected selection, not the full repository suite.

The selection includes twelve original files with 171 tests, all 14 [Bundle input-drift regressions](../../tests/test_bundle_input_drift_recovery.py), and three affected compatibility nodes. Execution SHA256: `90c0022c48d9cc9a879d43a4e56874638dcfd47528de05b42ff2458bc8270478`. Seven-scope manifest SHA256: `0c4bcbb286f73bba38ea7ef5ab45efa2b6d7d597953cea38318e475650bb3221`.

| Scope | Passing counterexample |
| --- | --- |
| Default cancellation | [Harness cancellation](../../tests/test_harness_cancel_control.py) resolves Owner identity to an actual signed transport process and checks stopped exit and replay. It uses a narrow Owner facade. |
| Cancelled handoff | [Signed Target operation](../../tests/test_human_guidance_providers.py) retains exact note and source-backed handoff after lifecycle acknowledgment. Physical cancellation is covered separately. |
| Concurrent writers | [Completion races](../../tests/test_quest_goal_completion_race.py) cover both final writer orders against evolution and public conditions. [Authenticated roots](../../tests/test_quest_goal_concurrent_roots.py) accept one writer, reject the stale writer usefully, replay accepted effect after the head moves, and reject changed payload. |
| Stale completion | Evolution-first and conditions-first races reject stale completion while the old candidate stays readable. |
| Condition omission | Signed Target mutation rejects missing sourced-condition accounting. |
| Frozen facts | Higher Reasoning epoch preserves requests, StageCommit and Plan rows. This is not a blanket proof of every Target contract. |
| Bundle continuation | Completed drift reaches verified recovery across dispatch, batch, inline and durable transports. Pending, foreign and damaged paths fail. Actual successor has distinct attempt, fence and operation with the same native session. Original signed bytes stay unchanged. Three compatibility nodes pass. |

[Public history](../../tests/test_quest_goal_history.py) verifies historical conditions and supersession review. Overlapping cases count once in the 188 total.

## Frontend verification

The preserved native frontend run passes **32 Node tests**, with zero failures, cancellations or skips. Application and E2E TypeScript checks pass. Typecheck stderr is an npm update notice only. Node-test stderr is empty. No update was performed.

The final continuation commit changes only Bundle Python and its test. Passing frontend and final product trees share Web Git subtree `f8971d749acc32b3641f292bc2afa1a8ae618a4a`. Unchanged Node and TypeScript checks were not repeated. Fresh final-tree build passes, verifying 3,082 dependencies before and after with zero source or dependency writes. JavaScript SHA256 is `03146153c74475a9ba98d9cc28a492d9736beac06604506f21c026dce2cfdb4f`. CSS SHA256 is `0356529acbb8db3dac9f16722b6013039813e64a8bf0443f5ea008f86f70c4a4`. Both match the passing frontend.

## Actual Agent and durable browser acceptance

The final isolated fixture was prepared at `2026-10-09T06:41:16.092667Z` from the selected product tree. Native app-server responses select GPT-6.1 Sol without recorded reroutes. The upstream provider does not independently report a response model. Pending uses six MCP calls, exit 0. First evolution uses eight calls, exit 1 solely retaining the deliberately lost confirmation. Reconcile and identical retry recover one accepted version. Later direction uses seven calls, exit 0, on a different signed operation automatically admitted by ordinary Owner correction.

All **21 native MCP calls** are attributed to audited HTTP requests through actual `_meta.callId`, with thread and native-item identity. No Code Mode dispatcher outputs were emitted. The final positive result verifies actual transport, scientific judgment, durable product semantics, browser observations and all seven selected backend scopes at `2026-10-09T07:03:32.381092+00:00`. Its SHA256 is `37f30891ded5ba7a2fe2d120054560bfa0e256a03c671ad123ce860e94b2f10a`.

The root read actual arguments, results and scientific judgments, and inspected five selected screenshots. Calibration A physically continues at pending, evolved and later captures. Reconstruction B is acknowledged stopped. B's canonical 601-byte note is accepted through ordinary RM intake and read completely. A controller separately accepts its Quest role through public HTTP. An accepted same-Quest evidence cause changes the goal and full criteria to sequence 2 while preserving the original sourced clause. Queued C stays unstarted and Plan rows stay frozen.

The owned browser proves pending, evolved and later states, full criteria, alignment, work, history and second-context refresh. Actual restart keeps the same port, Quest and persistent data, changing generation `064208` and PID `752138` to generation `070054` and PID `846130`. Restore makes zero new admissions or provider calls. A fresh browser reads the restored state. The original observer's premature `restart` label was a same-process reload and is excluded. The first actual restore attempt failed because cleanup had stopped A. The final support reader explicitly reads historical state without a live handle. Product source remains unchanged. Restart proves persistence after cleanup, not A physical survival after cleanup.

The tiny synthetic sample proves engineering behavior. Pixel MSE is not calibration evidence. It establishes neither clinical validity nor generalization or formal scientific completion. The root does not claim unobserved A values or accepted custody of the raw JSON referenced by B's note. The micro stop adapter uses actual OS signals and signed exits with product identity mapping. A separate backend test covers the default product supervisor.

## Required judgment and preserved failures

Candidate X has thirteen ordered product commits. The root read all 8,739 candidate-diff lines and every later increment. The original P1 historical-reader and final condition-fence findings are repaired. With actual acceptance complete, the root score is 18/18.

One no-comments pass covered 77 files, removed 36 comment or docstring lines in nine files, retained 17 contract comments and checked unchanged noncomment ASTs. Actual Opus implementation stopped at its CLI limit with an incomplete five-file patch, no selectable commit and no complete test receipt. It contributes no implementation graft. Design-stage B grafts are separate from that dropout.

Actual Opus judgment returned insufficient-credit HTTP 403. Future work follows the user's GPT-6.1 Sol requirement. Independent trail closure uses a separate GPT-6.1 Sol agent under the same-model exception. Historical request or self-reported labels do not prove another upstream family.

Setup, schema, continuation and restore failures remain preserved. Prior 061343 did not admit its later operation within 360 seconds, so the whole fixture failed. Native Wait returning did not establish AR acceptance. The continuation repair and new complete fixture replace that attempt rather than combine successful old fragments.

A historical auxiliary completion, service and Reasoning run had 15 passes and five failures at obsolete `harness_child_agent`, rejected as `idea_review_mode_invalid`. Only one failing case was independently reproduced on the clean base. The other four were not. Those failures remain outside the final 188 selection.

The implementation is not deployed or merged. Production 8768 was not restarted. Final recorded identity matches pre-work. Readiness returns HTTP 200 with overall unavailable. Asset-verification timeout remains, and autonomous-creation timeout is observed again after an intermediate ready reading. Their cause is not inferred or repaired in this ticket. Stable identity does not establish zero unrelated production activity or no shared-host resource impact. Final owned server and A/B process identities are confirmed stopped. Publication and final trail closure are recorded separately.
