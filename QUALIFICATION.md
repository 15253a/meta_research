# T1/T2 qualification runbook

This is a thin qualification boundary around the existing orchestrator. It adds no
second database, scheduler, daemon, or research state machine. It provides:

- trusted SEED/DREAMER view preparation;
- an immutable contract and claim lock;
- no-host-tools research workers and exact read-only Docker mounts;
- one irreversible final predictor batch; and
- a root-operated scorer that ignores candidate-reported metrics.

It does **not** by itself prove novelty, statistical superiority, or provenance of
bytes manually supplied by the operator. The operator who selects the final source
tree is trusted. Automated repo imports, uploads, asset references, and host tools are
mechanically disabled in qualification mode.

## 1. Prepare the data views

Use a dedicated non-root research account and root as the sealed-data/scoring
operator. The shared parent must be `0755`: the research process opens paths one
component at a time. The sealed directory becomes root-owned `0711`; its files remain
root-owned `0400`, so the research UID can verify metadata but cannot read labels.

```bash
install -d -o root -g root -m 0755 /absolute/qualification-data
install -o root -g root -m 0600 /dev/null /absolute/qualification-data/hmac-secret
head -c 32 /dev/urandom > /absolute/qualification-data/hmac-secret
```

For T2:

```bash
python -m orchestrator.qualification_data prepare-seed \
  --archive /absolute/SEED.zip \
  --public-root /absolute/qualification-data/seed-public \
  --sealed-root /absolute/qualification-data/seed-sealed \
  --secret-file /absolute/qualification-data/hmac-secret \
  --research-uid "$RESEARCH_UID" --evaluator-uid 0 \
  > /absolute/qualification-data/seed-receipt.json
```

For T1, first write a bounded JSON rule such as:

```json
{"comparison":"higher_is_positive","neutral_policy":"drop","score":"valence","threshold":3}
```

Then run:

```bash
python -m orchestrator.qualification_data prepare-dreamer \
  --archive /absolute/dreamer.zip \
  --public-root /absolute/qualification-data/dreamer-public \
  --sealed-root /absolute/qualification-data/dreamer-sealed \
  --secret-file /absolute/qualification-data/hmac-secret \
  --label-rule /absolute/qualification-data/dreamer-rule.json \
  --research-uid "$RESEARCH_UID" --evaluator-uid 0 \
  > /absolute/qualification-data/dreamer-receipt.json
```

Destroy the HMAC secret after all views and receipts are durably backed up. Retaining
it where the research UID can read it defeats the opaque order/ID firewall.

## 2. Install the immutable contract

The canonical contract protocol is
`meta-research-qualification-firewall/v1`. Its `mounts` must name only prepared,
read-only roots and bind each `qualification-view.json` hash. `sealed_truth` binds the
root-owned `truth.json`. T2 requires folds `1..15` and exactly three frozen seeds. T1
requires at least three distinct exploration datasets plus one DREAMER holdout.

Set both of these policy arrays to exactly the contract mount paths, with no extra
path:

```yaml
execution:
  path_allowlist: [ ...exact contract mount paths... ]
  sandbox:
    readonly_mounts: [ ...the same exact paths... ]
```

Contract and claim input files must be canonical UTF-8 JSON: sorted keys, compact
separators, finite numbers, and one trailing newline. Install them as the research
UID before the first database/cycle is created:

```bash
python -m orchestrator.qualification_firewall --work-root "$WORK" \
  install-contract --contract /absolute/contract.json
python -m orchestrator.qualification_firewall --work-root "$WORK" \
  lock-claim --claim /absolute/claim.json
python -m orchestrator.qualification_firewall --work-root "$WORK" verify
```

The claim protocol is `meta-research-qualification-claim-lock/v1`. T1's public
DREAMER label rule must exactly match the claim. T2 freezes subjects `1..15`, the
three seeds, all 15 folds, and source-inner-LOSO or target-X-only HPO.

## 3. Research and final execution

During research, DREAMER is never mounted. The 15 final SEED folds are also never
mounted: exposing different folds across persistent research turns would reveal each
target's labels when that subject appears as a source elsewhere. Therefore T2
per-fold training and source-inner-LOSO HPO must execute inside the frozen final
predictor, independently for each unit.

The final source tree must be owned by the research UID, contain no `.git`, symlinks,
group/world-writable files, or multi-linked files, and stay byte-identical for the
whole batch. Its command writes only canonical `predictions.json`:

```json
{"fold":null,"probabilities":[[0.9,0.1]],"sample_ids":["64-lowercase-hex"],"seed":null,"unit_id":"dreamer","version":1}
```

T2 uses the frozen integer `seed`, fold `1..15`, three probabilities per row, and
unit IDs such as `seed-17-fold-08`. Sample IDs must be the exact public set.

Run once as the research UID:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" run-final \
  --system-root /absolute/meta-research --source-root /absolute/frozen-source
```

For a GPU contract, also pass `--gpu-contract`; an exact allocation canary must bind
the frozen claim/source/runtime, container inventory log, fenced guardian receipt,
and sandbox spec before the irreversible marker is written. A fully authenticated
but stale pre-final canary is replaced before any scientific unit is spent. Finally,
run the independent scorer as root:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" score-final
```

The authoritative result is `state/qualification/final-result.json`. Once final is
consumed, the normal research system permanently refuses to restart that work root;
target metrics cannot feed another research turn. Score replay rereads truth and
predictions and recomputes metrics before accepting the existing result. A
failed/spent unit is never rerun.
