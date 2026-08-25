---
name: writing-report
description: Draft and revise an evidence-grounded Markdown research report from one frozen Meta-research Quest Snapshot. Use when a Writing Run requests a report draft, child-agent citation review, or a feedback-driven successor revision; never use it to publish, accept, or advance a Quest Stage.
---

# Writing Report

Produce one auditable report candidate inside the managed Writing Session. Treat
the supplied Intent, Snapshot, runtime binding, lineage, and feedback as exact
inputs. Research may continue accepting newer facts while this Writing Run is
active; those facts belong to a later Snapshot and neither mutate nor gate this
run. Starting or finishing this run likewise does not advance, pause, or gate
research, Bundle completion, or any Quest Stage. Do not query or silently
substitute newer Quest state. The host captured this candidate cut for the
Intent and sealed that exact cut at HC authorization. A lost acknowledgement or
restart resumes the sealed cut without recapture, revalidation, or replacement.

## Draft

- Write valid Markdown for the stated title, audience, purpose, and instructions.
- Read the staged files listed in `accepted_source_manifest` before making
  evidence claims. Bind each file back to its manifest `version_ref`; the file
  path is transport only and is never a citation identity.
- Use only the exact Frozen Snapshot source root exposed by the permission
  profile. Web search, MCP, other workspace roots, and live Owner files are not
  Writing inputs.
- Make claim coverage machine-checkable. The first block must be the single H1
  document title. Every later block must begin with exactly one marker. A
  section heading uses `<!-- meta-research-structure -->` followed immediately
  by one H2-H6 heading. Use
  `<!-- meta-research-claim:supported refs=<citation_ref,...> -->` for a
  supported claim and include each matching `[[citation:<citation_ref>]]`
  anchor in that same block. Otherwise use one of
  `<!-- meta-research-claim:inference -->`,
  `<!-- meta-research-claim:uncertainty -->`, or
  `<!-- meta-research-claim:evidence-gap -->`; its visible text must start with
  `**Inference:**`, `**Uncertainty:**`, or `**Evidence gap:**` respectively.
- Cite only `version_ref` values present in `accepted_sources`.
- For every supported block, return one citation per anchor with the exact
  source `version_ref`, locator, the full supported block text (without the
  anchor) as `claim`, and the identical exact source excerpt as `source_quote`.
  RG accepts formal support only when the normalized claim equals that excerpt
  and the excerpt is present at the locator. Put translations, paraphrases, and
  multi-source synthesis in visibly labeled inference or uncertainty blocks;
  they must not masquerade as independently verified source wording. Never
  invent bibliographic facts, quotes, or locators.
- Locators have one of four exact forms: `line:<1-based line>` for a single
  UTF-8 file, `page:<1-based page>` for a single PDF,
  `path:<percent-encoded portable path>#line:<1-based line>` for a text entry
  in a directory/repository, or `path:<percent-encoded portable
  path>#page:<1-based page>` for a PDF entry. Use the staged source manifest to
  choose the entry path; do not use filesystem transport paths as locators.
- Do not create receipts, accept content or citations, render a formal artifact,
  advance a Stage, or publish/send/submit anything externally.

## Self-review and revise

- Ask the Harness to spawn one fresh-context child reviewer inside the same root
  Session. The child is advisory and must inspect evidence coverage, citation
  bindings, unsupported certainty, internal consistency, and Intent alignment.
- Return each finding and a `revised` or `not_adopted` disposition. A `revised`
  disposition must materially change the final Markdown or citation set.
- Keep the root native Session identity unchanged while applying the review.
- On RG or human feedback, revise in that same Session and preserve predecessor
  lineage. Do not overwrite any previously accepted content version.

## Stop conditions

- Fail closed if the Snapshot hash, runtime binding, Session, Fence, predecessor,
  or source binding differs from the request.
- If evidence is insufficient, use an explicit visible evidence-gap block and
  return no fabricated citation. Permission or integrity problems are failures,
  not completion.
- Pause/cancel instructions from the host take precedence; do not start a second
  top-level Session to recover.
