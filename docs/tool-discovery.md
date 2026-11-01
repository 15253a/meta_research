# Current tool discovery and contracts

`tools.catalog.page` and `tools.contracts.read` are read operations in the existing
Semantic MCP gateway. The gateway projects the same `SemanticOperation` records
and current channel grant used by `tools/list` and `tools/call`. Discovery is
optional: a caller with a known operation and accurate reader arguments can call
it immediately. A catalog hash identifies a page sequence, never a permission.

## Caller behavior

Call `tools.catalog.page` with optional `query`, `owning_module`, `offset` and
`limit`. The default page size is 20, with a maximum of 100. Continue with the
returned `next_offset`, `catalog_hash` and unchanged filters. A null
`next_offset` ends the filtered catalog. `tool_catalog_changed` requires a fresh
first page, including when the caller's current execution scope changes.

Call `tools.contracts.read` with an exact `operation_id`. The response includes
the registered input/output schemas, ownership, access mode, reconciliation
operation, and return/error explanations. The response describes that operation;
it neither issues a grant nor freezes availability. Unregistered, ungranted and
unavailable operations cannot be loaded as callable contracts. Runtime Owner
checks still validate exact resource scope, versions, parameters and effects.

The Harness checks the current root/attempt/fence before dispatch. A root waiting
for a HumanRequest response only sees the operations permitted during that wait;
resuming the same root restores its current execution scope. Revoked channels
cannot read catalogs, load contracts or call previously discovered readers.

## Adding or changing an operation

Keep the handler and input/output schemas in its existing `SemanticOperation`
producer. Add it to the appropriate existing root grant composition; registering
an operation does not grant it to every root. No separate discovery inventory is
maintained. Removed registrations disappear from discovery automatically.

Use a concise `description` for the tool's purpose. Put detailed pagination,
units, acceptance or return semantics in `return_description`, and known error
explanations in `error_descriptions`. These fields belong to the same operation
record and its contract binding. The common gateway error envelope applies even
when an Owner has additional codes; the documented codes are not an exhaustive
allowlist. Preserve Owner error codes and details.

If registration can outlive a missing dependency or a disabled mode, supply the
operation's `unavailable_reason(context)` predicate. It must be read-only and
report the same current restriction for listing, contract loading and calling.
Keep argument-specific validation in the handler. For example, an Acquisition
request with no provider can still reconcile or replay a recorded effect; a
blanket availability prohibition would incorrectly hide that valid path.

Related implementation slices keep their own producers: #266 extends asset
intake and readers, #267 extends historical queries and continuation semantics,
#268 changes ordinary workspace access, #270 extends Literature operations, and
#271/#272 define their own assistant/recorder grants. Their current schemas and
metadata flow into discovery on integration; proposed names are never registered
as placeholders. Preserve each slice's changes to shared guidance when merging.

`tests/test_tool_discovery_registry.py` exercises registration/schema changes;
`tests/test_tool_discovery.py` checks catalog paging, contracts and direct readers;
`tests/test_tool_discovery_integration.py` uses admitted roots and public MCP HTTP
to check restricted grants, revocation, HumanRequest waiting and resumption.
