# `papers.json`

Read this file before creating or changing the public ledger. It is the single source of truth for `deepfetch.papers.v4`.

## Contents

- [Top level](#top-level)
- [Paper](#paper)
- [Identity](#identity)
- [Metadata](#metadata)
- [Pre-understanding](#pre-understanding)
- [Full text](#full-text)
- [Reading](#reading)
- [Ownership and final invariants](#ownership-and-final-invariants)

## Top level

Keep exactly these keys:

```json
{
  "schema_version": "deepfetch.papers.v4",
  "topic": {
    "input": "<original prompt>",
    "interpretation": "<concise search interpretation>",
    "search_concepts": [],
    "scope_notes": []
  },
  "run": {
    "intensity": "medium",
    "active_search_budget_minutes": 13,
    "active_search_elapsed_seconds": 0,
    "dimensions_used": [],
    "stopping_reason": null
  },
  "paper_order": [],
  "papers": {},
  "missing_fulltexts": [],
  "limitations": []
}
```

- `paper_order` contains every key in `papers` exactly once.
- `missing_fulltexts` contains every `paper_id` whose `fulltext_path` is `null`, in `paper_order` order.
- `dimensions_used` records semantic discovery dimensions, independent of search provider. Final runs include `text_queries`, `literature_roles`, and `citation_graph`; OpenAlex and Web Search may contribute to any of them, while citation direction and depth remain agent decisions.
- `limitations` contains concise run-level evidence or coverage limits.
- Unknown scalar values are `null`. Unknown, empty, or not-applicable collections are `[]`.

## Paper

Each `papers[paper_id]` has exactly `identity`, `metadata`, `pre_understanding`, `fulltext_path`, and `reading`. The sections below define those five values; the Reading section contains the one exact initial `reading` shape.

### Identity

Choose a stable `paper_id` at creation from the strongest verified identifier available: DOI, then arXiv, then OpenAlex, then a deterministic title fingerprint. Keep that ID forever when stronger identifiers arrive later. Merge records only with explicit identity evidence.

Normalize DOI values without a URL prefix. Store the OpenAlex work token as `W...`. Preserve an exact verified title. OpenAlex membership is optional: a Web-discovered scholarly work with a verified DOI, arXiv ID, or exact title may occupy a placeholder even when `openalex_id` is `null`.

### Metadata

Store bibliographic facts only. `authors` and `institutions` are arrays of names. `citation_count_observed_at` is an RFC 3339 timestamp paired with `cited_by_count`; an undated count is unknown. `source_urls` are metadata locations, not proof that full text was obtained.

### Pre-understanding

The main agent writes:

- `summary`: a cautious account supported by the named basis;
- `evidence_level`: `title_only`, `citation_context`, or `abstract_supported`;
- `basis`: `{ "type": "title|citation_context|abstract|metadata", "source": "...", "locator": null }` items;
- `why_included`: why this paper matters to the prompt;
- `uncertainty`: what remains unconfirmed.

Evidence only upgrades. A weaker update cannot replace a stronger summary or uncertainty. Final validation requires summary, basis, and inclusion reason, plus a matching basis for the declared level. `abstract_supported` also requires a stored abstract.

### Full text

`fulltext_path` is either `null` or a relative path under `fulltext/` ending in `.pdf`, `.html`, or `.xml`. Private run state holds hashes and acquisition diagnostics. A path proves only that a verified local full text was registered.

At most 10 paper records may have a non-null `fulltext_path`. This does not limit placeholder or
metadata-only records.

### Reading

Before reading, use this exact shape:

```json
{
  "status": "not_read",
  "understanding_summary": null,
  "methods": [],
  "experimental_setup": {
    "datasets_samples": [],
    "protocols": [],
    "baselines_controls": [],
    "metrics": [],
    "hardware_software": []
  },
  "key_claims": [],
  "limitations": [],
  "artifacts": {
    "code": {"reported": null, "items": []},
    "data": {"reported": null, "items": []},
    "model": {"reported": null, "items": []},
    "project": {"reported": null, "items": []},
    "supplement": {"reported": null, "items": []}
  },
  "credibility": {
    "score": null,
    "assessment_confidence": null,
    "rationale": null,
    "strengths": [],
    "concerns": []
  },
  "evidence_locators": [],
  "notes": []
}
```

A successful Reader sets `status` to `complete` and fills what the full text supports. `understanding_summary` is required. Empty arrays remain honest when an item is absent or not reported.

`experimental_setup` records data or samples, protocols or splits, baselines or controls, metrics, and reported hardware or software.

Each key claim is:

```json
{
  "claim": "The paper's claim",
  "evidence_locators": ["loc-1"],
  "internal_support": "supported",
  "support_rationale": "How the paper's own evidence bears on its claim"
}
```

`internal_support` is `supported`, `partially_supported`, `unsupported`, or `unclear`. It is not an external truth verdict.

Each limitation is `{ "description": "...", "source": "authors|reader", "evidence_locators": [] }`.

Each artifact category uses `{ "reported": true|false|null, "items": [] }`. An item is `{ "name": null, "url": null, "evidence_locators": [] }`. Reader records what the paper reports; it does not claim that a link is currently accessible.

Each evidence locator is `{ "id": "loc-1", "page": null, "section": null, "element": null, "description": "..." }`. Claims reference locator IDs in the same reading. Use section or element when pages do not exist.

`credibility.score` is one integer from 1 to 5, or `null` when no defensible assessment is possible:

1. central inference has severe methodological mismatch or little support;
2. major design threats substantially weaken it;
3. evidence is usable with important uncertainty;
4. evidence strongly supports the main conclusions;
5. evidence is unusually robust, transparent, and internally convergent.

Internal evidence dominates the score. Venue, publisher, authors, institutions, citations, and age are weak context and cannot conceal methodological flaws. `assessment_confidence` is `low`, `medium`, `high`, or `null`; it describes confidence in the assessment.

A terminal Reader failure uses the exact empty reading shape with `status="failed"` and one concise failure note. It contains no inferred understanding, claims, artifacts, or score. A later successful retry replaces it.

## Ownership and final invariants

- Main agent owns `topic`, `run`, ordering, identity, metadata, pre-understanding, full-text selection, and run limitations.
- Acquisition returns files and compact status; deterministic registration writes `fulltext_path`.
- A Reader patch owns only its assigned paper's `reading`. The merge tool alone may quarantine a mismatched file and clear its path.
- Across one run, at most 10 distinct `paper_id`s receive a Reader assignment. Retries of the same paper do not increase this count; every terminal outcome after first assignment still counts.
- A paper with no full text finishes `not_read`. A paper with a path finishes `complete` or `failed`.
- Papers without full text may inform the landscape through their bounded pre-understanding; they do not establish experimental details, artifacts, claim support, or credibility.
