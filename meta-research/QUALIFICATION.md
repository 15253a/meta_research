# T1/T2 qualification runbook

This is a thin qualification boundary around the existing orchestrator. It adds
no second database, scheduler, daemon, or research state machine. It provides:

- trusted SEED/DREAMER public views and evaluator-owned truth;
- an immutable pre-start contract and a later, one-time claim lock;
- no-host-tools research workers and exact read-only Docker mounts;
- one spent-before-spawn T1 confirmatory LODO batch plus a root audit authority;
- one irreversible final predictor batch; and
- a root-operated scorer that ignores candidate-reported metrics.

## Implemented scope

The machine boundary implements T1 A→B→C→D admission and T2's frozen 3-seed x
15-fold final batch. B binds the terminal A high-water snapshot, exact source,
explore-view trees, claim, and direct C command. C is one spent-before-spawn
LODO batch; it cannot mount DREAMER and cannot be rerun after an uncertain or
failed start. T1-D remains locked until a root-controlled external audit
authority says every required C review check passed. The final-consumed v3
marker binds that authority hash.

This does not turn a schema check into scientific judgment. The machine proves
the frozen identities, mount exclusion, execution receipt, output shape,
single-spend lifecycle, and audit-authority chain. A named root evaluator must
still inspect the LODO protocol, held-out-label isolation, metrics/statistics,
frozen claims and controls, and post-lock novelty evidence. Final production
acceptance additionally requires the real target environment and human signoff;
a DREAMER `final-result.json` alone is not that signoff.

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

When exploration is complete, stop the owner cleanly. This is a useful preview,
but it is not the locking authority:

```bash
python -m orchestrator.storage_ops --work-root "$WORK" verify
```

`lock-claim` repeats that complete verification under the exact work-root
instance lease, checks that live SQLite is quiescent, and requires its latest
terminal cycle/status to equal the immutable snapshot manifest. It never calls
`reconcile`; a missing terminal snapshot must be repaired by the ordinary owner
before B can be locked.

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
ownership, receipt, and cross-field validation. T1 also needs one direct-argv
confirmatory batch command. It must contain `{src}`, directly name every exact
explore root from the contract, must not name DREAMER, and must write
`confirmatory.json`. For example:

```json
{"argv":["python","{src}/confirm.py","--data","/absolute/SEED","--data","/absolute/SEED-IV","--data","/absolute/FACED"],"gpu_required":false,"output":"confirmatory.json"}
```

Publish the canonical claim as the research UID while the owner is stopped:

```bash
python -m orchestrator.qualification_firewall --work-root "$WORK" \
  lock-claim --claim /absolute/claim.json \
  --source-root /absolute/frozen-source \
  --confirmatory-command /absolute/confirmatory-command.json
python -m orchestrator.qualification_firewall --work-root "$WORK" verify
```

For T2, omit `--confirmatory-command`; `--source-root` is still required. The
output must show both `claim_boundary_locked:true` and `claim_locked:true`. The
boundary binds the claim hash, frozen source, verified A high-water, and exact
explore-tree hashes. It is published before the claim, so a crash in between
already closes ordinary research and exact replay only completes the same
claim. A claim-only legacy work root is rejected rather than backfilled.

After either boundary or claim appears, `orchestrator.run` fails before Docker,
SQLite, connectors, or providers. Do not resume the generic loop. The separate
T1-C command below is the only path allowed to remount all frozen explore views;
ordinary post-B exploration is rejected by both the owner entry and the
qualification firewall.

## 5. Run and audit T1 stage C

Skip this section for T2. The confirmatory program must write exactly one
canonical `confirmatory.json`. Its closed top-level shape is:

```text
version, protocol, folds, aggregate, audit_material
```

`protocol` is `meta-research-qualification-confirmatory-output/v1`. `folds`
must be canonically ordered and exactly cover the frozen `confirmatory_lodo`
datasets. Each row has exactly `held_out_dataset`, `status`, `metrics`, and
`failure`; every fold must report success for C to succeed. `aggregate` and
`audit_material` are non-empty objects containing the frozen analysis and the
materials the independent evaluator will review. For example:

```json
{"aggregate":{"direction_consistent":true,"meta_effect":0.1},"audit_material":{"controls":"sha256:0000000000000000000000000000000000000000000000000000000000000000","novelty":"sha256:1111111111111111111111111111111111111111111111111111111111111111"},"folds":[{"failure":null,"held_out_dataset":"FACED","metrics":{"effect":0.1},"status":"success"},{"failure":null,"held_out_dataset":"SEED","metrics":{"effect":0.1},"status":"success"},{"failure":null,"held_out_dataset":"SEED-IV","metrics":{"effect":0.1},"status":"success"}],"protocol":"meta-research-qualification-confirmatory-output/v1","version":1}
```

Run C once as the research UID with the same frozen source used at B:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" \
  run-confirmatory \
  --system-root "$QUALIFICATION_SYSTEM_ROOT" \
  --source-root /absolute/frozen-source
```

When GPU is frozen in the contract, also pass the exact read-only GPU contract
described in section 6. The command verifies the source and every explore tree
against B before the sandbox is prepared, names all explore roots, and excludes
DREAMER. It publishes `state/qualification/confirmatory/spent.json` before
preparing or spawning the batch. A crash after that point is recovered from the
guardian receipt or durably failed; it is never silently executed again.
Before the first spend, the entire candidate-output namespace must be absent;
a pre-seeded `confirmatory.json` is a hard error. On both the normal and crash
recovery paths, success additionally requires the deterministic sandbox
`*.promoted.json` receipt to contain exactly one output ledger row for
`confirmatory.json` with the same hash and byte count. That promotion reference
is bound into `confirmatory/result.json`, so a stale host file that the sandbox
did not produce cannot become C evidence.

The CLI returns `0` only when the terminal C output and execution receipt are
valid, and `3` for a durable failed result. Require:

```bash
jq -e '.status == "success"' \
  "$WORK/state/qualification/confirmatory/result.json"
```

That success means the machine checks passed, not that the claim passed
scientific review. Before D, a separate root evaluator must inspect at least:

- the exact LODO train/held-out split and absence of held-out labels from fit,
  preprocessing, model selection, and HPO;
- metric/statistical recomputation against the frozen plan;
- all mandatory controls and the 1..3 locked claims; and
- the post-lock novelty search/query ledger and its conclusions.

The evaluator prepares a canonical, root-owned `0400` JSON input outside
`$WORK`. Its closed shape is:

```text
version, protocol, task, claim_boundary_sha256,
confirmatory_result_sha256, auditor, checks, evidence, notes,
reviewed_at_unix
```

`protocol` is
`meta-research-qualification-confirmatory-audit-input/v1`. `checks` contains
exactly these Boolean keys:

```text
lodo_protocol_verified
heldout_label_isolation_verified
metric_and_statistics_verified
claim_and_controls_reviewed
novelty_audit_completed
```

Compute the two binding hashes from the immutable files, not from parsed or
reformatted JSON:

```bash
BOUNDARY_SHA="sha256:$(sha256sum \
  "$WORK/state/qualification/claim-boundary.json" | awk '{print $1}')"
RESULT_SHA="sha256:$(sha256sum \
  "$WORK/state/qualification/confirmatory/result.json" | awk '{print $1}')"
```

The following is a structural draft only. Replace both binding hashes, every
evidence reference/digest, the auditor, notes, and timestamp with the reviewed
values, then run the canonicalizer from section 4 before changing ownership:

```json
{
  "auditor":"replace-with-named-root-evaluator",
  "checks":{
    "claim_and_controls_reviewed":true,
    "heldout_label_isolation_verified":true,
    "lodo_protocol_verified":true,
    "metric_and_statistics_verified":true,
    "novelty_audit_completed":true
  },
  "claim_boundary_sha256":"sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "confirmatory_result_sha256":"sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "evidence":[
    {"check":"claim_and_controls_reviewed","ref":"file:///absolute/claim-controls-evidence","sha256":"sha256:2222222222222222222222222222222222222222222222222222222222222222"},
    {"check":"heldout_label_isolation_verified","ref":"file:///absolute/isolation-evidence","sha256":"sha256:3333333333333333333333333333333333333333333333333333333333333333"},
    {"check":"lodo_protocol_verified","ref":"file:///absolute/lodo-evidence","sha256":"sha256:4444444444444444444444444444444444444444444444444444444444444444"},
    {"check":"metric_and_statistics_verified","ref":"file:///absolute/metric-evidence","sha256":"sha256:5555555555555555555555555555555555555555555555555555555555555555"},
    {"check":"novelty_audit_completed","ref":"file:///absolute/novelty-evidence","sha256":"sha256:6666666666666666666666666666666666666666666666666666666666666666"}
  ],
  "notes":"replace-with-the-reviewed-C-verdict",
  "protocol":"meta-research-qualification-confirmatory-audit-input/v1",
  "reviewed_at_unix":1,
  "task":"T1",
  "version":1
}
```

Every key needs at least one evidence row with exactly `check`, `ref`, and a
`sha256:<64-lowercase-hex>` digest. The CLI records those evaluator assertions;
it deliberately does not dereference arbitrary evidence URIs. The evaluator is
responsible for keeping the referenced raw evidence immutable and for using its
actual digest. `reviewed_at_unix` must not precede the terminal C result.

Publish the verdict from a dedicated root-owned, non-group/world-writable
authority directory outside the research work root. Do not use `/tmp` or a
research-writable ancestor. The research UID must be able to traverse the known
authority path for D admission, so use execute-only `other` access rather than
an unreadable `0700` directory:

```bash
AUTH_DIR="/var/lib/meta-research/qualification-authorities/$QUALIFICATION_ID"
AUDIT_INPUT="$AUTH_DIR/t1-c-audit-input.json"
AUDIT_AUTHORITY="$AUTH_DIR/t1-c-audit.json"
install -d -o root -g root -m 0711 "$AUTH_DIR"
install -o root -g root -m 0400 \
  /absolute/canonical-confirmatory-audit-input.json "$AUDIT_INPUT"

python -m orchestrator.qualification_runner --work-root "$WORK" \
  audit-confirmatory \
  --audit-input "$AUDIT_INPUT" \
  --authority-output "$AUDIT_AUTHORITY"
```

The external authority is root-owned `0444`; `$WORK` receives `0400` copies of
the input and authority reference. In addition, the CLI creates a root-owned
`0555` directory named `.meta-research-qualification-decisions-v1` beside the
sealed truth and publishes one root-owned `0444` decision record keyed by the
canonical work-root path. Its protocol is
`meta-research-qualification-confirmatory-audit-decision/v1`. The record binds
the contract, sealed truth, claim, boundary, C result, verdict, and external
authority. This root ledger—not the
research-owned reference—is the permanence authority: deleting `$WORK` copies
cannot replace a failed decision with a later passed one. Back up this ledger
with the sealed truth.

A passed audit returns `0`; any false check publishes a failed authority,
returns `3`, and permanently rejects D for that work root. Start a new
qualification work root if the frozen scientific attempt must change. The
canonical operator input and its derived authority are each bounded to 256
KiB. Before publishing, the CLI validates the external path/conflict and both
research-copy conflicts, derives and size-checks every record, then publishes
the immutable root decision first, the external authority second, and only then
the repairable `$WORK` copies. An oversized input or invalid/conflicting
authority path therefore fails without leaving a research-owned half-chain. A
crash after the decision is fail-closed: an exact retry repairs the remaining
chain, while a different verdict conflicts with the already durable decision.

## 6. Run the irreversible stage D / T2 final batch

The final source tree must be owned by the research UID, contain no `.git`,
symlinks, group/world-writable files, or multi-linked files, and stay
byte-identical for the batch. Its command writes only canonical
`predictions.json`:

```json
{"fold":null,"probabilities":[[0.9,0.1]],"sample_ids":["64-lowercase-hex"],"seed":null,"unit_id":"dreamer","version":1}
```

T2 uses the frozen integer seed, fold 1..15, three probabilities per row, and
unit IDs such as `seed-17-fold-08`. Sample IDs must be the exact public set.

For T1, every admission path—including direct final-marker consumption,
firewall verification, and final mount authorization—requires the passed C
authority above and revalidates the durable claim/boundary, C result, spent
receipt, terminal drained guardian execution receipt, exact sandbox promotion
ledger, output bytes, durable audit input, external root ownership, immutable
root decision ledger, and complete hash chain before the DREAMER sandbox can be
constructed. A standalone root audit JSON or pre-seeded host output is not C
proof. T2 has no C authority. Run once as the research UID:

```bash
python -m orchestrator.qualification_runner --work-root "$WORK" run-final \
  --system-root "$QUALIFICATION_SYSTEM_ROOT" \
  --source-root /absolute/frozen-source
```

New final markers use
`meta-research-qualification-final-consumed/v3`. Legacy v2 final work roots are
already terminal and are deliberately not backfilled into this authority chain;
retain their original code/evidence for audit or start a fresh qualification
work root. In particular, a pre-C T1 marker can never be upgraded into C
admission after seeing DREAMER.

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

`run-final` and `score-final` return `0` only for a successful scientific
outcome and `3` when they durably publish a failed batch/score. Operational
violations still fail loudly. The authoritative result is
`state/qualification/final-result.json`; require both of these gates in any
wrapper:

```bash
jq -e '.failure_count == 0' "$WORK/state/qualification/final/batch.json"
jq -e '.status == "success"' "$WORK/state/qualification/final-result.json"
```

Once final is consumed, the ordinary research system permanently refuses to
restart that work root, so target metrics cannot feed another research turn.
Score replay rereads truth and predictions and recomputes metrics before
accepting an existing result. For T1 it also revalidates the external C audit
and requires its hash to equal `final-consumed.json`. A failed/spent unit is
never rerun.
