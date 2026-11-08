"""Regression tests for the development-only model and its evaluation contract."""

import copy
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))

from stage93b import dataset, window_rows
from stage93b_metrics import average_precision, calibration_fit, common_rows, metric_summary, paired
from stage93b_model import FEATURES, fit_logistic, fold_assignments, predict_logistic, predictions, train_models


def config():
    result = json.loads((ROOT / "configs/stage93b.json").read_text())
    result.update(cv_folds=3, ridge_candidates=[.01, .1], validation_bootstrap_draws=400,
                  support={"per_class_rows": 1, "seed_clusters": 2})
    return result


def records(design):
    envs = [(env, split) for split in ("train", "validation") for env in design[f"{split}_environments"]]
    rows = []
    for e, (env, split) in enumerate(envs):
        for i in range(6):
            for j, slack in enumerate((9000, 21_000, 28_000)):
                y = int(j != 0)
                x = {k: float(i + 1) for ks in FEATURES.values() for k in ks}
                x.update(slack_us=slack, rtt_us=13_000 + i * 3000, current_probability=.3 + i * .05)
                rows.append({"environment": env, "split": split, "batch": ("stage92", "stage92r", "stage92r2")[i // 2],
                             "seed": 100 * e + i, "connection_id": f"{env}/{i}", "record_id": j, "label": y,
                             "effective_slack_us": slack, "candidates": [
                                 dict(selected=True, effective_label=y, features=x, estimate_us=17_000 + 500 * i),
                                 dict(selected=False, effective_label=None, absolute_label=None, received_us=None)]})
    return rows


class OfflineModelTest(unittest.TestCase):
    def test_monotone_slack_constraint_binds_on_adverse_data(self):
        x = np.array([[1], [2], [3], [4]], dtype=float)
        fitted = fit_logistic(x, np.array([1, 1, 0, 0]), .01, [0])
        self.assertEqual(fitted["raw"][1], 0)
        self.assertTrue(np.all(np.diff(predict_logistic(fitted, x)) >= 0))
        self.assertLess(fit_logistic(x, np.array([1, 1, 0, 0]), .01)["raw"][1], 0)

    def test_scaler_and_raw_parameterization(self):
        x = np.array([[2, 7], [5, 7], [9, 7], [14, 7]], dtype=float)
        fit = fit_logistic(x, np.array([0, 0, 1, 1]), .01, [0])
        self.assertEqual(fit["center"], [7.5, 7])
        self.assertEqual(fit["scale"][1], 1)
        self.assertGreater(fit["raw"][1], 0)
        raw = np.array(fit["raw"])
        np.testing.assert_allclose(predict_logistic(fit, x), 1 / (1 + np.exp(-(raw[0] + x @ raw[1:]))))

    def test_historical_baselines_and_slack_diagnostics(self):
        design = config()
        data = dataset(records(design), design)
        self.assertEqual(data.x["M2"][0, 0], 9000 - 17_000)
        self.assertEqual(data.x["D1"][0, 0], 9000 - 13_000)
        raw = predictions(data, {"M1": {"fit": None}})
        np.testing.assert_array_equal(raw["M1"], data.x["M1"][:, 0])

    def test_group_cv_is_deterministic_and_training_only(self):
        design = config()
        rows = records(design)
        data = dataset(rows, design)
        folds, mapping = fold_assignments(data, design)
        self.assertTrue(all(len(set(folds[data.group == group])) == 1 for group in mapping))
        self.assertTrue((folds[data.validation] == -1).all())
        np.testing.assert_array_equal(folds, fold_assignments(data, design)[0])
        fit, cv = train_models(data, data.train, folds, design)
        for row in rows:
            if row["split"] == "validation":
                row["label"] ^= 1
                row["candidates"][0]["effective_label"] = row["label"]
        changed = dataset(rows, design)
        self.assertEqual((fit, cv), train_models(changed, changed.train, folds, design))
        with self.assertRaisesRegex(ValueError, "requires development training"):
            train_models(data, np.r_[data.train, data.validation[0]], folds, design)

    def test_rejects_sealed_split_seed_overlap_and_unobserved_labels(self):
        design = config()
        rows = records(design)
        for field, value in (("split", "sealed"), ("seed", rows[-1]["seed"])):
            changed = copy.deepcopy(rows)
            changed[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                dataset(changed, design)
        rows[0]["candidates"][1]["effective_label"] = 0
        with self.assertRaisesRegex(ValueError, "unobserved"):
            dataset(rows, design)

    def test_budget_boundaries_and_unsubmitted_records(self):
        design = config()
        rows = records(design)
        env = design["validation_environments"][0]
        edited = [r for r in rows if r["environment"] == env]
        for row, slack in zip(edited, (0, 25_000, 50_000, 100_000, 100_001)):
            row["effective_slack_us"] = row["candidates"][0]["features"]["slack_us"] = slack
        edited[-1].update(candidates=[], label=None)
        data = dataset(rows, design)
        self.assertEqual(data.population[env]["nonpositive_budget"], 1)
        self.assertEqual(data.population[env]["above_100ms"], 1)
        self.assertEqual(data.population[env]["unsubmitted"], 1)
        for lo, hi in ((0, 25_000), (25_000, 50_000), (50_000, 100_000)):
            subset = data.slack[window_rows(data, env, (lo, hi))]
            self.assertTrue((subset > lo).all())
            self.assertIn(hi, subset)

    def test_missing_features_keep_common_and_model_specific_support_separate(self):
        design = config()
        rows = records(design)
        rows[0]["candidates"][0]["features"]["srtt_rate_us_per_s"] = None
        data = dataset(rows, design)
        self.assertTrue(data.available["M3"][0])
        self.assertFalse(data.available["M7"][0])
        p = {name: np.where(mask, .4, np.nan) for name, mask in data.available.items()}
        self.assertNotIn(0, common_rows(p, FEATURES, data.train))
        self.assertIn(0, common_rows(p, ("M0", "M3"), data.train))

    def test_paired_bootstrap_weights_whole_seed_means(self):
        data = SimpleNamespace(y=np.array([1, 1, 0]), group=np.array([0, 0, 1]), seed_by_group=[10, 11],
                               connection=np.array(["a", "a", "b"]))
        p = {"M0": np.array([.5, .5, .5]), "M3": np.array([.8, .8, .8])}
        first = paired(data, p, np.arange(3), "M3", "M0", config())
        self.assertAlmostEqual(first["brier_difference"]["record_equal"], -.01)
        self.assertAlmostEqual(first["brier_difference"]["seed_equal"], .09)
        np.testing.assert_allclose(first["brier_difference"]["seed_bootstrap_ci95"], [-.21, .39])
        self.assertEqual(first["leave_one_seed_out"]["max_omitted_seed"], 10)
        p["M3"][1] = np.nan
        self.assertEqual(paired(data, p, np.arange(3), "M3", "M0", config())["n"], 2)

    def test_metrics_ties_reliability_and_sparse_suppression(self):
        p = np.array([.9, .8, .8, .1])
        y = np.array([1, 0, 1, 0])
        self.assertAlmostEqual(average_precision(p, y), 5 / 6)
        self.assertEqual(average_precision(np.full(4, .5), y), .5)
        self.assertEqual(calibration_fit(np.array([.9, .1, .9, .1]), y, True)["status"],
                         "complete or quasi separation")
        data = SimpleNamespace(y=y, group=np.array([0, 0, 1, 1]), connection=np.array(["a", "a", "b", "b"]))
        summary, calibration = metric_summary(data, p, np.arange(4), 4, config())
        self.assertIsNotNone(summary["roc_auc"])
        self.assertEqual(sum(r["n"] for r in calibration["reliability"]), 4)
        sparse, calibration = metric_summary(data, p[:1], np.array([0]), 1, config())
        self.assertIsNone(sparse["roc_auc"])
        self.assertIsNone(sparse["brier"]["seed_bootstrap_ci95"])
        self.assertIsNone(calibration["slope"])


if __name__ == "__main__":
    unittest.main()
