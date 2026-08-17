# Ledger tools

In examples, `DEEPFETCH_ROOT` is the absolute `deepfetch-v4/` directory and `OUTPUT_DIR` is the absolute run directory.

Initialize from arbitrary input:

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" init \
  --out-dir "$OUTPUT_DIR" --topic-file "/absolute/prompt.txt" \
  --interpretation "Concise interpretation" \
  --concept "concept one" --concept "concept two" \
  --intensity medium
```

`upsert` accepts one discovery-owned paper object, an array, `{ "papers": [...], "limitations": [...] }`, or a complete `deepfetch.openalex.v4` search/get envelope. OpenAlex-only radar fields are discarded and metadata flows directly into the ledger. Enrich every retained record with `summary`, `evidence_level`, `basis`, `why_included`, and `uncertainty` under [the ledger contract](papers-json.md). `update-run` records monotonic active-search seconds, actual dimensions, and the stopping reason.

Register one verified Acquisition result:

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" register-fulltext \
  --out-dir "$OUTPUT_DIR" --paper-id "openalex:W123" \
  --file "/absolute/provider/result.pdf"
```

`register-fulltext` accepts at most 10 distinct paper records in one run. Keep additional search
results as metadata placeholders. Replacing the file for an already registered or Reader-admitted
paper does not consume another slot; after 10 distinct papers have received Reader assignments, a
different paper cannot replace one of them.

Create Reader jobs and merge their deterministic patches:

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" prepare-readers \
  --out-dir "$OUTPUT_DIR" --task "Current research question"

python3 "$DEEPFETCH_ROOT/scripts/papers.py" apply-reader \
  --out-dir "$OUTPUT_DIR" --result "/absolute/reader-patch.json"
```

Each generated job carries the exact `patch_template` and an absolute `reading_contract_path`. Reader agents edit only their own patch and apply it through the merge command.

After the main agent writes `summary.md`, finalize:

```bash
python3 "$DEEPFETCH_ROOT/scripts/papers.py" finalize --out-dir "$OUTPUT_DIR"
```

Use `validate` for sparse work in progress and `validate --final` to inspect the final gate without cleanup. Add `--keep-debug-state` to `finalize` only for debugging or resume. Subcommand `--help` defines exact CLI flags.
