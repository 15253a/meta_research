# Agent contracts

DeepFetch uses one main agent, the Quest-scoped Acquisition Root, and independent Readers. Keep each role inside its boundary.

## Main agent

Own topic interpretation, multidimensional discovery, OpenAlex and Web Search calls, paper selection, placeholders, the full-text subset, orchestration, and `summary.md`. Search and ledger breadth have no paper-count cap beyond relevance and the active-search clock. Maintain a ranked reserve and choose no more than 10 distinct papers whose combined marginal evidence value is highest for the task; acquisition order and access ease never determine admission. Re-rank before consuming the final Reader slots. Use Web Search results and bibliographic or abstract pages only to discover and verify papers; do not expand an article body into the main context. Hand selected bodies to Acquisition and Readers. Read all Reader results from the ledger after fan-in. Do not perform full-text scientific reading in the main context.

## Preflight and Acquisition

The Quest-scoped Acquisition Root owns provider selection, access mode, lawful routing, browser
state and private storage. DeepFetch submits one exact target through the common
`agent_runtime.acquisition.request` effect. The host reconciles the same `effect_id` before any
request replay. A Reader may receive only the same `paper_id` returned as `obtained` with a verified
path and content proof.

Return this `action=acquire` envelope for one target:

```json
{
  "effect_id": "fulltext-openalex-W123",
  "target": {
    "paper_id": "openalex:W123",
    "title": "Exact title",
    "source_urls": []
  }
}
```

Include DOI, arXiv ID and source URLs when Radar already has them. Acquisition treats them as hints,
applies the Quest's accepted access configuration, and returns one typed result. `obtained` includes
the verified path, format and content proof. `waiting_user` or `missing` includes only status and
failure. When a real human obligation blocks the current task, use the common HumanRequest effect
explicitly; a provider wait is only an Acquisition result.

Acquisition works from the main agent's ranked reserve queue. Never keep more unresolved
acquisition items than the unfilled read-target capacity. Before the active-search deadline,
`missing` or explicit abandonment before a first Reader assignment releases its reservation and
may be replaced; after the deadline, do not promote a reserve candidate. `waiting_user` retains its
reservation until resolved or abandoned. Only an obtained, identity-verified body proceeds to
Reader admission.

If login expires during a run, return the affected paper immediately. Continue independent OpenAlex, OA acquisition, and Reader work. Offer re-login and retry, user-supplied full text, or abandonment of that paper. User wait does not consume active-search time when no other search work is running.

## Readers

Create one logical Reader per admitted full text. Across the run, no more than the main agent's read target, and never more than 10 distinct `paper_id`s, may receive a Reader assignment. The first assignment consumes that paper's slot permanently for the run; retries use the same slot. For admitted work, start:

`readers_to_start = min(queued jobs, max(0, 10 - active Readers), runtime slots currently free for Readers)`

Fill available capacity immediately. Ten is a ceiling, not a required peak. Never assign multiple papers to one Reader. Use successive waves only for already admitted papers when host capacity is lower than the admitted job count, or for retries of those papers; no later wave may admit an eleventh paper. An idle Acquisition Root does not reserve an execution slot. A lower observed peak caused by host capacity is a runtime limitation, not a reason to serialize otherwise independent Reader jobs.

A Reader receives:

- the research task;
- its assigned paper record;
- one absolute full-text path and SHA-256;
- one absolute path to the `papers-json.md` reading contract;
- one single-use assignment ID and patch template.

It opens its job, reads the Reading section at `reading_contract_path` completely, and opens its assigned full text. It does not search, download, inspect other papers, repair metadata, or write the overall report. It reads enough of the whole paper to fill the complete `reading` object, including claims, experimental setup, reported artifacts, limitations, and credibility.

The Reader applies its patch through `scripts/papers.py apply-reader` and returns only `paper_id`, assignment ID, terminal status, and a concise error to the main agent.

Failure types:

- `reader_failed`, `timeout`, or `invalid_output`: retain the valid full text and publish an empty `failed` reading. Retry the same paper when worthwhile; otherwise that slot ends without a replacement paper.
- `file_invalid` or `paper_mismatch`: let the merge tool quarantine the file, clear the public path, and restore `not_read`. The admitted paper keeps its slot; correct and retry that paper when worthwhile, but do not replace it with an eleventh paper.

If a Reader disappears, the main agent may submit a `timeout` failure patch using that job's assignment data. This closes the job without inventing a reading.
