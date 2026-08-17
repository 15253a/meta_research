# Discovery radar

Use two complementary discovery channels:

- OpenAlex is the structured radar for reproducible metadata search, exact identity resolution, and citation neighborhoods.
- Web Search is the recall radar for vocabulary discovery, recent or weakly indexed work, reviews, benchmarks, named methods, and gaps visible only after inspecting the current ledger.

Neither channel is a quality verdict. The main agent owns every query, anchor, direction, depth, relevance, and stopping decision.

## OpenAlex

`scripts/openalex.py` is state-free. It returns normalized metadata and explicit citation directions.

Search with multiple formulations:

```bash
python3 "$DEEPFETCH_ROOT/scripts/openalex.py" search \
  --query "unsupervised domain adaptation EEG emotion recognition" \
  --query "cross-subject EEG affective computing transfer" \
  --limit 25 --output "$OUTPUT_DIR/.deepfetch/openalex-search.json"
```

Resolve one or more DOI or OpenAlex IDs:

```bash
python3 "$DEEPFETCH_ROOT/scripts/openalex.py" get \
  "doi:10.xxxx/example" "W1234567890" \
  --output "$OUTPUT_DIR/.deepfetch/openalex-works.json"
```

Inspect either or both citation directions around one or more seeds:

```bash
python3 "$DEEPFETCH_ROOT/scripts/openalex.py" citations \
  --seed "W1234567890" --seed "doi:10.xxxx/example" \
  --direction both --limit 50 \
  --output "$OUTPUT_DIR/.deepfetch/openalex-citations.json"
```

Use `--from-year`, `--to-year`, repeated `--work-type`, or `--sort FIELD:asc|desc` when they clarify a search dimension. Run a subcommand with `--help` for its exact flags.

Search/get envelopes use `deepfetch.openalex.v4`. Pass an exact `get` envelope or only the individually retained works from a search to `papers.py upsert`; do not register a whole noisy search page merely because its format is accepted. Enrich retained records with bounded pre-understanding before finalization. Citation-neighborhood output remains radar evidence from which the main agent selects records.

Set `OPENALEX_API_KEY` when available. The client redacts it from output and runtime errors.

## Web coverage audit

Use Web Search during discovery whenever OpenAlex results expose unfamiliar terminology or appear sparse. Before freezing the full-text subset, always make one bounded audit of the current ledger and the candidates already seen. First reconsider high-relevance candidates that were found but not registered; then search the most consequential missing axes rather than repeating the same broad query. Useful axes include:

- task settings and major method families;
- task and method synonyms;
- review, survey, taxonomy, tutorial, benchmark, or dataset language;
- classic or highly cited named methods found in references;
- exact method names and authors found in promising papers;
- recent years, early-access articles, preprints, and negative, contrary, or comparative evidence.

Stop the audit when the important gaps have representative placeholders and additional formulations mostly return duplicates or out-of-scope work. Candidate count alone is not a stopping reason. Let the selected intensity budget bound this work; do not impose a fixed query count.

## Admit a Web lead

A result page or snippet is discovery evidence, not paper evidence. Before adding a Web lead to the ledger:

1. verify an exact scholarly title and at least one identity-bearing source such as a DOI, arXiv record, OpenAlex work, PubMed record, proceedings page, repository record, or publisher article page;
2. resolve through OpenAlex when possible to normalize metadata, but retain a verified DOI-, arXiv-, or title-keyed paper when OpenAlex has no record;
3. merge by DOI first, then arXiv, OpenAlex ID, or explicit title-and-author identity evidence;
4. write only bounded title-, abstract-, citation-context-, or metadata-supported pre-understanding;
5. send any selected article body to Acquisition and a Reader instead of reading it in the main context.

Blogs, news pages, lab publication lists, and search snippets may point to a paper but cannot replace its scholarly record. Do not add them as papers or copy a snippet into `metadata.abstract`. Keep useful identity-bearing URLs in `metadata.source_urls`.
