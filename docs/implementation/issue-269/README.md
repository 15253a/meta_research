# #269: purpose discovery and exact contracts

This change starts from `origin/v1-test@b98d7443ebbb4ddd27b79d6ecc6c48c44cfaa567` in an isolated worktree. The source checkout's existing edits were retained. GitHub #269 and #262, their comments, native blockers and open pull requests were read before branching: #269 was open, assigned to `15253a`, without native blockers; #262 was open and unassigned. The sole open PR, #221, concerned separate DeepFetch work. #263 and #291 were closed and already included in the base; #293 remained open.

## Behavior and boundaries

- Ordinary root providers see `tools.catalog.page`, `tools.contracts.read` and `tools.call`. Initialization supplies the current granted purposes and at most three common operation summaries. A purpose page expands its operations and topic readers; a known identity can be called immediately. Narrow channels without discovery grants retain their actual directly exposed contracts, such as the recorder's one read-only source.
- `tools.call` is a transport projection. It selects the original identity before the existing grant, schema, handler, output and error checks. It creates no semantic grant, effect identity, authority store or mandatory discovery step. Original MCP `tools/call` identities remain valid.
- RG exposes Question, Baseline, Literature, Dataset, Environment and Human Interact as available to the root. Current guidance is in HC; historical human input is reached through the existing `human_request.read` contract. RM separates exact content, custody/lifecycle and material delivery. WF spans existing Owners and also reaches the original registered reconciliation operations for unknown outcomes.
- Navigation is derived from actual registered operations and current availability. An operation can appear under several purposes but retains one identity and contract. Query/purpose/topic/view and current availability participate in pagination identity. Stable contract hashing from #291 is retained; live authorization is never cached by navigation.
- External MCP starts with configured service summaries, then reads a service's operations or a tool's precise contract on demand. Its projected call returns the original service result/error. Original exposed names remain directly callable. Frozen operation connection/contracts do not freeze service removal or role revocation.
- Workspace file/shell access remains native, through the host's actual paths and scope. No RW tool or parallel native capability registry was added. Runtime references were checked against existing directory/history implementations before removing outdated engineering placeholders.

## Requirements to implementation and evidence

| Requirement | Production path | Verification |
| --- | --- | --- |
| Purpose overview, few initial summaries, on-demand contracts | `semantic_mcp.py`, `tool_navigation.py` | `test_tool_purpose_navigation.py`; actual model initialize/list and call audit |
| All current role operations remain reachable | same registry and root grants | eight-role purpose/page union in `test_tool_discovery.py` |
| Direct reader, precise diagnostics and original effect recovery | `tools.call` enters existing dispatch; `harness.py` waiting-scope check | public HTTP tests, real model known reader, version correction and unknown-effect recovery |
| Current permission, disabled handler, changed pages | existing gateway grant/availability with purpose filters | discovery registry/integration tests and purpose authority test |
| Current HC versus historical Human Interact; exact RM versus working copy | original HC/RG/RM handlers and native directories | real Idea-root model reads, feedback receipt and unchanged working-copy bytes |
| External service discovery, exact contract, actual call | `external_mcp.py` over the frozen service snapshot and current authority | real stdio/HTTP fixture tests; actual model calls a controlled stdio service |
| Loaded method and contract instructions agree | research navigation, custody, Bundle/Plan/Reasoning and external recovery references | semantic review and bounded resource checks |

The complete #263 tool inventory and recovery evidence are reused from `docs/implementation/issue-263/`; they do not claim newly executed business calls for every tool. #291 supplies the isolated Linux runner and stable-registration/hash regressions. The new tests check that purpose navigation retains every available operation in each role grant. Native tools retain their actual errors and host permissions.

## Validation record

The machine-local evidence directory is `D:/meta_research/test/Funtation_model/.issue-269/`; the reused Linux runner stores distinct immutable runs under `.issue-291/runs/269-*`. `verify_real_agent.py` records the exact source/test manifest, initial state boundary, real provider commands, prompts, model JSONL, MCP wire calls/results, independent assertions and credential cleanup. Full raw research/provider logs remain local.

The real consumer uses a newly created isolated data root, an admitted Idea provider unit and the installed Codex CLI/model. Initial research state and the platform lease are controlled fixtures; business calls use production Owners. A deliberately damaged post-commit receipt tests unknown-effect recovery without replaying the effect. The external service is a real controlled subprocess, not an unconfigured production supplier. This is bounded #269 acceptance, not #274's complete Cycle or a deployment claim.

[Actual Agent acceptance](REAL_AGENT.md) records the complete five-turn run and the final focused discovery run separately, including their exact source delta. The latter consumed the clarified AssetVersion reader contract and read the original in one attempt without guessing its logical asset identity. `source_head` in these receipts is the base Git HEAD while work was uncommitted; archive hashes and source manifests identify the actual tested candidate bytes.

The installed Python project has no configured static typechecker. Linux uses Python 3.12 and the locked dependencies; frontend TypeScript checks run separately. Failures in the inherited full suite remain failures even when reproduced at the exact base.

The full Python run completed all 538 files: 4,856 collected, 4,373 passed, 481 failed, two skipped and zero incomplete. Of the failed nodes, 480 also failed in the reusable baseline. The sole new failure was a packaged-guidance assertion that required a Markdown link without a section fragment; the test now checks the linked document while retaining its packaging/content checks, and its complete file passes all ten tests. The retained full-run counts are unchanged.

The full suite was frozen before the final review fixes. [Its six runtime-file differences](full-suite-final-source-delta.json) are covered by separate final regressions: external MCP (47 passed), WF/authorization/recovery (14), exact contracts (16), packaged shared resources (2), public HTTP/creation corrections (4), and the final human-reply guidance file (10). These scopes overlap and are not added into a synthetic suite total. The final real discovery run matches all 374 current runtime files.

Frontend app/e2e TypeScript checks passed. Frontend tests had 40 passes and five failures; all five `MetaTrace is not defined` failures reproduce from the exact task base with the same dependencies. Both independent review axes, Standards and Spec, have no remaining actionable findings after their fixes and rereviews. The original checkout's HEAD and dirty status still match their retained receipts.

[Final run summaries and review disposition](validation.json) and the [failure comparison](failure-comparison.json) preserve the inherited failures, changed diagnostic values, independent retests and evidence boundaries. This is not a claim that the repository's full suite is green.

## Remaining integration ownership

#274 owns final same-version Cycle and downstream integration; #262 owns overall completion. This implementation does not close those issues or imply their acceptance. #293 owns dynamic remaining budget and extension approval: no nonexistent budget service is advertised by this catalog. Newly registered and granted operations remain discoverable through their real registration. Existing deployment/data and the unrelated in-flight work were not modified.
