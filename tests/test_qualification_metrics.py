"""Adversarial tests for the DB-free trusted T1/T2 metric boundary."""
from __future__ import annotations

import copy
import hashlib
import json
import math

import pytest

from orchestrator import qualification_metrics as QM


def _ids(prefix, count):
    return [hashlib.sha256(f"{prefix}:{index}".encode()).hexdigest()
            for index in range(count)]


def _t1_truth(*, labels=None, groups=None):
    labels = [0, 1, 1, 0] if labels is None else labels
    groups = [10, 10, 20, 20] if groups is None else groups
    return {
        "version": 1,
        "task": "T1",
        "classes": 2,
        "units": [{
            "unit_id": "dreamer", "sample_ids": _ids("dreamer", len(labels)),
            "labels": labels, "groups": groups}],
    }


def _t1_predictions(probabilities=None):
    probabilities = ([[0.9, 0.1], [0.2, 0.8], [0.6, 0.4], [0.3, 0.7]]
                     if probabilities is None else probabilities)
    return [{
        "version": 1,
        "unit_id": "dreamer",
        "seed": None,
        "fold": None,
        "sample_ids": _ids("dreamer", len(probabilities)),
        "probabilities": probabilities,
    }]


def _t2_truth(*, long_first_fold=False):
    folds = []
    for fold in range(15, 0, -1):
        labels = [0, 1, 2] * (10 if long_first_fold and fold == 1 else 1)
        folds.append({
            "fold": fold, "sample_ids": _ids(f"fold-{fold}", len(labels)),
            "labels": labels})
    return {"version": 1, "task": "T2", "classes": 3, "folds": folds}


def _one_hot_predictions(labels, *, classes=3, rotate=False):
    rows = []
    for label in labels:
        predicted = (label + 1) % classes if rotate else label
        rows.append([1.0 if index == predicted else 0.0 for index in range(classes)])
    return rows


def _t2_predictions(truth, *, bad_seed_fold=None):
    folds = {item["fold"]: item for item in truth["folds"]}
    result = []
    # Deliberately non-canonical order: output must sort by seed, then fold.
    for seed in (42, 1, 7):
        for fold in range(15, 0, -1):
            result.append({
                "version": 1,
                "unit_id": f"seed-{seed}-fold-{fold:02d}",
                "seed": seed,
                "fold": fold,
                "sample_ids": list(folds[fold]["sample_ids"]),
                "probabilities": _one_hot_predictions(
                    folds[fold]["labels"], rotate=(bad_seed_fold == (seed, fold))),
            })
    return result


def _assert_rejected(truth, predictions):
    with pytest.raises(QM.QualificationMetricsError):
        QM.score_qualification(truth, predictions)


def test_t1_exact_metrics_groups_nll_and_ece():
    result = QM.score_qualification(_t1_truth(), _t1_predictions())

    assert result["version"] == 1
    assert result["task"] == "T1"
    assert result["classes"] == 2
    assert result["unit_id"] == "dreamer"
    assert result["sample_count"] == 4
    assert result["metrics"] == pytest.approx({
        "accuracy": 0.5,
        "balanced_accuracy": 0.5,
        "macro_f1": 0.5,
        "nll": -(math.log(0.9) + math.log(0.8) + math.log(0.4) + math.log(0.3)) / 4,
        "ece": 0.4,
    })
    assert [item["group"] for item in result["groups"]] == [10, 20]
    assert [item["sample_count"] for item in result["groups"]] == [2, 2]
    assert result["groups"][0]["metrics"]["accuracy"] == 1.0
    assert result["groups"][1]["metrics"]["accuracy"] == 0.0
    # allow_nan=False is the final JSON-safety assertion, not a lossy sanitizer.
    assert json.loads(json.dumps(result, allow_nan=False, sort_keys=True)) == result


def test_t1_ece_boundary_enters_upper_bin_and_argmax_tie_uses_lowest_class():
    boundary = QM.compute_qualification_metrics(
        _t1_truth(labels=[0, 1], groups=[1, 1]),
        _t1_predictions([[0.6, 0.4], [0.59, 0.41]]))
    # 0.60 is in [0.6, 0.7), while 0.59 is in [0.5, 0.6).
    assert boundary["metrics"]["ece"] == pytest.approx(0.495)

    tied = QM.compute_qualification_metrics(
        _t1_truth(labels=[0, 1], groups=[7, 7]),
        _t1_predictions([[0.5, 0.5], [0.4, 0.6]]))
    assert tied["metrics"]["accuracy"] == 1.0
    assert tied["groups"][0]["metrics"]["accuracy"] == 1.0


def test_nll_clips_zero_true_probability_at_one_e_minus_fifteen():
    result = QM.compute_qualification_metrics(
        _t1_truth(labels=[0, 1], groups=[1, 1]),
        _t1_predictions([[0.0, 1.0], [1.0, 0.0]]))
    assert result["metrics"]["nll"] == pytest.approx(-math.log(1e-15))


def test_t2_per_fold_per_seed_and_mean_subject_perfect_result():
    truth = _t2_truth()
    predictions = _t2_predictions(truth)
    before_truth, before_predictions = copy.deepcopy(truth), copy.deepcopy(predictions)

    result = QM.compute_qualification_metrics(truth, predictions)

    assert truth == before_truth and predictions == before_predictions
    assert result["version"] == 1 and result["task"] == "T2"
    assert result["classes"] == 3
    assert result["seed_count"] == 3 and result["fold_count"] == 15
    assert len(result["folds"]) == 45
    assert [(item["seed"], item["fold"]) for item in result["folds"]] == [
        (seed, fold) for seed in (1, 7, 42) for fold in range(1, 16)]
    assert [item["seed"] for item in result["seeds"]] == [1, 7, 42]
    for detail in result["folds"]:
        assert detail["metrics"] == {
            "accuracy": 1.0,
            "balanced_accuracy": 1.0,
            "macro_f1": 1.0,
            "nll": 0.0,
            "ece": 0.0,
        }
        assert math.copysign(1.0, detail["metrics"]["nll"]) == 1.0
    for detail in result["seeds"]:
        assert detail["fold_count"] == 15
        assert detail["mean_subject_metrics"] == result["mean_subject_metrics"]
    assert result["mean_subject_metrics"] == {
        "accuracy": 1.0,
        "balanced_accuracy": 1.0,
        "macro_f1": 1.0,
        "nll": 0.0,
        "ece": 0.0,
    }
    json.dumps(result, allow_nan=False, sort_keys=True)


def test_t2_mean_subject_is_equal_weight_over_folds_not_pooled_samples():
    truth = _t2_truth(long_first_fold=True)
    predictions = _t2_predictions(truth, bad_seed_fold=(1, 1))
    result = QM.compute_qualification_metrics(truth, predictions)

    seed_one = next(item for item in result["seeds"] if item["seed"] == 1)
    assert seed_one["mean_subject_metrics"]["accuracy"] == pytest.approx(14 / 15)
    assert result["mean_subject_metrics"]["accuracy"] == pytest.approx(44 / 45)
    # A pooled calculation would weight the intentionally long failed subject more.
    pooled_accuracy = (14 * 3 + 2 * (30 + 14 * 3)) / (30 + 14 * 3) / 3
    assert result["mean_subject_metrics"]["accuracy"] != pytest.approx(pooled_accuracy)


def test_t2_balanced_accuracy_and_macro_f1_use_the_locked_three_class_universe():
    truth = {
        "version": 1, "task": "T2", "classes": 3,
        "folds": [{
            "fold": fold, "sample_ids": _ids(f"imbalanced-{fold}", 6),
            "labels": [0, 0, 0, 0, 1, 2]}
                  for fold in range(1, 16)],
    }
    predictions = []
    for seed in (1, 2, 3):
        for fold in range(1, 16):
            predictions.append({
                "version": 1, "unit_id": f"seed-{seed}-fold-{fold:02d}",
                "seed": seed, "fold": fold,
                "sample_ids": _ids(f"imbalanced-{fold}", 6),
                "probabilities": [[1.0, 0.0, 0.0] for _ in range(6)],
            })
    result = QM.compute_qualification_metrics(truth, predictions)
    detail = result["folds"][0]["metrics"]
    assert detail["accuracy"] == pytest.approx(2 / 3)
    assert detail["balanced_accuracy"] == pytest.approx(1 / 3)
    assert detail["macro_f1"] == pytest.approx(4 / 15)


@pytest.mark.parametrize("mutator", [
    lambda truth: truth.update(extra="forbidden"),
    lambda truth: truth.__setitem__("version", True),
    lambda truth: truth.__setitem__("version", 2),
    lambda truth: truth.__setitem__("task", "t1"),
    lambda truth: truth.__setitem__("classes", True),
    lambda truth: truth.__setitem__("classes", 3),
    lambda truth: truth.__setitem__("units", []),
    lambda truth: truth["units"].append(copy.deepcopy(truth["units"][0])),
    lambda truth: truth["units"][0].update(extra="forbidden"),
    lambda truth: truth["units"][0].__setitem__("unit_id", "DREAMER"),
    lambda truth: truth["units"][0].__setitem__("labels", []),
    lambda truth: truth["units"][0].__setitem__("labels", [0, 0, 0, 0]),
    lambda truth: truth["units"][0]["labels"].__setitem__(0, True),
    lambda truth: truth["units"][0]["labels"].__setitem__(0, 2),
    lambda truth: truth["units"][0].__setitem__("groups", (10, 10, 20, 20)),
    lambda truth: truth["units"][0]["groups"].pop(),
    lambda truth: truth["units"][0]["groups"].__setitem__(0, False),
])
def test_t1_rejects_noncanonical_truth(mutator):
    truth = _t1_truth()
    mutator(truth)
    _assert_rejected(truth, _t1_predictions())


@pytest.mark.parametrize("mutator", [
    lambda predictions: predictions.append(copy.deepcopy(predictions[0])),
    lambda predictions: predictions[0].update(extra="forbidden"),
    lambda predictions: predictions[0].pop("fold"),
    lambda predictions: predictions[0].__setitem__("version", True),
    lambda predictions: predictions[0].__setitem__("version", 2),
    lambda predictions: predictions[0].__setitem__("unit_id", "other"),
    lambda predictions: predictions[0].__setitem__("seed", 0),
    lambda predictions: predictions[0].__setitem__("fold", 1),
    lambda predictions: predictions[0].__setitem__("probabilities", tuple(
        tuple(row) for row in predictions[0]["probabilities"])),
    lambda predictions: predictions[0]["probabilities"].__setitem__(0, (0.9, 0.1)),
    lambda predictions: predictions[0]["probabilities"].pop(),
    lambda predictions: predictions[0]["probabilities"][0].append(0.0),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, True),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, float("nan")),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, float("inf")),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, -0.1),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, 1.0000005),
    lambda predictions: predictions[0]["probabilities"][0].__setitem__(0, 0.5),
])
def test_t1_rejects_noncanonical_or_unsafe_predictions(mutator):
    predictions = _t1_predictions()
    mutator(predictions)
    _assert_rejected(_t1_truth(), predictions)


def test_probability_sum_tolerance_is_inclusive_and_never_softmaxes_bad_rows():
    accepted = _t1_predictions([
        [0.5, 0.5000005], [0.2, 0.8], [0.6, 0.4], [0.3, 0.7]])
    QM.compute_qualification_metrics(_t1_truth(), accepted)

    rejected = _t1_predictions([
        [0.5, 0.500002], [0.2, 0.8], [0.6, 0.4], [0.3, 0.7]])
    _assert_rejected(_t1_truth(), rejected)


def test_integer_probabilities_are_rejected_by_the_strict_float_contract():
    _assert_rejected(
        _t1_truth(labels=[0, 1], groups=[1, 1]),
        _t1_predictions([[1, 0], [0, 1]]))


@pytest.mark.parametrize("mutator", [
    lambda truth: truth.update(extra="forbidden"),
    lambda truth: truth.__setitem__("classes", 2),
    lambda truth: truth["folds"].pop(),
    lambda truth: truth["folds"].__setitem__(
        -1, copy.deepcopy(truth["folds"][0])),
    lambda truth: truth["folds"][0].update(extra="forbidden"),
    lambda truth: truth["folds"][0].__setitem__("fold", 1.0),
    lambda truth: truth["folds"][0].__setitem__("labels", [0, 1]),
    lambda truth: truth["folds"][0]["labels"].__setitem__(0, True),
    lambda truth: truth["folds"][0]["labels"].__setitem__(0, 3),
])
def test_t2_rejects_noncanonical_truth(mutator):
    truth = _t2_truth()
    predictions = _t2_predictions(truth)
    mutator(truth)
    _assert_rejected(truth, predictions)


@pytest.mark.parametrize("mutator", [
    lambda predictions: predictions.pop(),
    lambda predictions: predictions.append(copy.deepcopy(predictions[0])),
    lambda predictions: predictions.__setitem__(-1, copy.deepcopy(predictions[0])),
    lambda predictions: predictions[0].update(metric_value=1.0),
    lambda predictions: predictions[0].__setitem__("version", 1.0),
    lambda predictions: predictions[0].__setitem__("unit_id", 1),
    lambda predictions: predictions[0].__setitem__("seed", True),
    lambda predictions: predictions[0].__setitem__("seed", None),
    lambda predictions: predictions[0].__setitem__("seed", -1),
    lambda predictions: predictions[0].__setitem__("fold", False),
    lambda predictions: predictions[0].__setitem__("fold", 0),
    lambda predictions: predictions[0]["probabilities"].pop(),
])
def test_t2_rejects_missing_duplicate_extra_or_bad_prediction_cells(mutator):
    truth = _t2_truth()
    predictions = _t2_predictions(truth)
    mutator(predictions)
    _assert_rejected(truth, predictions)


def test_t2_rejects_not_exactly_three_seed_by_fifteen_fold_cross_product():
    truth = _t2_truth()

    two_seed_values = _t2_predictions(truth)
    for prediction in two_seed_values:
        if prediction["seed"] == 42:
            prediction["seed"] = 7
    _assert_rejected(truth, two_seed_values)

    four_seed_values = _t2_predictions(truth)
    four_seed_values[0]["seed"] = 99
    _assert_rejected(truth, four_seed_values)


def test_t2_prediction_input_order_does_not_change_output():
    truth = _t2_truth()
    predictions = _t2_predictions(truth)
    first = QM.compute_qualification_metrics(truth, predictions)
    second = QM.compute_qualification_metrics(truth, list(reversed(predictions)))
    assert first == second


def test_public_sequence_contract_accepts_tuple_but_rejects_string():
    truth, predictions = _t1_truth(), _t1_predictions()
    assert QM.score_qualification(truth, tuple(predictions)) == QM.score_qualification(
        truth, predictions)
    _assert_rejected(truth, "not-a-prediction-sequence")
