# Scientific asset lifecycle

Research Memory keeps each exact content version and its original acceptance receipt immutable. A separate asset revision and receipted change describe the scientific interpretation. One explicit pointer selects current content. An old exact reference always reads the original bytes.

Supplement and substantive change use the existing intake with `asset_ref` and `change`. The change contains `kind`, `predecessor_version_ref`, `expected_revision`, and `explanation`. Acceptance commits the content, explanation, revision, and current selection in one fenced transaction. A competing request against the same revision fails with `asset_revision_stale`.

Correction also records `error`, `scope`, exact accepted `evidence_bindings`, and its impact assessment. Each per-work `impact` contains `work_ref`, `judgment`, and `explanation`. Judgments are `unaffected`, `recheck`, `redo`, or `unknown`; use `unknown` for an actual work whose impact still needs investigation. When `impact` is omitted or empty, a nonempty `no_affected_work_explanation` must state the checked scope and why no work is affected. The owner returns `asset_change_no_affected_work_explanation_required` when that explicit assessment is missing. It does not infer affected work or require invented work references. Existing research uses remain intact. Accepted correction evidence protects its exact basis from retirement. The owner does not claim that a stored judgment completed the required recheck or redo.

Retirement requires an explanation, explicit confirmation of low value, obsolescence, and error, understood impact, and no remaining explanatory value. The writer checks fresh research references, pending accepted input custody, holds, and the expected asset and reference revisions. An eligibility assessment is an observation, never a retirement permit. Failure, negative findings, and changed goals do not trigger retirement.

Accepted Idea, Plan, and Reasoning content retain exact asset sources in their scientific documents. An intake origin permits Quest access without inventing a scientific role. When an agent chooses that material as a new scientific basis, the final content writer checks usability under the same fence as retirement. Retirement verifies the retained document metadata and its existing acceptance receipt. Historical resolution and replay keep the original source identity when current changes.

Accepted retirement removes the selected version from current and rejects new uses. It retains managed objects, shared directory entries, linked originals, and historical explanations. This implementation does not reclaim bytes. A retained exact version remains readable with its retirement notice. A same-key replay returns the original fact; a changed payload conflicts.

## Public operations

| Operation | Entry |
| --- | --- |
| Atomic intake and revision | `POST /api/v1/research-assets/intakes` |
| Exact metadata and lifecycle | `GET /api/v1/research-assets/{version_ref}` |
| Exact original content | `GET /api/v1/research-assets/{version_ref}/content` |
| Logical lifecycle | `GET /api/v1/research-assets/assets/{asset_ref}/lifecycle` |
| Explicit current selection | `GET /api/v1/research-assets/assets/{asset_ref}/current` |
| Retirement | `POST /api/v1/research-assets/{version_ref}/retirement` |

The retirement body contains `expected_revision`, `expected_reference_revision`, `explanation`, `low_value`, `obsolete`, `incorrect`, `impact_understood`, and `has_explanation_value`. A blocked response exposes reason codes and the actual reference and hold identities.

Root agents discover `research_memory.assets.page`, `.lifecycle`, and `.current`. They use `.intake` and `.retire` with their corresponding `.reconcile` operation. Every operation verifies the current Quest and runtime fence. Intake records the accepting Quest as an immutable origin fact. That access fact does not manufacture a scientific use. Exact content uses the existing `research_memory.content.read` tool.

A semantic intake durably records that it requires an effect scope in its canonical request. After a transient storage failure, it remains queued until an authorized caller retries the same effect ID and payload with its current runtime scope. Background workers skip these jobs. The final fenced acceptance checks that scope and the original asset revision and predecessor again. Reconciliation reports the queued state; it does not grant acceptance permission.

## Migration and verification

Migration `0060_asset_lifecycle` leaves previous content and receipts unchanged. Legacy versions remain `unselected` with revision zero. No latest version becomes current by inference. The first explicit change selects its stated predecessor and creates an accepted current selection. Owner-bound formal content remains protected by its existing owner.

The public owner probes are in `tests/test_public_asset_lifecycle.py`. HTTP and actual semantic dispatch share the contract in `tests/test_asset_lifecycle_adapters.py`. That suite also checks readiness with the expanded current operation catalog. `tests/test_asset_lifecycle_protection.py` verifies origin-only Plan and Reasoning sources, Target production, pending inputs, completion protection, human responses, and shared directory entries with real owner fixtures. Backend tests run on Linux because the runtime requires `fcntl`. Workspace handoff notes provide the isolated Linux runner. Frontend checks use `npm test`, `npm run typecheck`, and `npm run build` from `web`.

The local engineering checks use temporary content and independent data roots. They do not establish long-term scientific benefit or mutate deployed research assets.
