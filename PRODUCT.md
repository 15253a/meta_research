# Meta-research vNext

Meta-research vNext is a local-first research runtime. This initial production
slice installs an isolated data root, starts a detached per-user daemon, and
serves an authenticated loopback Web shell backed by SQLite and a managed
object store.

The supported public host operations are exposed by the `meta-research`
command. Research state is observed through the authenticated Snapshot and SSE
interfaces; the Web client does not read persistence directly.
