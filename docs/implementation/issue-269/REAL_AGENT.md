# #269 actual Agent consumption

The installed Codex CLI `0.159.0` and `gpt-6.1-sol` completed the bounded acceptance through the production `CodexHarnessAdapter`, live Idea provider unit, current scope-bound grants and actual Owners. The initial Quest, human records, retained material and platform lease are controlled fixtures in new isolated data. No deployed service or existing research data was changed.

Two runs establish the final evidence. Their base HEAD is `b98d7443ebbb4ddd27b79d6ecc6c48c44cfaa567`; because implementation bytes were not yet committed, source identity is the frozen archive and per-file hashes, not that base HEAD alone.

| Run | Actual results | Frozen source |
| --- | --- | --- |
| Full consumer, `consumer-final01` | All five turns, 47 tool calls; all arguments and text/structured results match the completed CLI events. Three failures were observed and recovered; they are not hidden as successful calls. Three native commands. | Archive `e8ab71ea9c992a60031ceadafb68ea1ebd19a51e4e92df1c86284a61a92dbde3` |
| Final discovery, `consumer-final-discovery` | One targeted turn, 22 tool calls, all completed CLI results match HTTP; zero failed calls. Two native commands. The actual model reads the updated content contract and reads the retained asset with one exact-version call, with zero logical-asset-reference guesses. | Archive `208134b730ef0ab2eb174e0e7cd5e93dca749d8cf10d3bd30a408d305f550580` |

The full consumer is original session `01a1bc93-f5fc-72b3-8a69-5f853a769961` across all five turns. The final targeted consumer is session `01a1bc98-6504-79e1-8f86-c472665a5e82`. The latest frozen `src/` bytes match the final candidate across all 374 runtime files. [Source reuse](source-reuse.json) records every changed file and its exact diff: 372 files are unchanged; `research_content.py` adds precise AssetVersion field/error guidance; `external_mcp.py` adds a configuration-error explanation and rejects non-string JSON-RPC methods earlier. The malformed-method and configuration-contract branches use separate final regression evidence; the successful external model run did not consume those last additions.

## What the model actually did

The full run's HTTP sequence numbers identify the retained evidence, not a proposed recipe.

| Path | Observed consumption |
| --- | --- |
| Provider first surface | Actual `initialize` supplies currently granted purpose summaries/common entries. Actual `tools/list` contains exactly `tools.catalog.page`, `tools.contracts.read`, `tools.call`. The installed CLI then uses these tools; business schemas are obtained only in requested exact contracts. |
| RG historical human guidance → current treatment | The model selects RG/Human Interact and HC, reads the historical instruction by its precise hash, distinguishes it from the current request/reply, and submits legitimate current guidance feedback at sequence 24. Sequence 25 reads current goal assessments containing that feedback's exact receipt; a later feedback reconciliation reads its actual committed treatment. |
| RG material → RM exact original | HumanInput leads to its retained attachment. Sequence 23 reads `ACCEPTED_ORIGINAL_269: the negative control did not reproduce the effect.` from the actual RM version. It remains distinct from the tentative working copy. |
| Known reader | Sequence 33 directly calls the already-known original operation/arguments through `tools.call`; no catalog or contract call precedes this read. |
| Correctable reference failure | Sequence 37 attempts the supplied stale version unchanged, receives `content_version_unbound`, then the same model discovers the current question reader and reads the actual accepted version. |
| WF unknown outcome → original reconciliation | The model invokes literature registration once. A controlled wrapper corrupts only its post-commit response; the real Owner executes once. After the unknown-result error, sequence 52 reads WF and receives an item with the **same** `research_memory.literature.register.reconcile` identity. Sequence 53 invokes that entry with the original effect, obtains the actual committed result, then sequence 54 reads the retained literature record. |
| HC current request/reply | Accurate request and response readers return the real fixture reply `CURRENT_REPLY_269: instrument recalibration is complete.` The model preserves the distinction between a provided human reply and an Owner's disposition, and between that reply and the historical guidance. |
| Native workspace | The model reads the actual host-issued current workspace with native commands, observes `UNACCEPTED_WORKING_COPY_269`, and leaves the working-copy bytes unchanged. A fresh Quest has only one actual current directory; no prior-cycle directories were fabricated. Existing directory/history tests cover the previous-two-cycle boundary, not this model run. |
| External service → contract → actual result | Sequences 61–64 perform configured service overview, service operation discovery, exact contract read and actual `read_temperature` call. The real controlled stdio subprocess ledger records exactly one business call, sensor `acceptance-269`, returning `23.75` Celsius. No connectivity action is invoked. This is not evidence about an unconfigured production supplier. |

In the full run, an additional spontaneous mistake preceded the RM read: the model guessed a logical `asset_ref` as the content source and received `content_source_unbound`. It recovered by obtaining the correct asset reader. This observation prompted the final contract clarification; it is preserved rather than counted as a first-attempt success.

The final targeted run uses the **same natural task**, without adding the correct `source_ref` to its prompt. A post-turn assertion checks only calls whose `version_ref` is the retained fixture asset's version; HumanInput document readers are not mistaken for asset-original readers. The model receives the updated `tools.contracts.read` response, uses the correct version identity immediately, reads the original at sequence 18, records feedback at 22 and reads its exact impact receipt at 23. This is one successful observation of improved use, not a claim that all future agents will avoid every error.

## Reviewable artifacts and limits

- [Full consumer call/result verification](full-consumer/real-agent-consumption.json) and [actual provider initial surface](full-consumer/provider-initial-surface.json).
- [Final discovery call/result verification](final-discovery/real-agent-consumption.json) and [final actual provider surface](final-discovery/provider-initial-surface.json).
- [Exact runtime source reuse](source-reuse.json) and the [reproducible probe](verify_real_agent.py).

The consumer summaries retain exact submitted arguments, result digests, failure codes, native command evidence and credential cleanup. Every exported result was independently matched against a completed actual CLI event, including its text and structured representation. Initial MCP responses plus those observed calls establish the installed provider's consumed tools; no raw Responses API request capture is claimed. Complete prompts, HTTP audits, CLI JSONL and source archives remain in the machine-local `.issue-269/consumer-*` directories. Earlier `initial-01` records a probe wiring error before model startup; `initial-02` preserves the first successful run before strengthened WF/impact assertions.

Each run uses a new isolated workspace/home and dedicated reverse proxy. Temporary `auth.json` is removed after the process, with zero remaining copies at the copied path; the controller stops its own tunnel. Original credentials and deployment remain untouched. These probes establish the specific #269 role-root behaviors; they do not claim a complete research Cycle, production deployment, every tool's business branches or #274 completion.

Run the probe on Linux with the locked test/runtime dependencies, an installed Codex executable and an isolated authorized provider home. Required arguments are `--source`, `--source-head`, `--output` (must not exist), `--codex-executable`, `--codex-home`, and `--model`. `--prepare-only` checks fixture/grant/service setup without a model. Default `--scenario all` exercises the five turns; `--scenario discovery --require-exact-asset-reader` performs the final targeted consumption check. The extra assertion does not alter the model task or grant.
