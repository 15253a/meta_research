# Frontend verification

Run these commands from `web/` with Node.js 22.18 or newer (the existing Node
tests import TypeScript source directly) and the checked-in npm lockfile:

```sh
npm ci
npm test
npm run typecheck
npm run build
```

`npm test` runs the four existing `tests/*.test.mjs` files with Node's test
runner. `npm run typecheck` checks application and Playwright TypeScript without
executing behavior. `npm run build` typechecks and rebuilds
`src/meta_research/web_dist/`; it does not run browser behavior tests.

To select a Node behavior, place runner options before the files:

```sh
node --test --test-name-pattern="a current observed Target" tests/*.test.mjs
```

Do not append Node selection options to `npm test`; npm would place them after
the file arguments, where Node does not apply this selection.

The existing Playwright projects keep functional behavior separate from the
two fixed raster suites. The default browser is installed Google Chrome,
resolved by Playwright's `chrome` channel on Windows and Linux. Set
`META_RESEARCH_CHROME` to a browser executable path to override it. No browser
download is needed when Chrome is already installed.

For a small browser UI scenario on Windows or Linux, run:

```sh
npm run test:e2e -- --project=functional current-cycle.spec.ts --grep="the foreground Cycle keeps one exact Question"
```

This existing test serves the built frontend and fixture responses through
Playwright routes. It checks the current Question and four Stage states at
1440, 800 and 390 pixels and switching historical results without changing the
foreground scope. It exercises the browser UI; it does not execute the backend.

For a functional scenario against an isolated product on Linux, install the
repository's Python dependencies and run:

```sh
npm run test:e2e -- --project=functional quest-creation-states.spec.ts --grep="the ready Proposal keeps the accepted violet source and six seed-field cards"
```

This existing scenario starts a deterministic local product with a temporary
data directory and an ephemeral loopback port, authenticates the browser,
opens the research workspace at `/?workspace=1`, opens Quest creation, performs
the device probe, fills the draft and verifies the generated Proposal's source
and six seed-field cards, then stops the product and removes its temporary data.
The deterministic providers do not
invoke a real research Agent or prove scientific outcomes. It does not use the
8768 service or its research data.

The deterministic product helper defaults to `uv run python`. To use an
already prepared environment without `uv`, set `META_RESEARCH_TEST_PYTHON` to
its Python executable before running the command:

```sh
# Linux, from web/
META_RESEARCH_TEST_PYTHON="$(pwd)/../.venv/bin/python" npm run test:e2e -- --project=functional quest-creation-states.spec.ts --grep="the ready Proposal keeps the accepted violet source and six seed-field cards"
```

This interpreter override applies to scenarios using `DeterministicProduct`.
If a verification build uses an alternate output directory, point
`META_RESEARCH_TEST_WEB_ROOT` at that built directory. Its basename must be
`web_dist`, as required by the existing deterministic product helper.
Older specs that invoke the CLI directly still require `uv`. Select tests based
on the behavior being changed; running every browser or raster test is not a
prerequisite for every implementation ticket. Raster references are Linux
captures and need their established rendering environment for comparison.

The product backend currently imports POSIX `fcntl` and cannot start in native
Windows Python. Use Linux for the product-backed scenario; the fixture-backed
browser UI scenario above works in native Windows. Changing the backend's
platform support is outside this verification setup.

At the #170 verification baseline, an additional Linux ManualCreation scenario
fails its existing focus-restoration assertion after closing
the dialog (`manual-creation.spec.ts:111`). The recommended Proposal scenario
and fixture-backed current-Cycle scenario pass. Keep the ManualCreation failure
visible when choosing subsequent validation; the preparation work does not
change the focus assertion or product
behavior.

For workspace and public API response-time budgets, the dedicated Linux CI,
and `npm run test:response-time`, see [response-time CI](../docs/response-time-ci.md).
