# #271 — task-level Writing API and readable Companion

This reopens only the 2026-11-05 scope of #271, on `origin/v1-test@57ee12d724ddd8af5c784a223f5e943796c58472`. #271 and #262 were open and unassigned at intake; no native blocker or overlapping open PR was found. The dirty original checkout was preserved in place. Historical delivery and its failures remain in [the original validation record](issue-271-validation.md).

## User path and credential boundary

The existing Writing form supports current Codex, a preconfigured `config_ref`, or `task_api` with exact endpoint, model, protocol, nonsecret instructions and `api_key_ref`. Main document/PPT choices, selected material versions, scientific basis and ordinary Quest workspace stay on the existing path.

A password can be saved separately through authenticated, CSRF-protected `POST /api/v1/companion/content-credentials`. Its response contains only a random reference. Key input uses transient component state and a dedicated request, bypassing page drafts and the generic pending-write store. The daemon stores credentials outside Companion workspaces, in a private directory/file (0700/0600 on POSIX), bound to the exact Quest, endpoint and model. This is the existing trusted local daemon security boundary, not a separate OS vault or a sandbox against a hostile process running as that daemon user. Optional `api_key_ref` repairs only the secret for that same binding; “保存密钥” does not start a new Writing turn.

An existing restricted environment is also supported: `META_RESEARCH_CONTENT_CREDENTIALS` points to a private JSON map of references to `api_key_env` and an exact `endpoints` list, with optional `models` and `quest_refs` restrictions. It does not expose arbitrary environment lookup. Existing `META_RESEARCH_CONTENT_MODELS` entries remain usable with their endpoint/model/api_key_env and default Chat Completions protocol. Secrets must never be placed in instructions or task text.

Writing model selection is stored with the first ordinary turn, atomically with native generation rotation and enqueue. The content tool resolves that selection for the current turn or a follow-up in the same generation. A conflicting selector or an external call when current Codex was selected is rejected. A new generation does not inherit the binding. Existing idempotency, cancellation and runtime fences remain authoritative; a response arriving after its fence was revoked is not delivered under the stale task. Cancellation cannot undo work already performed by an external server.

If a private credential file is lost or corrupt, replacement under its original reference requires an existing persisted task with that exact Quest/reference/endpoint/model binding. An existing credential's scope cannot be widened by this recovery. The key can therefore be corrected without replacing the original Writing task.

## Protocol and error contract

`assistant.content.generate` accepts explicit authorized text messages. For a bound Companion Writing task, only messages are needed. Research roots may supply a selected `config_ref` or task model object under their existing grant. The service appends neither Quest history nor directory contents. Optional instructions are content instructions, not executable code, arbitrary headers or a new protocol adapter.

| Protocol | Request/response |
| --- | --- |
| `openai_chat_completions` | Bearer authentication; model/messages; nonempty choices[0].message.content. Explicit truncation, tool completion or refusal is not successful prose. Legacy compatible endpoints without finish_reason remain supported. |
| `openai_responses` | Bearer authentication; model/input; completed output message/output_text blocks. Incomplete or refusal output is rejected. |
| `anthropic_messages` | x-api-key, anthropic-version 2023-06-01; model/messages, system text and max_tokens 8192; end_turn text blocks. Larger documents can be composed in bounded sections; tool calls and incomplete output are not supported as prose. |

These are nonstreaming text adapters. They do not promise arbitrary protocols, file uploads, model tool execution or multimodal content. Full HTTP(S) URLs retain the existing endpoint compatibility; redirects are rejected and credentials never follow a redirected destination. Responses are bounded to 8 MiB and secrets echoed as output are rejected. Official protocol references: [Responses](https://platform.openai.com/docs/api-reference/responses/create), [Messages](https://platform.claude.com/docs/en/api/messages).

The existing #263 diagnostics distinguish authentication, permission, rate/quota, service failure, timeout/unknown outcome, connection failure, request rejection and protocol response failure. Missing/unauthorized credential references fail before network I/O. Provider error bodies are not returned. There is one request per call, no automatic retry and no model fallback. The original Companion receives reason/correction and keeps draft, selection and evidence; uncertain remote outcomes require checking the service and existing output before another call.

## Companion and delivery

The actual Companion resource and every-turn adapter instruction now ask for coherent explanations appropriate to a reader new to the field, then technical detail when asked. Uncertainty, material gaps and formal guidance remain explicit. Conversation brevity does not rewrite reviewed full text, broaden permissions or remove necessary terminology/data/citations from professional documents. Creation/manual branches receive the same reader-level reminder.

Live acceptance found a pre-existing HC overwrite: the model supplied a full guidance draft, but the pending proposal replaced it with the human's drafting request. The overwrite is removed. The original request stays in its turn; the full pending draft is editable and retains selected strength/materials. Only the existing explicit, version-bound confirmation adopts it. Backend and browser regressions cover distinct request/draft text and a further human edit with a changed version/hash.

The shared Writing reference explains the real tool, supported protocols and recovery. The selected content model writes the requested prose; Companion remains responsible for checking, revising with that model, editable file production, independent review and actual rendering. External text alone is not file acceptance.

New Writing turns also persist the original human request as `display_message`. The conversation shows that request with expandable exact settings/source input. The original expanded message, hashes, provider input and idempotency identity stay intact. Existing turns and ordinary discussions retain their text; the UI does not guess which substrings to hide. Forward migrations 0079 and 0080 add the nullable model-binding and display fields.

## Evidence

Local current-round evidence is preserved under `D:/meta_research/test/Funtation_model/.issue-271-reopen/` (abbreviated `E/` below). Logs, source archives, individual test outcomes, raw native outputs and negative attempts are retained. These paths are local evidence, not published repository artifacts.

### Source and preserved work

| Identity | Value |
| --- | --- |
| Branch | `codex/issue-271-task-writing` |
| Base fetched before branching | `57ee12d724ddd8af5c784a223f5e943796c58472` |
| Implementation used for the full test run | `92b65d58596cd020d9b23f9a4292d8c09756f271` |
| Credential restoration and full guidance fix | `a8df81d85fea377b8eac7cfc7f3be4dcd0930b6a` |
| Task 401 self-service correction | `09a013b8bb84a227f633323a1e79cc7aba94aae2` |
| Final production source, display and HTTP compatibility | `bfe20f8187d5bc32717c51151878ae49c5096d8b` |
| Migration test-head correction (production unchanged) | `29aed82` |

The managed worktree is `C:/Users/28402/.codex/worktrees/issue-271-task-writing/worktree`. The original `D:/10-7优化实现8768/research-8768-20260915/worktree` remains on `v1-test@7e5c38928006af6bef63392bf7f54cd0a86f7b66`, with its original 18 dirty status entries preserved; no stash, reset or checkout of those changes was performed. `E/original-checkout-readback.json` records the readback. No production deployment, remote push, issue ownership change or issue closure was performed.

Remote issue bodies/comments and open PRs were read before implementation and saved in `E/issues-271*.json`, `issues-262*.json` and `pulls-state-open-per_page-100.json`. Both issues were open/unassigned; the sole open PR #221 concerned other DeepFetch work. #259's final clarification was also read and retained as `E/259-final.md`: users may provision a key in the UI, while prompts receive only its reference. This led to the separate credential endpoint instead of requiring users to configure daemon environment variables.

### Engineering checks

Backend execution used an isolated Linux source archive with Python 3.12.13 and the locked dependencies, rather than the host's unsupported older Python. Source and runner identity are audited in `E/engineering/source-provenance-audit.json`.

| Check | Result and scope |
| --- | --- |
| Companion resource/adapter regression | 24 passed; new-root, same-root, creation and manual reminder/resource checks |
| Initial Writing integration | 72 passed; exact materials, HTTP, atomicity and tool failures |
| Credential restoration | Red reproduced, then all 35 task API cases passed |
| Full guidance body | Red reproduced, then 7 backend and 2 browser cases passed |
| Final diagnostics at `09a013b` | 58 passed; task API and content diagnostics |
| Final backend display/HTTP compatibility | 48 passed; Python source, tests and dependencies byte-equivalent to `bfe20f8` |
| Final browser | 14 passed (12 Writing, 2 guidance); original human request, exact expandable input, volatile key, credential repair and full guidance edit/hash |
| TypeScript / production build | App and e2e typechecks passed; Vite build passed with its existing large-chunk warning |
| Node unit suite | 40 passed, 5 failed; the exact `57ee12d` web baseline reproduces the same five `MetaTrace` ReferenceErrors |
| Focused runtime boundaries at `a8df81d` | 133 passed, 4 failed; all four already fail in the exact baseline, two with identical messages and two differing only in generated interaction IDs |

These overlapping runs must not be added together as a count of unique tests. `writing-display-green` was captured before the final frontend build: its backend inputs equal `bfe20f8`, but its frontend/docs do not. The final frontend is independently tied to its source hashes in `E/ui-final-validation.json`, and the final wheel includes the current assets. The full test run is tied to `92b65d`; later fixes have their focused supplements above. This is not a claim that the final HEAD passed an entirely green full suite.

The task API tests send observable requests to local HTTP endpoints for all three protocols, checking exact path/model/body/auth and one-call behavior. They cover 401/403/429/5xx, invalid/refusal/partial responses, missing credentials before network I/O, exact Quest/model/destination restrictions, authenticated/CSRF key provisioning, restart/follow-up persistence, current-model denial and revoked fences before calls/after responses. The existing diagnostic tests cover timeout/unknown outcome and connection errors. A separate actual local HTTP 401 probe verifies the distinct task-key and configured-service correction paths.

The final wheel was built from exact `bfe20f8`, SHA-256 `09570f3bb0ced55cedbad4c0d4077d04ea0ef8700258e077d8313bc4b41fb857`. Its 377 packaged files and four web assets match Git bytes. An isolated installed-wheel smoke passes migration 0080 with both columns, registry/content operation, all formal resources, scoped credentials, Responses parsing and separation of credential storage from the workspace. See `E/engineering/wheel-bfe20f8/` and `source-provenance-audit.json`.

The full run completed all 539 files / 4,901 collected cases in 1,827 seconds: **4,418 passed, 481 failed, 2 skipped, 0 incomplete**. It used 12 worker processes. The reused exact-source baseline has **4,383 passed, 479 failed, 2 skipped, 0 incomplete**, from 24 workers. `E/engineering/baseline-reuse-audit.json` verifies all 1,312 baseline archive files against `57ee12d`, the same Git tree as the historical final delivery, equal runners, Python and dependency versions. It establishes source equivalence, not identical scheduling.

All 479 baseline failures recur in the same phase. Of these, 398 have identical messages; all 81 changed messages were examined individually: 32 old migration-head expectations, 25 historical profile expectations with changed formal-resource digests, 19 generated IDs/digests/addresses, three receipt digests/clocks, one runtime-binding digest and one volatile timing assertion. These are retained as failures rather than relabeled passes. Exact before/after messages and classifications are in `E/engineering/full-failure-comparison*.json`.

The two candidate-only failures are the newly stale current-migration assertion in `test_target_host_intake_retry_migration_preserves_populated_owner_records`, and `test_pause_resume_preserves_exact_root_human_wait_until_retry[run]` raising `command_preview_stale`. The migration test now expects actual head 0080 in `29aed82` and passes its focused run (snapshot `d3fdaad118344e9d435f6915bc374158381497bd9f3e0f0ee2689261c7130331`). Its populated Owner rows, foreign keys, unique/check constraints and repeated-upgrade assertions are unchanged.

The pause/resume case is a new observation in this full run, **not** a matching historical full-suite failure. Targeted instrumentation captured the background operation worker acknowledging a Bundle inbox item and advancing the Agent Runtime revision between preview and confirmation. Delaying background startup made the frozen candidate pass three consecutive runs; the unmodified final-source node also passed. A fixed interleaving then ran the actual `bundle_stage.process_once()` between preview and confirmation on both exact base `57ee12d` and final product `bfe20f8`: both advanced the revision through the same acknowledgement and rejected the old preview with `command_preview_stale`. The writer/test files and relevant HC confirmation/execution functions are unchanged between those versions. This establishes an existing background/preview race and the intended stale-preview protection; it does not prove that scheduling will always avoid the race. No production version/permission guard was weakened. `E/engineering/preview-race-diagnosis.md` retains exact commands, observations and all controls; debug instrumentation lives only in the explicitly named evidence harness, not repository code. Final engineering readback checks 37 isolated task roots and finds no remaining process (`final-process-readback.json`).

### Real native Agent, files and page

Acceptance uses the actual Codex CLI 0.159.0 and `gpt-6.1-sol` through the formal Companion adapter, authenticated HTTP surface and tool grant. A synthetic Quest was seeded through Owner fixtures in `/tmp/issue271-live-Li7CJWAd`; it is not a completed scientific Cycle. Only isolated task data/services were used. Runtime/model identity and native outputs are retained under `E/live/`.

| Scenario | Observed result |
| --- | --- |
| New root, plain-language explanation | `01a1bf13-ca86-73f0-82c0-49ecc23743ef`: clearly explains synthetic 42/100 versus 30/100, seven days, one greenhouse, no randomization/significance test; does not infer effectiveness |
| Same-root technical follow-up | Same native root; correctly distinguishes 12 percentage points from 40% relative increase, randomization from confidence intervals; invents no significance result |
| Full guidance proposal | Initial negative case exposed the overwritten body; final repeat at `bfe20f8` proves native proposal text equals stored proposal text and appears in full in the reply, with exact Question scope and strength 3; no guidance was confirmed |
| New Writing root | `01a1bf1a-7bb4-7240-a8c7-6101289e0188`, new generation with the ordinary persistent workspace and exact template/source asset versions |
| Task credential rejection | Actual Responses request returns 401 once; no retry, fallback or substantive report authoring; inputs and selected model retained |
| Same-root recovery | Repairing the same credential reference allows the original Writing root to consume endpoint prose and produce editable DOCX/PPTX; an independent native reviewer checks the files |
| Final 401 wording | Repeated on the same Writing root at `bfe20f8`; tells the user to use Writing “保存密钥” on the original reference and continue the same conversation; all 29 existing file hashes unchanged |

Native source snapshots are intentionally separate (`readability-manifest.json`, `writing-initial-manifest.json`, `writing-success-manifest.json`, `manifest.json`). Earlier successful behavior is not relabeled as an execution of the final commit. Changed guidance/401/display behavior receives a final-source repeat; the source audit binds unchanged resources to their earlier executions.

The actual report is a one-page editable DOCX with one table; the matching PPTX has two slides and six editable text shapes. The requested `TASK-271-ENDPOINT-CONTENT` marker proves consumption of endpoint prose. Counts and limitations remain consistent with the selected synthetic source. The native Agent left visual QA pending; the host then rendered the exact delivered bytes through LibreOffice and reviewed all three page images, with no clipping/overflow. Evidence: `E/live/office-structure.json`, `output-manifest.json`, `visual-qa/`, and `host-visual-review.md`.

The authenticated file API listed/downloaded 29 deliverables with matching SHA-256 and size. The built production page was then checked at 1440px and 390px using the actual exported server snapshot and exact server-downloaded bytes: all real assistant text was retained and visible, official file links downloaded the correct bytes, no JavaScript errors, horizontal overflow or mutation occurred. `page/recover-delivery03/report.json` has 74 checks; `page/guidance-fixed-final02/report.json` has 20. Original `/tmp/...` Markdown paths emitted by the model are faithfully displayed but are **not** counted as usable browser delivery links; the verified delivery surface is Companion's file list.

The final new Writing revision binds exact previous-report asset `asset_version_9ba4b5da00e043d18ebe06d568176c86`, content SHA-256 `d93222ab009a13e58b81cfbbe4d0f85f4ec2f8bc60f42f2b99f87b376256ff40`. It calls the same endpoint/model/reference, adds the requested limitation sentence, and writes `report-revised.docx` with SHA-256 `4cebb3d6608fd47751aebb91f3551d193a75aa122aadf110ce22df37b54299fc`. All 29 prior files stay unchanged; only `word/document.xml` differs inside the revised DOCX. Rendering and direct visual review confirm one page with the added sentence and intact table/layout. Original DOCX/PPTX rendering is reused only because those exact bytes are unchanged: three Office files, four rendered pages in total. `E/page/revision-display-final/report.json` verifies 24 page checks, including the exact persisted human request and default-collapsed complete model input on both viewports.

The final native visual-review turn actually inspects all four rendered PNGs, confirms the source hashes and records `visual-review.md` without regenerating outputs. In total, all nine formal turns complete across three native roots and one persistent workspace; the revision/visual root is `01a1bf30-dd53-7080-a9b0-fd3baabf009b`. Native provenance records six sessions including three reviewer children, all `gpt-6.1-sol`. Every turn receives the reader-level reminder. Exactly four task-content HTTP calls occur (401, success, 401, success), always the same Responses endpoint, selected model and credential reference. Prompts and deliverables contain no fixture key bytes; research foreground state is unchanged. Final runtime's 465 production files match the `bfe20f8` checkout. See `E/live/acceptance.json`, `native-evidence.json` and the retained native turn outputs.

Final authenticated file delivery contains 38 files, all GET 200 with exact size/hash matches. `E/page/final-visual-delivery/report.json` passes **104 checks** against stable `visual-snapshot.json` (SHA-256 `c466f3db9f8c4f438c78e02dc14d62904bf68f4b58704bb5cd9027d614983202`) and the final production build. At 1440px and 390px, all nine actual replies match in full and every line is visible, the new human request and expandable complete input match exactly, guidance proposals match, and 76 official file downloads match the 38 server-delivered files. No JavaScript errors, horizontal overflow or writes occur. The browser replays exported HTTP snapshots and exact downloaded bytes; it does not itself invoke a live provider or submit research changes.

`E/live/final-cleanup.json` confirms removal of temporary native auth/private config and the synthetic credential file, with no remaining task-owned process. `tunnel-cleanup.json` confirms task reverse-proxy port 17921 is no longer listening. Nonsecret source, logs, native output and file evidence remain preserved. The engineering audit's separate 37 task roots also have no remaining process. No production service or research workspace was modified by cleanup.

### Independent code review

Standards and Spec were reviewed separately against the fixed base and follow-up commits, using repository instructions and the code-review skill. Standards found no actionable new violation after the final `09a013b..bfe20f8` review. Spec found missing-reference recovery and later checked the full-guidance, self-service 401 and display fixes; final static review has no remaining actionable finding. The original observations and follow-up evidence remain available, including `E/engineering/standards-followup-*.md` and `E/spec-review-final.md`. Runtime acceptance and full-suite limitations above remain separate from the static review verdict.

### External validation boundary

The Companion and its reviewer are real native Agents using the actual configured Codex provider. The **Writing task content provider is a controlled local protocol endpoint**, with a deliberately fixed synthetic response; it is not a live external supplier model. These checks validate request dispatch, credential restrictions, observed errors, recovery and consumption of returned prose, not external supplier availability, quotas, billing, model quality or every provider-specific extension. No live external supplier credential was available/used for that claim. No automatic model substitution occurred.

The protocol contract is finite, nonstreaming and text-only as documented above. The fixtures are software acceptance data, not empirical research results. #274/#262 final integrated research acceptance and deployment are outside this bounded Writing change.
