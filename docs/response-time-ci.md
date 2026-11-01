# Response-time CI

The [response-time workflow](../.github/workflows/response-time.yml) checks the
built workspace and public read APIs against an isolated deterministic product
on Ubuntu 24.04. It runs for pull requests targeting `v1-test`, pushes to
`v1-test`, and manual dispatch. A newer run cancels an older run for the same
pull request or branch.

The product uses a fresh temporary data root, an ephemeral loopback port, and
deterministic providers. Process startup, database preparation, authentication,
and frontend compilation finish before the measured operations. The check
needs no repository secrets or production access.

Setup completes Idea through the production worker and holds the deterministic
Plan provider at `plan-primary`; model inference and Bundle execution are
outside the measurements.

## Run locally

Use Linux, Python 3.12, Node.js 22.23.1, and uv 0.11.3. The backend requires
POSIX `fcntl`. From the repository root:

```sh
uv sync --frozen
cd web
npm ci
npx playwright install --with-deps chromium
META_RESEARCH_TEST_PYTHON="$(pwd)/../.venv/bin/python" npm run test:response-time
```

The npm command builds the frontend and runs
`playwright.response-time.config.ts`. This dedicated configuration uses
Playwright's installed Chromium and its own result directory. The ordinary
functional and raster suites retain their existing configuration.

## Measurements and budgets

[budgets.json](../web/tests/response-time/budgets.json) is the source of truth
for sample counts and limits. The initial settings use two warmup operations
and twenty measured samples per metric, with workspace scenarios at 1440- and
390-pixel viewport widths.

| Metric | Completion condition | Initial p95 budget |
| --- | --- | ---: |
| Workspace | Current Quest/Cycle and its conversation are visible | 5,000 ms |
| Stage switch | The selected stage's session and content are visible | 1,000 ms |
| Status API | A successful `/api/v1/status` response body is read | 1,000 ms |
| Snapshot API | A successful `/api/v1/snapshot` response body is read | 3,000 ms |

The p95 uses nearest rank: sort the twenty samples and select the nineteenth.
Each metric must also keep its maximum at or below twice its p95 budget. The
report preserves all raw samples, median, p95, maximum, configured limits, and
runtime versions. Warmup samples do not count toward the budget calculation.

Each workspace navigation uses a fresh authenticated browser context, with the
backend process and static files already warmed. Stage switching alternates
Idea and Plan in an open workspace. UI timers end after the matching session's
output has loaded and two animation frames have elapsed. The status and
snapshot API measurements run once, independently of the two viewport runs,
to help distinguish HTTP latency from interface latency.

Snapshot samples measure warm HTTP delivery. The product retains snapshots
while the durable revision is unchanged, so this budget does not measure a
complete projection rebuild. Workspace and stage assertions also check the
expected identity; a fast response containing the wrong research state does
not pass.

These initial budgets cover a small deterministic dataset on the CI runner.
They are engineering regression limits, not a production 8768 service-level
objective or evidence about model response times and long-running research.
Review raw samples when changing a limit, together with any change to the
dataset, browser, runner, or measured completion condition.

## Results and failures

The workflow always attempts to upload `web/response-time-results/` as the
`response-time-results` artifact, retained for fourteen days. It contains the
timing report and Playwright evidence, including failure traces. Missing
artifacts produce a warning when dependency or browser setup fails before the
test can produce output; the failed setup step still fails the job.

For a slow or failed run, first inspect its raw samples and failure trace.
Compare the measured operation and expected Quest/Cycle/session identity, then
use the existing query diagnostics to locate expensive backend work. Query
diagnostics describe query computation and can belong to a retained snapshot;
the external response timer remains the source for this CI's API latency.

## Workflow references

The workflow follows the official [Playwright CI instructions](https://playwright.dev/docs/ci)
for browser/system dependency installation and artifact retention. Runtime
setup uses the official [checkout v6](https://github.com/actions/checkout/tree/v6),
[setup-node v6](https://github.com/actions/setup-node/tree/v6), and
[setup-uv v10.1.0](https://github.com/astral-sh/setup-uv/tree/v10.1.0)
instructions; the uv action is pinned to its verified commit.
