# T1/T2 qualification runbook

This is a thin qualification boundary around the existing orchestrator. It adds
no second database, scheduler, daemon, or research state machine. It provides:

- trusted SEED/DREAMER public views and evaluator-owned truth;
- an immutable pre-start contract and a later, one-time claim lock;
- no-host-tools research workers and exact read-only Docker mounts;
- one irreversible final predictor batch; and
- a root-operated scorer that ignores candidate-reported metrics.

## Implemented scope

The final boundary is mechanically usable for T1 stage D (one sealed DREAMER
unit) and T2's frozen 3-seed x 15-fold batch. It does **not yet implement T1
stage C** as a dedicated one-shot confirmatory LODO batch. In particular, there
is currently no independently scored C receipt that `run-final` requires, and
the claim lock does not bind an A high-water snapshot or the exact source tree.
Therefore a DREAMER `final-result.json` alone must not be reported as a complete
reference §7.4 T1 qualification. This is a known product gap, not an operator
step to simulate by repeatedly running the ordinary research loop.

The boundary also does not prove novelty, statistical superiority, or the
provenance of bytes manually supplied by the operator. The operator selecting
the final source tree remains trusted. Automated repository imports, uploads,
asset references, and host tools are mechanically disabled in qualification
mode.

## 1. Prepare the data views

Use a dedicated non-root research account and root as the sealed-data/scoring
operator. The shared parent must be `0755`: the research process opens paths one
component at a time. The sealed directory becomes root-owned `0711`; its files
remain root-owned `0400`, so the research UID can verify metadata but cannot
read labels.

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
  --archive /absolute/DREAMER.zip \
  --public-root /absolute/qualification-data/dreamer-public \
  --sealed-root /absolute/qualification-data/dreamer-sealed \
  --secret-file /absolute/qualification-data/hmac-secret \
  --label-rule /absolute/qualification-data/dreamer-rule.json \
  --research-uid "$RESEARCH_UID" --evaluator-uid 0 \
  > /absolute/qualification-data/dreamer-receipt.json
```

Back up the receipts and destroy the HMAC secret. Retaining it where the
research UID can read it defeats the opaque order/ID firewall.

## 2. Install only the immutable contract before research

Use a dedicated work root and system-root policy for each task. The canonical
contract protocol is `meta-research-qualification-firewall/v1`; its closed
top-level shape is:

```text
version, protocol, task, research_uid, evaluator_uid, forbid_code_imports,
mounts[], sealed_truth, final
```

Each mount contains exactly `path`, `role`, `dataset`, `fold`, and
`view_receipt_sha256`. `sealed_truth` contains the evaluator-owned `truth.json`
path and hash. T1 requires at least three distinct `explore` datasets and one
prepared DREAMER `sealed_holdout`. T2 requires prepared folds 1..15 and exactly
three frozen seeds. The strict semantic validator in
`orchestrator.qualification_firewall` is authoritative.

The task-specific `final` objects are exactly:

```json
{"classes":2,"folds":[],"gpu_required":false,"seeds":[],"unit_ids":["dreamer"]}
```

```json
{"classes":3,"folds":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],"gpu_required":false,"seeds":[7,17,29],"unit_ids":[]}
```

The first is T1 and the second is T2; replace the three T2 seeds before
contract installation if needed, and then use the same values in the claim.
Set `gpu_required:true` in both contract and claim when GPU is mandatory. T1
explore mounts use `fold:null` and may use `view_receipt_sha256:null`; DREAMER
must bind the hash of its `qualification-view.json`. Each T2 mount is one
`seed-public/fold-NN` root with role `fold`, dataset `SEED`, integer fold, and
its own view receipt hash. Every stored hash has the form
`sha256:<64-lowercase-hex>`.

Set both policy arrays to exactly the contract mount paths, with no extra path:

```yaml
execution:
  path_allowlist: [ ...exact contract mount paths... ]
  sandbox:
    readonly_mounts: [ ...the same exact paths... ]
```

The contract file must be canonical UTF-8 JSON: sorted keys, compact separators,
finite numbers, and one trailing newline. Install the contract as the research
UID before the first database/cycle is created. Do **not** lock the claim yet:

```bash
python -m orchestrator.qualification_firewall --work-root "$WORK" \
  install-contract --contract /absolute/contract.json
python -m orchestrator.qualification_firewall --work-root "$WORK" verify
```

The verification output must show `claim_locked:false` and
`final_consumed:false`. Contract installation after `research.sqlite` or
`cycles/` exists is correctly rejected.

## 3. Run stage A research

Run the ordinary owner as the research UID, using the qualification-specific
system root and its production connector:

```bash
python -m orchestrator.run \
  --system-root "$QUALIFICATION_SYSTEM_ROOT" \
  --work-root "$WORK" \
  --max-cycles "$EXPLORATION_CYCLE_LIMIT" \
  --connector-profile "$QUALIFICATION_CONNECTOR_PROFILE"
```

With a contract installed, every stage worker is tool-free, code imports and
external asset references are rejected, and an invocation receives only a
contract mount explicitly named in its validated argv. For T1, DREAMER remains
unmountable during A/B/C/HPO/claim. For T2, all 15 final fold views remain
unmountable until the irreversible final marker; consequently the present A
phase is literature/method/source construction, not iterative tuning on the
sealed SEED folds.

When exploration is complete, stop the owner cleanly and verify the latest
storage snapshot before creating the claim lock:

```bash
python -m orchestrator.storage_ops --work-root "$WORK" verify
```

## 4. Lock stage B exactly once

Derive the claim from the append-only A evidence. The claim protocol is
`meta-research-qualification-claim-lock/v1`; its closed common fields are:

```text
version, protocol, task, claims, feature_operator, label_mapping, model,
preprocessing, hpo, search_space, primary_metrics, statistical_tests,
multiple_testing, exclusion_rules, controls, datasets, final_command
```

The validator enforces 1..3 distinct claims and the task's mandatory controls.
T1 additionally freezes exploration/confirmatory datasets and the exact public
DREAMER label rule. T2 freezes subjects 1..15, all 15 folds, the three seeds,
and source-inner-LOSO or target-X-only HPO. `final_command.argv` is direct argv,
not shell text, and must contain `{src}` and `{data}` (plus `{seed}`/`{fold}` for
T2); output is fixed to `predictions.json`.

Use these exact task-specific fragments as the starting point; descriptive
objects such as `model` and `statistical_tests` must be replaced with the
actual frozen choices rather than left as placeholders.

T1:

```json
{
  "controls":["majority","class-prior-random","matched-random","label-permutation","subject-id-only","dataset-id-only","trial-id-only","source-only-linear","confidence-only","preprocessing-consistency","leakage-probe"],
  "datasets":{
    "confirmatory_lodo":["SEED","SEED-IV","FACED"],
    "exploration":["SEED","SEED-IV","FACED"],
    "sealed_holdout":{"comparison":"higher_is_positive","dataset":"DREAMER","neutral_policy":"drop","score":"valence","threshold":3}
  },
  "final_command":{"argv":["python","{src}/predict.py","--data","{data}"],"gpu_required":false,"output":"predictions.json"}
}
```

The two dataset lists must each contain exactly the distinct T1 explore dataset
names in the contract, and the DREAMER rule must equal the prepared public
manifest.

T2:

```json
{
  "controls":["majority","source-prior-random","source-only-linear","source-only-mlp","source-only-deep","single-best-source","confidence-only","label-shuffle","trial-id-only"],
  "datasets":{
    "classes":3,
    "dataset":"SEED",
    "final_folds":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15],
    "final_seeds":[7,17,29],
    "hpo_labels":"source-inner-loso",
    "input":"1s-nonoverlap-DE-62x5",
    "normalization":"per-fold",
    "subjects":[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15]
  },
  "final_command":{"argv":["python","{src}/predict.py","--data","{data}","--seed","{seed}","--fold","{fold}"],"gpu_required":false,"output":"predictions.json"}
}
```

`hpo_labels` may instead be `unsupervised-target-x`. To canonicalize a completed
draft before either install/lock command:

```bash
python - /absolute/draft.json /absolute/canonical.json <<'PY'
import json, os, sys
from pathlib import Path

source, target = map(Path, sys.argv[1:])
def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value

value = json.loads(source.read_text(encoding="utf-8"),
                   object_pairs_hook=unique_object,
                   parse_constant=lambda token: (_ for _ in ()).throw(
                       ValueError(f"non-finite JSON: {token}")))
target.write_bytes((json.dumps(
    value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    allow_nan=False) + "\n").encode("utf-8"))
os.chmod(target, 0o600)
PY
```

The firewall still performs the authoritative closed-field, filesystem,
ownership, receipt, and cross-field validation.

Publish the canonical claim as the research UID while the owner is stopped:

```bash
python -m orchestrator.qualification_firewall --work-root "$WORK" \
  lock-claim --claim /absolute/claim.json
python -m orchestrator.qualification_firewall --work-root "$WORK" verify
```

The output must now show `claim_locked:true`. A second or changed claim is
rejected by no-clobber publication. Until the missing T1-C boundary described
above is implemented, do not resume the generic T1 research loop and call the
result a confirmatory run: the current code does not prevent result-driven
adaptation after B.

## 5. Run the implemented irreversible final batch

The final source tree must be owned by the research UID, contain no `.git`,
symlinks, group/world-writable files, or multi-linked files, and stay
byte-identical for the batch. Its command writes only canonical
`predictions.json`:

```json
{"fold":null,"probabilities":[[0.9,0.1]],"sample_ids":["64-lowercase-hex"],"seed":null,"unit_id":"dreamer","version":1}
```

T2 uses the frozen integer seed, fold 1..15, three probabilities per row, and
unit IDs such as `seed-17-fold-08`. Sample IDs must be the exact public set.

Run once as the research UID:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" run-final \
  --system-root "$QUALIFICATION_SYSTEM_ROOT" \
  --source-root /absolute/frozen-source
```

For a GPU contract, also pass the required path value
`--gpu-contract /absolute/gpu-contract.json`. The exact allocation canary must
bind the claim/source/runtime, container inventory log, fenced guardian receipt,
and sandbox spec before the irreversible marker is written.

Do not hand-transcribe that file. As the root/deployment operator, capture the
exact successful final deployment receipt path from stage A, verify it, and
extract its normalized allocation before handing the read-only file to the
research UID:

```bash
set -euC
test ! -e "$GPU_CONTRACT"
trap 'rm -f "$GPU_CONTRACT"' EXIT
jq -e -S -c --arg work "$WORK" --arg owner "$DEPLOYMENT_OWNER_ID" '
  if (.phase == "final" and .production_ready == true and
      .owner_id == $owner and .prerequisite.owner_id == $owner and
      .prerequisite.attestation.value.work_root.path == $work and
      .prerequisite.gpu_contract != null)
  then .prerequisite.gpu_contract
  else error("deployment receipt does not match qualification work/owner/GPU")
  end
' "$DEPLOYMENT_RECEIPT" > "$GPU_CONTRACT"
chown "$RESEARCH_UID" "$GPU_CONTRACT"
chmod 0400 "$GPU_CONTRACT"
trap - EXIT
```

`$DEPLOYMENT_RECEIPT` must belong to this exact qualification work root and
deployment identity. `$DEPLOYMENT_OWNER_ID` is the exact owner ID printed/bound
by that stage-A receipt; do not select either value by an ambiguous “latest
file” glob.

Then run the independent scorer as root:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" score-final
```

Both CLI commands return `0` only for a successful scientific outcome and `3`
when they durably publish a failed batch/score. Operational violations still
fail loudly. The authoritative result is
`state/qualification/final-result.json`; require both of these gates in any
wrapper:

```bash
jq -e '.failure_count == 0' "$WORK/state/qualification/final/batch.json"
jq -e '.status == "success"' "$WORK/state/qualification/final-result.json"
```

Once final is consumed, the ordinary research system permanently refuses to
restart that work root, so target metrics cannot feed another research turn.
Score replay rereads truth and predictions and recomputes metrics before
accepting an existing result. A failed/spent unit is never rerun.
