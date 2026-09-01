# Paper profile (`paper-v1`)

This request is a research paper, not a report with a different file extension.
The shared Writing rules above still govern evidence, citation markers, the
frozen Snapshot, lineage, and root advisory finalization. Interpret every
occurrence of “report” in the shared compatibility core as “candidate document”
for this request only. Keep the same root native Session; do not start a
paper-specific top-level Session.

## Canonical semantic source

Choose a structure appropriate to the Intent: empirical, methods, review,
theoretical, or another defensible research-paper genre. Do not force every
paper into one IMRaD example. Give each H2 section a stable semantic role while
allowing its visible title and language to fit the audience:

`<!-- meta-research-paper-section role=<role> -->`

Allowed roles are `abstract`, `framing`, `related-work`, `methods`, `model`,
`evidence`, `results`, `analysis`, `synthesis`, `evaluation`, `discussion`,
`limitations`, `implications`, `conclusion`, and `appendix`. Start with one
`abstract` and one `framing` section. Include at least one central argument role
(`methods`, `model`, `evidence`, `results`, `analysis`, `synthesis`, or
`evaluation`), at least one qualifying role (`discussion`, `limitations`, or
`implications`), and one `conclusion`; only appendices may follow the
conclusion. Use 5–24 unique, content-bearing H2 sections.

Every section must contain at least one machine-classified claim block. The
semantic roles are authoring invariants, not fixed English headings or Word
styling hints. The abstract states the bounded question, approach, principal
result, and limitation. Framing establishes the research gap and contribution.
Central sections make the evidence cut and argument reproducible. Qualifying
sections address counterevidence, scope, and threats to validity. The conclusion
answers only what the frozen evidence permits. Do not add unsupported authors,
affiliations, dates, journal metadata, or a fabricated bibliography. The
renderer will produce a real DOCX from this accepted semantic source; do not
emit OOXML, base64, HTML, or a download link.

## Type-specific review

In addition to the shared rubric, the root advisory finalization checks
section-role coverage, methods/results separation, abstract-to-conclusion
consistency, counterevidence, threats to validity, and whether the stated
contribution exceeds the accepted evidence. Structural conformance does not
constitute RG citation acceptance or publication readiness.
