# Presentation profile (`presentation-v1`)

This request is a presentation, not a report renamed to `.pptx`. The shared
Writing rules above still govern evidence, citation markers, the frozen
Snapshot, lineage, and fresh-context review. Interpret every occurrence of
“report” in the shared compatibility core as “candidate document” for this
request only. Keep the same root native Session; do not start a deck-specific
top-level Session.

## Canonical semantic source

The H1 is the deck title. Define between 2 and 40 content-bearing slides. Each
slide starts with the shared structure marker followed by one sequential H2:

`## Slide <1-based number>: <unique slide title>`

Every slide must then contain at least one shared claim-classification block.
Give each slide one primary rhetorical job and a clear takeaway. Use multiple
slides, speaker-ready wording, and deliberate narrative progression; do not
compress a paper outline into a single wall of bullets. Preserve qualifiers,
counterevidence, legends, units, and source meaning when simplifying content.
If the snapshot cannot support a needed visual or claim, use an evidence-gap or
uncertainty block rather than a decorative substitute.

The renderer will turn these stable slide boundaries into editable PowerPoint
text shapes. Do not emit OOXML, base64, image-only slides, arbitrary template
paths, or a download link. Rendering and external delivery happen after formal
acceptance and are not actions this Skill may take.

## Type-specific review

In addition to the shared rubric, the fresh-context reviewer checks narrative
spine, one-job-per-slide discipline, claim-to-slide placement, density risk,
qualifier retention, title/takeaway agreement, and whether the deck's requested
decision is justified for the exact audience and purpose. Structural
conformance does not constitute visual QA, RG citation acceptance, or external
delivery success.
