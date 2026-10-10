# v4 workspace windows

Implements [#228](https://github.com/15253a/meta_research/issues/228) from
`v1-test@41908b78e56a7b3c8bb22553a955abef6defde28`, using Matt implement,
TDD at existing public API/browser seams, and one Standards/Spec review.

Quest creation keeps the continuous brief beside its route and independent
Companion. HumanRequest keeps independent assistant work beside the original
request and formal response. The explicit response review shows the exact
request/revision, selected text/materials and decision before submission.
Authorization acceptance is reviewed before the existing authorization ladder;
decline and defer do not carry allowed materials. Native nested dialogs keep
Escape and focus within their current layer.

“修改配置” retains research style, time, exclusions, GPU and advanced text.
Search sources and library/full-text settings belong to DeepFetch; generic MCP
remains a system setting. Shared sources and per-Quest selection still use #215.
Connection testing, shared saving and Quest selection are separate operations.

## State and versions

Unsent inputs, unsaved connection forms, selected materials and reading positions
are page-local. Quest initialization, HumanRequest identity/revision and Quest
configuration determine their scopes. Closing a window does not send anything.
Refreshing continues the existing saved server state and sealed IndexedDB
response recovery; page-local editing is not cross-device persistence.

Runtime conditions retain their existing CAS revision. A separate
`literature_configuration` in the same version stores `mode`,
`library_entry_url` and `institution_required`. The URL is validated by the
existing library validator and excluded from the rendered model runtime text.
New manual and autonomous DeepFetch requests read saved configuration. Queued requests and
replays retain their frozen configuration; a different configuration cannot
replace an active acquisition session. No new retrieval provider is introduced.

The research assistant uses the existing #233 understanding/revision/hash
confirmation contract. Explanations stay in the Companion. Every reviewed field
and optional material belongs to the same confirmation. Changing them requires
a new server revision; exact retries retain the same body and idempotency key.
Strength 5 alone does not imply whole-Quest goal evolution.

## History

The timeline has an independent vertical scroll region and a browsing Quest
separate from execution foreground. `/api/v1/research-timeline/quests` enumerates
existing accepted Quests with offset/limit pagination. Existing research-library
pages enumerate formal Questions, including those without Cycles and readable
historical lifecycle states. Existing overview reads provide all Cycles for the
selected Quest. Loading and failed reads remain visible; refreshes do not move
the reading position. “返回当前工作” explicitly returns to live work.

## Verification boundaries

New regression cases are in `human-request-v4`, `guidance-confirmation`,
`quest-intent-session`, `runtime-conditions`, `external-mcp`, `search-sources`
and `research-timeline` browser files. `workspace-v4-product` captures all three
windows at desktop, low and narrow sizes against the real isolated HTTP product,
with deterministic external providers. `test_runtime_library_web` and
`test_timeline_quest_history_web` exercise authenticated public reads/writes.

The final delivery record lists exact trees, results, screenshots and baseline
failures. Route-fixture screenshots demonstrate isolated browser state/layout;
they do not demonstrate an external provider or model call. Existing #212,
#215 and #233 evidence covers unchanged source/material/provider contracts.
This ticket does not deploy or restart 8768.
