# Implementation baseline and local verification

[#170](https://github.com/15253a/meta_research/issues/170) restores the editable
source and existing verification entrypoints for
[#169](https://github.com/15253a/meta_research/issues/169). This is direct
implementation: source identification, environment preparation and test command
wiring belong to one preparation ticket.

## Source and deployed version

The implementation checkout was cloned from `15253a/meta_research`,
`v1-test@1578ca19220eaddb54ffb6a8de256655660dc1f0`, on 2026-10-07. Its working
tree was clean before creating `codex/issue-170-implementation-baseline`.
On Windows, set `git config core.autocrlf false` in this checkout to preserve
the baseline bytes when exporting or comparing files.

At 05:08 China Standard Time, a read-only audit of the 8768 host found:

- Deployment metadata: `64cc92c624e17f424e290911fff2274b48261cca`.
- Wheel SHA-256: `d6dffdd9d48edb1489873f0b481c8f4f397b79d6f0809766678c3350df0db3e0`.
- Listener: `127.0.0.1:8768`, PID `644379`, process start ticks `2650708694`.
- All 927 shared source files were byte-for-byte equal to the Git baseline.
  The checkout's nine extra files were its README and baseline/memory documentation.
- All 318 application package files matched the deployed source, installed
  package and wheel. The wheel's 321 hashed RECORD entries and the installed
  environment's 326 hashed RECORD entries verified. Metadata and file hashes
  remained stable across the observation.
- Internal readiness returned HTTP 200 with overall `unavailable`:
  `autonomous_creation_operation_timeout` and `asset_verification_io_timeout`
  were observed on their respective workers. These are existing live-service
  observations, separate from isolated verification results.

The commit identifiers differ; the byte comparison establishes file equality
and does not establish a Git ancestry relationship. This preparation changes no deployed
package or production data. Deployment and frozen research evidence remain
in their original locations. The audit used the existing internal readiness
read endpoint; it did not create an authentication session, restart the service,
run a model, or start research. Ports 8767 and 8766 were not listening.

## Isolated public service behavior

The backend uses Linux `fcntl` locks. Native Windows currently fails test
collection with `ModuleNotFoundError: fcntl`; do not replace those locks with
mocks to claim runtime behavior passed. Run backend and deterministic-product
browser scenarios on Linux, using a separate checkout and data directory.

Use Python 3.11–3.13, the checked-in `uv.lock`, and the existing dev group:

```sh
uv sync --frozen
uv run --frozen python -m pytest tests/test_public_research_assets.py::test_managed_text_intake_is_exact_readable_and_idempotent -q -p no:cacheprovider
```

This existing test builds the production runtime against pytest's temporary
data root. It accepts a managed text asset, repeats the idempotent write,
queries inventory, reads back the exact original bytes, and rejects conflicting
reuse of the idempotency key. It closes the runtime afterwards. It neither
connects to 8768 nor calls a real research Agent.

For an explicit data location, add `--basetemp /tmp/<new-ticket-test-directory>`.
Use a new disposable directory: pytest owns and may clear `--basetemp`.

## Frontend behavior, types and browser

See [frontend verification](../web/README.md) for the actual Node behavior
command, separate typechecks, browser selection, Linux deterministic product
and Windows browser-only scenario. Select affected functional tests; the fixed
raster suites are separate projects, not a per-ticket prerequisite.

On machines without system Chrome, the existing Playwright version can install
its browser with `npx playwright install chromium`; set `META_RESEARCH_CHROME`
to that Chromium executable. An already installed compatible browser can be
used directly. No new test framework is needed.

## Execution evidence and limits

The dated handoff workspace retains `baseline-verification.json`, the read-only
capture script, dependency records, raw command output, browser artifacts and
failure-probe records under `.scratch/implementation-170-20261007/`.
These are local implementation evidence, including disposable test data and
test keys; they contain no production research data or production credentials.

Actual Node behavior execution on 2026-10-07:

| Existing file | Passed behaviors |
| --- | ---: |
| `active-target-status.test.mjs` | 5 |
| `experiment-log-fallback.test.mjs` | 5 |
| `experiment-log-model.test.mjs` | 8 |
| `target-research-facts.test.mjs` | 13 |
| Total | 31 |

The independent TypeScript checks and frontend build passed. The Linux public
asset test passed before and after its failure probe. The Windows browser UI
scenario passed at 1440, 800 and 390 pixels. The Linux product-backed
`quest-creation-states.spec.ts` scenario, `the ready Proposal keeps the accepted
violet source and six seed-field cards`, also passed using an isolated runtime,
real public endpoints and deterministic providers. Missing `typescript` before
`npm ci` and native Windows `fcntl` collection failure are recorded separately
as environment limitations, not behavior regressions.

The existing deterministic browser helper now opens `/?workspace=1`, the public
workspace entrypoint, rather than waiting for workspace controls on the home
page. A separate existing ManualCreation scenario reaches the dialog but fails
focus restoration at `manual-creation.spec.ts:111`; its assertion and product
behavior remain unchanged. The failure trace is preserved. The complete
browser suite is not claimed as passing.

The acceptance probes temporarily change an existing assertion to an impossible
expected value, require the behavior command to fail with an assertion error,
restore the original bytes in `finally`, verify SHA-256 restoration and rerun
the test. They add no mirrored implementation test.

Types, isolated persistence, deterministic providers and short browser runs
each establish their own limited behavior. They do not establish Agent
understanding, scientific benefit, production recovery, or long-term research
stability. This ticket does not modify Agent behavior and requires no real
research Agent sample; longer scientific acceptance remains with the user.
