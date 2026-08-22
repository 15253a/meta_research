from __future__ import annotations

import base64
import hashlib
import json
import math
import sys


def _checkpoint_vectors(
    selected: object,
    *,
    sample_count: int,
) -> tuple[list[float], list[float], list[str]]:
    if not isinstance(selected, list) or len(selected) > 32:
        raise ValueError("invalid selected checkpoints")
    baseline: list[float] = []
    variant: list[float] = []
    content_hashes: list[str] = []
    for ordinal, item in enumerate(selected):
        if not isinstance(item, dict) or item.get("ordinal") != ordinal:
            raise ValueError("invalid checkpoint order")
        asset = item.get("asset")
        encoded = item.get("content_base64")
        if not isinstance(asset, dict) or not isinstance(encoded, str):
            raise ValueError("invalid checkpoint binding")
        expected_hash = asset.get("content_hash")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise ValueError("invalid checkpoint hash")
        content = base64.b64decode(encoded, validate=True)
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ValueError("checkpoint content changed")
        checkpoint = json.loads(content)
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("schema_ref")
            != "meta-research/micro-experiment-checkpoint/v1"
            or not isinstance(checkpoint.get("baseline"), list)
            or not isinstance(checkpoint.get("variant"), list)
            or len(checkpoint["baseline"]) != sample_count
            or len(checkpoint["variant"]) != sample_count
        ):
            raise ValueError("checkpoint shape invalid")
        checkpoint_baseline = [float(value) for value in checkpoint["baseline"]]
        checkpoint_variant = [float(value) for value in checkpoint["variant"]]
        if not all(
            math.isfinite(value)
            for value in (*checkpoint_baseline, *checkpoint_variant)
        ):
            raise ValueError("checkpoint value invalid")
        baseline.extend(checkpoint_baseline)
        variant.extend(checkpoint_variant)
        content_hashes.append(expected_hash)
    return baseline, variant, content_hashes


def main() -> int:
    request = json.load(sys.stdin)
    sample_count = int(request["sample_count"])
    variant_parameter = float(request["variant_parameter"])
    request_kind = request["request_kind"]
    selected = request["selected_checkpoints"]
    if (
        not 4 <= sample_count <= 4096
        or not math.isfinite(variant_parameter)
        or request_kind not in {"retrain", "remeasure"}
        or (request_kind == "retrain" and selected != [])
    ):
        raise ValueError("invalid experiment input")

    baseline, variant, checkpoint_hashes = _checkpoint_vectors(
        selected,
        sample_count=sample_count,
    )
    if not checkpoint_hashes:
        baseline = [
            (index - ((sample_count - 1) / 2.0)) / sample_count
            for index in range(sample_count)
        ]
        variant = [value + variant_parameter for value in baseline]
    print(
        f"state formation: {len(baseline)} deterministic samples; "
        f"selected checkpoints: {len(checkpoint_hashes)}",
        flush=True,
    )
    print("measurement: applying frozen arithmetic-mean protocol", flush=True)
    baseline_mean = math.fsum(baseline) / len(baseline)
    variant_mean = math.fsum(variant) / len(variant)
    mean_delta = variant_mean - baseline_mean
    checkpoint = (
        {
            "schema_ref": "meta-research/micro-experiment-checkpoint/v1",
            "baseline": baseline,
            "variant": variant,
        }
        if request_kind == "retrain"
        else None
    )
    result = {
        "checkpoint": checkpoint,
        "analysis": {
            "sample_count": len(baseline),
            "selected_checkpoint_count": len(checkpoint_hashes),
            "selected_checkpoint_content_hashes": checkpoint_hashes,
            "direction": (
                "negative" if mean_delta < 0 else "positive" if mean_delta > 0 else "zero"
            ),
            "interpretation": "direction is observable, not an acceptance gate",
        },
        "result_content": {
            "schema_ref": "meta-research/micro-experiment-result/v1",
            "metrics": {
                "baseline_mean": baseline_mean,
                "variant_mean": variant_mean,
                "mean_delta": mean_delta,
            },
            "aggregation": "single fixed sample set; arithmetic mean",
        },
    }
    print("measurement: all required metrics produced", flush=True)
    print("META_RESEARCH_RESULT\t" + json.dumps(result, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
