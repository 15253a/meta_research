# Meta Research

Meta Research is a platform for sustained autonomous research and human collaboration.

Current optimization follows [spec #262](https://github.com/15253a/meta_research/issues/262), within [overall spec #169](https://github.com/15253a/meta_research/issues/169). See [AGENTS.md](AGENTS.md) for repository instructions and [CONTEXT.md](CONTEXT.md) for domain concepts.

Use Python 3.11–3.13 with the checked-in `uv.lock`. Backend verification requires Linux (`fcntl`). Frontend source and lockfile live in `web/`; packaged assets live in `src/meta_research/web_dist/`.

[Environment and backend checks](docs/implementation-baseline.md), [frontend checks](web/README.md), and [asset lifecycle contracts](docs/asset-lifecycle.md) provide the relevant development references.
