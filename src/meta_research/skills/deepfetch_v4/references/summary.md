# `summary.md`

The main agent writes `summary.md` after Reader fan-in. Use the input language. Synthesize the map rather than serially restating every paper.

## Order

1. scope;
2. research landscape;
3. technical development from classic work to the current frontier;
4. evidence for candidate questions, only for novelty, idea, or question-generation tasks;
5. conflicts, failures, boundaries, and gaps;
6. missing full texts and uncertainty;
7. coverage and stopping reason;
8. research recommendations, or Question-generation recommendations when requested.

Include the candidate-question branch only when it is relevant.

## Evidence identities

Keep three identities visible through concise labels or unambiguous prose:

- **Paper claim**: what one paper reports.
- **Cross-paper synthesis**: what comparison across papers supports.
- **Tentative inference**: the main agent's interpretation or recommendation.

Place valid `paper_id` values near material literature claims using `[paper_id]` or backticks, for example `[doi:10.1000/example]` or `openalex:W123`. Every ID written in the report must exist in the same `papers.json`. Do not replace local evidence binding with a detached list of IDs. Headings and pure run descriptions need no artificial citation.

A paper without full text may appear through its explicitly bounded pre-understanding. Phrase title-, abstract-, or citation-context evidence as such. Reserve experimental detail, internal claim support, artifact reporting, and credibility for completed Readers.

## Completion

Report discovery breadth and reading depth separately: ledger paper count, acquisition outcomes, distinct Reader-admitted papers (at most 10), completed readings, Reader failures, and mismatches or quarantines. Briefly explain why the selected full-text set had the highest expected marginal value for the task and identify consequential evidence left outside it by the cap. Report the search intensity, dimensions and discovery channels actually used, stopping reason, consequential missing full texts, and limitations. State whether the Web coverage audit added new papers or mostly duplicates, and state partial coverage plainly. Never describe a placeholder, an obtained-but-unread file, or a failed reading as full-text evidence. Put literature review before recommendations and keep code blocks out of the report body.
