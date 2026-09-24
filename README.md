# Meta-research

This branch preserves the source installed in the backend listening on port 8768 on 2026-09-24.

- `baseline/v0.0.0`: frozen source baseline. All 286 packaged files were byte-for-byte equal to the deployed package when captured.
- `v1-test`: continuation branch for the memory, storage, intake and indexing design review.
- [Baseline manifest](docs/baselines/v0.0.0.json): source provenance and package SHA-256 values.
- [Domain language](CONTEXT.md): the existing research concepts.

`v0.0.0` names this preservation point; it is not a claim of production release acceptance. The package's existing `0.1.0` version is unchanged. The parent is the last published `test-all` history; the 8768 source snapshot replaces its older deployment tree.

The repository contains source, tests, pinned Python and frontend dependencies, and the packaged frontend served by the backend. It does not contain research databases, research files, credentials, virtual environments or caches. Restoring code alone does not restore research data.

Use Python 3.11–3.13 and the checked-in `uv.lock` for the Python environment. The frontend source and npm lockfile live in `web/`; the deployed static assets live in `src/meta_research/web_dist/`. The source-preservation and design pass does not restart or migrate the running service.

Current design work: [memory-system map](https://github.com/15253a/meta_research/issues/148) and [evidence documents](docs/memory/README.md).
