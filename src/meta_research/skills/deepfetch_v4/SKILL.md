---
name: deepfetch-v4
description: Build a full-text-grounded scholarly evidence map only when a prompt explicitly requests DeepFetch, systematic multi-paper literature mapping, or literature-search-based novelty analysis. Use Nature Downloader for acquisition of an already-known paper and ordinary document analysis for one supplied paper.
---

# DeepFetch v4

Search wide. Let evidence earn certainty.

## Deliver

Publish exactly:

- `papers.json`
- `summary.md`
- `fulltext/`

Use low, medium, or high intensity only as an active-search clock:

| Intensity | Budget |
|---|---:|
| low | 8 minutes |
| medium | 13 minutes |
| high | 20 minutes |

Default to medium. Start the clock after Preflight. Exclude time spent waiting for user login or
uploads. At the deadline, stop discovery and reserve promotion. Freeze currently in-flight
target candidates, including acquisitions in flight and verified bodies awaiting Reader assignment,
plus Reader-admitted papers. Drain only that frozen work; do not promote a reserve candidate after
the deadline. Queue drain is outside the active-search clock.

Search breadth, ledger size, and reading depth are independent. Register as many verified,
relevant papers as useful within the search clock, but admit at most 10 distinct `paper_id`s to
full-text Readers across the entire run, not per batch. Treat those slots as a scarce evidence
budget: when at least 10 worthwhile candidates exist, choose the 10-paper set with the highest
expected marginal value for the task, never the first or easiest 10. Choose fewer rather than pad
the set with weak papers.

Run Radar, Ledger, and Fan-out as an overlapping pipeline. The numbered sections define ownership and completion gates, not a serial wait between discovery, acquisition, and reading.

## 1. Preflight

Read the Preflight section of [references/agents.md](references/agents.md). Treat an injected
`oa_only` session as the user's already explicit primary route: its preflight is satisfied without
probing institutional/browser access. Otherwise verify the configured institutional route through
the Acquisition session before starting the clock. Start only after access works or the user
explicitly selects open-access-only continuation. Never describe an already selected `oa_only`
route as forced, as a downgrade, or as a fallback caused by unavailable institutional access.

If the user cancels, end without starting a run.

Completion criterion: the institutional route is usable or the run is explicitly OA-only.

## 2. Radar

Read [references/openalex.md](references/openalex.md) and [references/papers-json.md](references/papers-json.md) completely, then initialize the ledger before issuing the first query. Interpret arbitrary input into search concepts, then conduct multidimensional discovery across text queries, literature roles, and citation structure. Use OpenAlex as the structured primary radar, not as the corpus boundary. Use Web Search as a second discovery channel for terminology, reviews, benchmarks, named methods, recent work, and other coverage gaps. Choose every query, anchor, direction, depth, and stopping judgment yourself.

Before freezing the full-text subset, run one bounded coverage audit against both the current ledger and the candidates already seen. Check whether the task settings, major method families, classic-to-recent development, reviews or benchmarks, and contrary or boundary evidence have representative placeholders. Reconsider high-relevance candidates that were discovered but never registered, then use Web Search on the consequential gaps. Candidate volume is not coverage. Treat every Web result as a lead: verify that it is a scholarly work and resolve its exact identity before registration. OpenAlex absence is not a reason to discard a verified paper. Search snippets and non-paper pages never count as full-text evidence.

Triage each broad result batch before accumulating another one. Register every important work whose scholarly identity is verified, even if only a title-level placeholder can be filled; merge duplicates and drop clearly irrelevant leads. Do not carry an unreviewed candidate dump into synthesis. Access convenience is not a relevance signal. A citation count, venue, author, institution, publisher, or publication date is context rather than proof.

Completion criterion: the three discovery dimensions have been touched; OpenAlex and the Web coverage audit have both been used, or Web Search unavailability is recorded as a limitation; and either the active-search budget expires or a defensible early stopping reason is recorded.

## 3. Ledger

Use the loaded ledger contract and [references/ledger-tools.md](references/ledger-tools.md). Create a placeholder whenever an important paper's exact title is known. Preserve the distinction between metadata-level pre-understanding and full-text reading. Rank the candidate set by expected marginal evidence value: direct task relevance; non-redundant coverage of methods, datasets, benchmarks, classic anchors, frontier work, and contrary or boundary evidence; ability to resolve consequential uncertainty; and available quality signals. Penalize redundancy. Treat venue, citations, and age as secondary context, and never treat access convenience as value. Re-rank unassigned candidates as discovery changes the pool, and do not consume all Reader slots before the bounded coverage audit. Select the highest-value set of at most 10; the ledger may be much larger.

Completion criterion: every paper retained for synthesis has one stable record, a bounded pre-understanding, and an explicit inclusion reason.

## 4. Fan-out

Read the Acquisition and Reader sections of [references/agents.md](references/agents.md). Choose an internal full-text read target of 0--10 papers and keep a ranked reserve queue. Early in Radar, admit only clearly high-value anchors and preserve capacity for candidates revealed by broader discovery. Before the active-search deadline, a candidate that fails before its first Reader assignment may be replaced by the highest-value remaining candidate while target capacity remains. Give only currently selected target candidates to Acquisition, register only identity-verified bodies, and create one Reader job per obtained full text. Run admitted jobs concurrently up to 10 Readers, bounded by the runtime's Reader-capable slots. The first Reader assignment permanently consumes that paper's run-level slot; retries of the same paper do not consume another slot, while a failure or mismatch does not authorize an eleventh paper. After the deadline, drain only the frozen target candidates and Reader-admitted work.

Readers own full-text understanding. The main agent owns discovery, selection, metadata, and synthesis. Let deterministic tools own concurrent writes.

Completion criterion: every selected full-text item has a terminal acquisition or explicit
abandonment; every registered full text has a complete or failed reading; no admitted job remains
pending.

## 5. Synthesis

Read [references/summary.md](references/summary.md). Reload `papers.json` after Reader fan-in, then write the report in the input language under that contract.

Run final validation and cleanup through `scripts/papers.py finalize`. Keep private state only when debugging or when the run must resume.

Completion criterion: all applicable report sections and all three evidence identities have been checked; public artifacts validate; every cited `paper_id` exists; consequential missing evidence is reported; and the output root contains no stale public full text.
