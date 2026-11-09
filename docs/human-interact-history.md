# Human Interact history (#267)

This change implements the reopened Human Interact scope of [#267](https://github.com/15253a/meta_research/issues/267), against `v1-test@d3200f7149326003660df5aa1c10430d4496c904`. The existing guidance owner, history readers and query revisions remain authoritative. No storage migration or additional historical store is introduced.

## Read path

1. Use the authorized purpose/operation catalog to obtain `human_request.read`. The initial human history response points to `formal_guidance_reader`; select `entry_kind=guidance` to discover confirmed guidance.
2. Follow a guidance item's exact `constraint_ref` and `version_ref`. Its body includes the confirmed text, strength, semantic scope, current status, treatment count and `treatments_reader`.
3. Follow that reader with `entry_kind=treatments`. Treatment pages retain the actual root kind, run and operation. Each item points to an exact feedback receipt using `treatment_ref` and `version_ref`.
4. Read the receipt for the original handling decision, understanding, reasons, research changes, continuing work and declared goal impact. A goal-impact declaration does not establish that a Quest goal update has been accepted.

The immutable feedback effects preserve earlier assessments after a root resolves them. The mutable `hc_guidance_treatments` pointer and a current root's `prior_treatment` are not the complete history. Returned content readers also use the existing `research_memory.content.read` path, enabling the authenticated research-library page to consume the same Owner records.

## Boundaries

- Root authorization and the selected authorized Quest are checked on every tool read. The authenticated Web API follows its existing account and Quest access boundary. Historical access grants no new write operation.
- Exact feedback reads verify receipt content and its guidance/delivery/root binding. Unknown sources, wrong versions and unreadable records remain errors.
- Each directory has its own existing query revision and continuation. New feedback invalidates affected guidance/treatment continuations; a changed query must restart. A no-record result differs from an unprocessed guidance record or failed read.
- Reads do not set delivery received/read timestamps, submit treatment, resume HumanRequest waiters or apply goal evolution. These effects remain on the existing HC contracts.
- The human research-library entry includes submitted materials, requests/replies and formal guidance. Guidance and request history have independent continuations. Exact originals and treatment details are opened on demand.

Bundle, Reasoning and shared navigation resources describe this same path. `references/research-navigation.md` is the detailed role-facing source for historical navigation.

## Verification boundary

Regression tests cover real Owner writes and exact history reads, Web API integration and frontend consumption. Bounded browser acceptance uses a fresh production runtime and database, real HTTP routes, and the built frontend; deterministic scientific providers prepare the records. This is distinct from the final deployed, model-driven research chain required by #274/#262. Those overall acceptance requirements remain open.

Validation on 2026-11-09:

- Six related backend modules: 36 passed. Both new history/Web API test files on the final implementation: 5 passed; these selections overlap.
- TypeScript app/e2e checking, production frontend build, fixture browser regression and actual HTTP-to-Chromium desktop/mobile acceptance passed. The latter reads three preserved feedback receipts across two roots, guidance pagination, pending guidance and thirteen replies over two pages.
- The frozen full backend run completed 4,911 nodes: 4,428 passed, 481 failed, 2 skipped, none incomplete. Its snapshot contains an early new-test assertion expecting a semantic error for a retired root; the implementation correctly returns HTTP 401. That assertion was corrected and the affected files rerun successfully. Full-run failures are retained rather than rewritten as passes. Production source did not change after the snapshot.
- The full Node suite has 40 passed and 5 failed; all five reproduce on the exact base with the existing `MetaTrace` test-harness failure. Selected relevant backend failures also reproduce on the exact base; remaining broad-suite failures have not all been attributed and are not claimed resolved.
- Wheel packaging verified all 377 package files against committed source bytes, including the five updated role/shared resources and four built frontend files.
- Independent review found no confirmed specification defect or hard repository-standard violation. One low-priority suggestion concerns sharing the short, independent guidance/request pagination request lifecycles; no correctness defect was identified there.

The implementation changes no deployed service or existing research data. The isolated acceptance service was stopped after verification. Publishing the source does not establish the final deployed acceptance required by #274/#262; ticket status and the current delivery conclusion are maintained on GitHub.
