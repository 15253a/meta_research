# Local verification and environment setup

Use the current [optimization spec #262](https://github.com/15253a/meta_research/issues/262) and the relevant ticket to select verification and acceptance scope. On Windows, use `git config core.autocrlf false` in the checkout to preserve source bytes when exporting or comparing files.

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
