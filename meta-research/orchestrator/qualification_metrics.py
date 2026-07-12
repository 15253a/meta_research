"""Trusted, deterministic qualification metrics for the sealed T1/T2 tasks.

The caller supplies already-parsed canonical JSON values.  This module is a
pure validation/calculation boundary: it performs no filesystem, database,
network, clock, or random operation and deliberately depends only on the
Python standard library.

Metric definitions are fixed here rather than delegated to model-authored
code:

* prediction = the lowest class index attaining the maximum probability;
* balanced accuracy = mean recall over the declared class set (a class with
  zero true support contributes ``0``);
* macro F1 = mean one-vs-rest F1 over the declared class set, with a zero
  denominator contributing ``0``;
* NLL uses the natural logarithm and clips the true-class probability from
  below at ``1e-15``;
* top-label ECE uses ten equal-width bins ``[j/10, (j+1)/10)``; the last bin
  includes confidence 1.0.  Exact interior boundaries enter the upper bin.

Every accepted value and every returned value is JSON-safe.  In particular,
``bool`` is never accepted as an integer/number and NaN/Inf are rejected.
"""
from __future__ import annotations

import bisect
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any, Dict, List, Tuple


CONTRACT_VERSION = 1
ECE_BINS = 10
NLL_EPSILON = 1e-15
PROBABILITY_SUM_TOLERANCE = 1e-6
_METRIC_KEYS = ("accuracy", "balanced_accuracy", "macro_f1", "nll", "ece")
_ECE_BOUNDARIES = tuple(index / ECE_BINS for index in range(1, ECE_BINS))
_SAMPLE_ID_RE = re.compile(r"^[0-9a-f]{64}$")

__all__ = ["QualificationMetricsError", "score_qualification"]


class QualificationMetricsError(ValueError):
    """The trusted truth/prediction contract is malformed or incomplete."""


def _object(value: Any, *, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise QualificationMetricsError(f"{path} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], *, path: str) -> None:
    actual = set(value.keys())
    if actual != expected:
        raise QualificationMetricsError(
            f"{path} keys must be exactly {sorted(expected)!r}; got {sorted(map(str, actual))!r}")


def _list(value: Any, *, path: str) -> list[Any]:
    if type(value) is not list:
        raise QualificationMetricsError(f"{path} must be a JSON array")
    return value


def _int(value: Any, *, path: str) -> int:
    if type(value) is not int:
        raise QualificationMetricsError(f"{path} must be an integer (bool is forbidden)")
    return value


def _finite_number(value: Any, *, path: str) -> float:
    if type(value) is not float:
        raise QualificationMetricsError(f"{path} must be a finite float (bool/int are forbidden)")
    if not math.isfinite(value):
        raise QualificationMetricsError(f"{path} must be finite")
    return value


def _labels(value: Any, *, classes: int, path: str) -> Tuple[int, ...]:
    raw = _list(value, path=path)
    if not raw:
        raise QualificationMetricsError(f"{path} must not be empty")
    result = []
    for index, item in enumerate(raw):
        label = _int(item, path=f"{path}[{index}]")
        if not 0 <= label < classes:
            raise QualificationMetricsError(
                f"{path}[{index}]={label} is outside [0, {classes})")
        result.append(label)
    return tuple(result)


def _sample_ids(value: Any, *, samples: int, path: str) -> Tuple[str, ...]:
    raw = _list(value, path=path)
    if len(raw) != samples:
        raise QualificationMetricsError(f"{path} length must equal labels length")
    result = []
    for index, item in enumerate(raw):
        if type(item) is not str or _SAMPLE_ID_RE.fullmatch(item) is None:
            raise QualificationMetricsError(
                f"{path}[{index}] must be a 64-character lowercase opaque id")
        result.append(item)
    if len(set(result)) != len(result):
        raise QualificationMetricsError(f"{path} contains duplicate opaque ids")
    return tuple(result)


def _normalize_truth(truth: Mapping[str, Any]) -> Dict[str, Any]:
    value = _object(truth, path="truth")
    version = _int(value.get("version"), path="truth.version")
    if version != CONTRACT_VERSION:
        raise QualificationMetricsError("truth.version must be 1")
    task = value.get("task")
    if type(task) is not str or task not in {"T1", "T2"}:
        raise QualificationMetricsError("truth.task must be exactly 'T1' or 'T2'")

    if task == "T1":
        _exact_keys(value, {"version", "task", "classes", "units"}, path="truth")
        classes = _int(value["classes"], path="truth.classes")
        if classes != 2:
            raise QualificationMetricsError("T1 truth.classes must be 2")
        units = _list(value["units"], path="truth.units")
        if len(units) != 1:
            raise QualificationMetricsError("T1 truth.units must contain exactly one unit")
        unit = _object(units[0], path="truth.units[0]")
        _exact_keys(
            unit, {"unit_id", "sample_ids", "labels", "groups"},
            path="truth.units[0]")
        if type(unit["unit_id"]) is not str or unit["unit_id"] != "dreamer":
            raise QualificationMetricsError("T1 unit_id must be exactly 'dreamer'")
        labels = _labels(unit["labels"], classes=classes, path="truth.units[0].labels")
        sample_ids = _sample_ids(
            unit["sample_ids"], samples=len(labels), path="truth.units[0].sample_ids")
        if set(labels) != set(range(classes)):
            raise QualificationMetricsError("T1 truth labels must contain both locked classes")
        raw_groups = _list(unit["groups"], path="truth.units[0].groups")
        if len(raw_groups) != len(labels):
            raise QualificationMetricsError("T1 groups length must equal labels length")
        groups = tuple(
            _int(group, path=f"truth.units[0].groups[{index}]")
            for index, group in enumerate(raw_groups))
        return {
            "version": version, "task": task, "classes": classes,
            "unit_id": "dreamer", "sample_ids": sample_ids,
            "labels": labels, "groups": groups,
        }

    _exact_keys(value, {"version", "task", "classes", "folds"}, path="truth")
    classes = _int(value["classes"], path="truth.classes")
    if classes != 3:
        raise QualificationMetricsError("T2 truth.classes must be 3")
    raw_folds = _list(value["folds"], path="truth.folds")
    if len(raw_folds) != 15:
        raise QualificationMetricsError("T2 truth.folds must contain exactly 15 folds")
    folds: Dict[int, Dict[str, Any]] = {}
    for index, raw_fold in enumerate(raw_folds):
        fold = _object(raw_fold, path=f"truth.folds[{index}]")
        _exact_keys(
            fold, {"fold", "sample_ids", "labels"}, path=f"truth.folds[{index}]")
        fold_id = _int(fold["fold"], path=f"truth.folds[{index}].fold")
        if not 1 <= fold_id <= 15:
            raise QualificationMetricsError("T2 fold ids must be in [1, 15]")
        if fold_id in folds:
            raise QualificationMetricsError(f"duplicate T2 truth fold {fold_id}")
        labels = _labels(
            fold["labels"], classes=classes,
            path=f"truth.folds[{index}].labels")
        sample_ids = _sample_ids(
            fold["sample_ids"], samples=len(labels),
            path=f"truth.folds[{index}].sample_ids")
        if set(labels) != set(range(classes)):
            raise QualificationMetricsError(
                f"T2 truth fold {fold_id} must contain all three locked classes")
        folds[fold_id] = {"labels": labels, "sample_ids": sample_ids}
    if set(folds) != set(range(1, 16)):
        raise QualificationMetricsError("T2 truth folds must be exactly 1..15")
    return {"version": version, "task": task, "classes": classes, "folds": folds}


def _probabilities(value: Any, *, classes: int, samples: int,
                   path: str) -> Tuple[Tuple[float, ...], ...]:
    rows = _list(value, path=path)
    if len(rows) != samples:
        raise QualificationMetricsError(
            f"{path} sample count {len(rows)} does not match truth count {samples}")
    result = []
    for row_index, raw_row in enumerate(rows):
        row = _list(raw_row, path=f"{path}[{row_index}]")
        if len(row) != classes:
            raise QualificationMetricsError(
                f"{path}[{row_index}] must contain exactly {classes} probabilities")
        normalized = []
        for class_index, raw_probability in enumerate(row):
            probability = _finite_number(
                raw_probability, path=f"{path}[{row_index}][{class_index}]")
            if not 0.0 <= probability <= 1.0:
                raise QualificationMetricsError("probabilities must be in [0, 1]")
            normalized.append(probability)
        total = math.fsum(normalized)
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise QualificationMetricsError(
                f"{path}[{row_index}] probabilities sum to {total!r}, not 1 within 1e-6")
        result.append(tuple(normalized))
    return tuple(result)


def _prediction_header(value: Any, *, index: int) -> Mapping[str, Any]:
    prediction = _object(value, path=f"predictions[{index}]")
    _exact_keys(
        prediction,
        {"version", "unit_id", "seed", "fold", "sample_ids", "probabilities"},
        path=f"predictions[{index}]")
    if _int(prediction["version"], path=f"predictions[{index}].version") != 1:
        raise QualificationMetricsError("prediction.version must be 1")
    if type(prediction["unit_id"]) is not str:
        raise QualificationMetricsError(f"predictions[{index}].unit_id must be a string")
    return prediction


def _normalize_predictions(truth: Dict[str, Any], predictions: Any) -> List[Dict[str, Any]]:
    if (isinstance(predictions, (str, bytes, bytearray))
            or not isinstance(predictions, Sequence)):
        raise QualificationMetricsError("predictions must be a sequence of objects")
    raw = list(predictions)
    classes = truth["classes"]
    if truth["task"] == "T1":
        if len(raw) != 1:
            raise QualificationMetricsError("T1 requires exactly one prediction object")
        prediction = _prediction_header(raw[0], index=0)
        if prediction["unit_id"] != "dreamer":
            raise QualificationMetricsError("T1 prediction.unit_id must be 'dreamer'")
        if prediction["seed"] is not None or prediction["fold"] is not None:
            raise QualificationMetricsError("T1 prediction seed and fold must both be null")
        sample_ids = _sample_ids(
            prediction["sample_ids"], samples=len(truth["labels"]),
            path="predictions[0].sample_ids")
        if set(sample_ids) != set(truth["sample_ids"]):
            raise QualificationMetricsError("T1 prediction opaque id set differs from truth")
        raw_probabilities = _probabilities(
            prediction["probabilities"], classes=classes,
            samples=len(truth["labels"]), path="predictions[0].probabilities")
        by_id = dict(zip(sample_ids, raw_probabilities))
        return [{
            "unit_id": "dreamer", "seed": None, "fold": None,
            "probabilities": tuple(by_id[item] for item in truth["sample_ids"]),
        }]

    if len(raw) != 45:
        raise QualificationMetricsError("T2 requires exactly 3 seeds x 15 folds = 45 predictions")
    result = []
    seen = set()
    seeds = set()
    for index, item in enumerate(raw):
        prediction = _prediction_header(item, index=index)
        seed = _int(prediction["seed"], path=f"predictions[{index}].seed")
        if seed < 0:
            raise QualificationMetricsError("T2 prediction.seed must be non-negative")
        fold = _int(prediction["fold"], path=f"predictions[{index}].fold")
        if not 1 <= fold <= 15:
            raise QualificationMetricsError("T2 prediction.fold must be in [1, 15]")
        key = (seed, fold)
        if key in seen:
            raise QualificationMetricsError(f"duplicate T2 prediction for seed/fold {key!r}")
        seen.add(key)
        seeds.add(seed)
        expected_unit_id = f"seed-{seed}-fold-{fold:02d}"
        if prediction["unit_id"] != expected_unit_id:
            raise QualificationMetricsError(
                f"T2 prediction.unit_id must be {expected_unit_id!r}")
        truth_fold = truth["folds"][fold]
        sample_ids = _sample_ids(
            prediction["sample_ids"], samples=len(truth_fold["labels"]),
            path=f"predictions[{index}].sample_ids")
        if set(sample_ids) != set(truth_fold["sample_ids"]):
            raise QualificationMetricsError(
                f"T2 fold {fold} prediction opaque id set differs from truth")
        raw_probabilities = _probabilities(
            prediction["probabilities"], classes=classes,
            samples=len(truth_fold["labels"]),
            path=f"predictions[{index}].probabilities")
        by_id = dict(zip(sample_ids, raw_probabilities))
        result.append({
            "unit_id": prediction["unit_id"], "seed": seed, "fold": fold,
            "probabilities": tuple(
                by_id[item] for item in truth_fold["sample_ids"]),
        })
    if len(seeds) != 3:
        raise QualificationMetricsError("T2 predictions must contain exactly three distinct seeds")
    expected = {(seed, fold) for seed in seeds for fold in range(1, 16)}
    if seen != expected:
        raise QualificationMetricsError("T2 predictions must be the complete 3-seed x 15-fold cross product")
    return sorted(result, key=lambda item: (item["seed"], item["fold"]))


def _predicted_class(row: Sequence[float]) -> int:
    # max() is intentionally avoided so the lowest-index tie rule is explicit.
    winner = 0
    for index in range(1, len(row)):
        if row[index] > row[winner]:
            winner = index
    return winner


def _metric_values(labels: Sequence[int], probabilities: Sequence[Sequence[float]],
                   *, classes: int) -> Dict[str, float]:
    count = len(labels)
    predicted = [_predicted_class(row) for row in probabilities]
    correct = [int(actual == guess) for actual, guess in zip(labels, predicted)]

    true_count = [0] * classes
    predicted_count = [0] * classes
    true_positive = [0] * classes
    for actual, guess in zip(labels, predicted):
        true_count[actual] += 1
        predicted_count[guess] += 1
        if actual == guess:
            true_positive[actual] += 1

    recalls = [
        true_positive[index] / true_count[index] if true_count[index] else 0.0
        for index in range(classes)]
    f1_values = []
    for index in range(classes):
        denominator = true_count[index] + predicted_count[index]
        f1_values.append(
            2.0 * true_positive[index] / denominator if denominator else 0.0)

    nll = -math.fsum(
        math.log(max(NLL_EPSILON, row[actual]))
        for actual, row in zip(labels, probabilities)) / count
    if nll == 0.0:
        nll = 0.0  # canonicalize the otherwise possible JSON number ``-0.0``

    bin_counts = [0] * ECE_BINS
    bin_confidence: List[List[float]] = [[] for _ in range(ECE_BINS)]
    bin_correct = [0] * ECE_BINS
    for is_correct, row in zip(correct, probabilities):
        confidence = row[_predicted_class(row)]
        bin_index = bisect.bisect_right(_ECE_BOUNDARIES, confidence)
        bin_index = min(ECE_BINS - 1, bin_index)
        bin_counts[bin_index] += 1
        bin_confidence[bin_index].append(confidence)
        bin_correct[bin_index] += is_correct
    ece_terms = []
    for bin_index, bin_count in enumerate(bin_counts):
        if not bin_count:
            continue
        bin_accuracy = bin_correct[bin_index] / bin_count
        mean_confidence = math.fsum(bin_confidence[bin_index]) / bin_count
        ece_terms.append((bin_count / count) * abs(bin_accuracy - mean_confidence))

    result = {
        "accuracy": math.fsum(correct) / count,
        "balanced_accuracy": math.fsum(recalls) / classes,
        "macro_f1": math.fsum(f1_values) / classes,
        "nll": nll,
        "ece": math.fsum(ece_terms),
    }
    if any(not math.isfinite(value) for value in result.values()):
        raise QualificationMetricsError("internal metric result is non-finite")
    return result


def _mean_metrics(items: Sequence[Mapping[str, float]]) -> Dict[str, float]:
    if not items:
        raise QualificationMetricsError("cannot aggregate an empty metric set")
    return {
        key: math.fsum(item[key] for item in items) / len(items)
        for key in _METRIC_KEYS
    }


def score_qualification(
        truth: Mapping[str, Any],
        predictions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Validate sealed truth/predictions and return deterministic trusted metrics.

    ``truth`` must be a Mapping representing the exact version-1 T1 or T2
    object.  ``predictions`` must be a JSON array (a real ``list``) of exact
    version-1 prediction objects.  Inputs are never mutated.  T1 returns one
    overall ``metrics`` object plus sorted ``groups`` detail.  T2 returns all
    45 sorted ``folds``, three per-seed equal-fold means, and one equal-weight
    ``mean_subject_metrics`` summary across the 45 seed/fold cells.
    """
    normalized_truth = _normalize_truth(truth)
    normalized_predictions = _normalize_predictions(normalized_truth, predictions)
    classes = normalized_truth["classes"]

    if normalized_truth["task"] == "T1":
        prediction = normalized_predictions[0]
        labels = normalized_truth["labels"]
        probabilities = prediction["probabilities"]
        group_details = []
        for group in sorted(set(normalized_truth["groups"])):
            indices = [
                index for index, item in enumerate(normalized_truth["groups"])
                if item == group]
            group_labels = [labels[index] for index in indices]
            group_probabilities = [probabilities[index] for index in indices]
            group_details.append({
                "group": group,
                "sample_count": len(indices),
                "metrics": _metric_values(
                    group_labels, group_probabilities, classes=classes),
            })
        return {
            "version": CONTRACT_VERSION,
            "task": "T1",
            "classes": classes,
            "unit_id": "dreamer",
            "sample_count": len(labels),
            "metrics": _metric_values(labels, probabilities, classes=classes),
            "groups": group_details,
        }

    fold_details = []
    metrics_by_seed: Dict[int, List[Dict[str, float]]] = {}
    for prediction in normalized_predictions:
        labels = normalized_truth["folds"][prediction["fold"]]["labels"]
        metrics = _metric_values(
            labels, prediction["probabilities"], classes=classes)
        fold_details.append({
            "seed": prediction["seed"],
            "fold": prediction["fold"],
            "unit_id": prediction["unit_id"],
            "sample_count": len(labels),
            "metrics": metrics,
        })
        metrics_by_seed.setdefault(prediction["seed"], []).append(metrics)
    seed_details = [{
        "seed": seed,
        "fold_count": len(metrics_by_seed[seed]),
        "mean_subject_metrics": _mean_metrics(metrics_by_seed[seed]),
    } for seed in sorted(metrics_by_seed)]
    return {
        "version": CONTRACT_VERSION,
        "task": "T2",
        "classes": classes,
        "seed_count": len(seed_details),
        "fold_count": 15,
        "folds": fold_details,
        "seeds": seed_details,
        "mean_subject_metrics": _mean_metrics(
            [detail["metrics"] for detail in fold_details]),
    }


def compute_qualification_metrics(
        truth: Mapping[str, Any],
        predictions: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compatibility name; new callers should use :func:`score_qualification`."""
    return score_qualification(truth, predictions)
