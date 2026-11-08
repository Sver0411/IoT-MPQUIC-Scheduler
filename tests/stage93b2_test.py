"""Check Phase B2 calibration, causal features, and development isolation."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))

from stage93b2_diagnostics import error_summary, historical_phases, missing_causes, phase_at, task_type
from stage93b2_model import BLOCKS, FULL, block_fit, block_predict, calibrate, calibrated, impute, oof, seed_weights
from stage93b2_pilot import build_plan, validate_schedule
from stage92_design import fingerprint


def config():
    return json.loads((ROOT / "configs/stage93b2.json").read_text())


def data():
    return SimpleNamespace(y=np.array([0, 1, 0, 0, 1, 1, 0, 1, 0, 1]),
                           train=np.arange(6), validation=np.arange(6, 10),
                           env=np.array(["train-a"] * 3 + ["train-b"] * 3 + ["val-a"] * 2 + ["val-b"] * 2),
                           group=np.array([0, 0, 1, 2, 2, 2, 3, 4, 5, 6]),
                           env_by_group=["train-a", "train-a", "train-b", "val-a", "val-a", "val-b", "val-b"],
                           connection=np.array(["c0", "c0", "c1", "c2", "c2", "c2", "c3", "c4", "c5", "c6"]),
                           slack=np.full(10, 12000))


class DiagnosticTest(unittest.TestCase):
    def test_seed_weights_and_validation_label_isolation(self):
        rows = data()
        p = np.array([.1, .75, .4, .85, .65, .35, np.nan, np.nan, np.nan, np.nan])
        weights = seed_weights(rows, rows.train)
        np.testing.assert_allclose([weights[:2].sum(), weights[2], weights[3:].sum()], [.25, .25, .5])
        fits = calibrate(rows, p)
        rows.y[rows.validation] ^= 1
        self.assertEqual(calibrate(rows, p), fits)
        self.assertEqual(fits["intercept_only"]["slope"], 1)
        self.assertGreaterEqual(fits["platt"]["slope"], 0)
        self.assertTrue((np.diff(calibrated(np.linspace(.01, .99, 30), fits["platt"])) >= 0).all())
        p[6] = .3
        with self.assertRaisesRegex(ValueError, "training OOF"):
            calibrate(rows, p)

    def test_oof_never_fits_the_held_out_seed_or_validation(self):
        rows = data()
        folds = np.array([0, 0, 1, 1, 1, 1, -1, -1, -1, -1])
        matrix = np.arange(len(rows.y))[:, None]
        groups = []

        def fit_predict(x, y, held):
            self.assertFalse(set(x[:, 0]) & set(held[:, 0]))
            self.assertTrue((x[:, 0] < 6).all())
            groups.append(held[:, 0].tolist())
            return np.full(len(held), .6)

        result = oof(rows, matrix, np.full(len(rows.y), True), folds, fit_predict)
        self.assertEqual(groups, [[0, 1], [2, 3, 4, 5]])
        self.assertTrue(np.isnan(result[rows.validation]).all())

    def test_imputation_and_missing_flags_use_only_fit_data(self):
        spec = BLOCKS["F_full_indicators"]
        x = np.ones((4, len(FULL)))
        x[:, 1] = [2, np.nan, 8, 20]
        x[:, 2] = np.nan
        y = np.array([0, 0, 1, 1])
        fit = block_fit(x, y, spec, .01)
        self.assertEqual(fit["medians"][1:3], [8, 0])
        held = x[:2].copy()
        held[0, 1] = 1000
        expanded = impute(held, fit["medians"], True)
        self.assertEqual(expanded[:, 1].tolist(), [1000, 8])
        self.assertEqual(expanded[:, len(FULL) + 1].tolist(), [0, 1])
        self.assertTrue(np.isfinite(block_predict(fit, held, spec)).all())

    def test_slack_interaction_preserves_monotonicity(self):
        spec = BLOCKS["E6_slack_srtt"]
        x = np.array([[1, 0, 2], [3, 0, 2], [0, 1, 8], [0, 4, 8]], dtype=float)
        fit = block_fit(x, np.array([0, 1, 1, 0]), spec, .01)
        for weight in (0, .2, .7, 1):
            slack = np.linspace(1, 6, 20)
            features = np.column_stack((slack * (1 - weight), slack * weight, np.full(len(slack), 2 + 6 * weight)))
            self.assertTrue(np.all(np.diff(block_predict(fit, features, spec)) >= 0))

    def test_verified_phase_boundaries_and_unknown(self):
        timing = (100, 115, 170, 185)
        cases = ((30, 40, "STABLE"), (75, 99, "PRE_SWITCH"), (100, 114, "TRANSITION"),
                 (115, 169, "OUTAGE"), (170, 184, "RECOVERY"), (185, 200, "POST_STABLE"),
                 (97, 101, "UNKNOWN"))
        for low, high, expected in cases:
            self.assertEqual(phase_at(low, high, timing, 25), expected)
        self.assertEqual(phase_at(100, 101, None, 25), "UNKNOWN")
        self.assertEqual(phase_at(115, 116, (100, 115, None, None), 25), "POST_STABLE")
        with self.assertRaises(ValueError):
            phase_at(70, 80, (100, 115, 170, None), 25)

    def test_control_timeline_does_not_establish_data_plane_phase(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run = root / "stage92/pilot/runs/example"
            run.mkdir(parents=True)
            (run / "network.jsonl").write_text('{"ack_us": 100}\n')
            inventory = [{"batch": "stage92", "case": {"name": "example"}, "exit_code": 0}]
            rows = [{"choice_lower_us": 100, "choice_upper_us": 101}]
            phases, meta = historical_phases(root, inventory, rows, config())
            self.assertEqual(phases, ["UNKNOWN"])
            self.assertEqual(meta["run_inventory"]["stage92"]["with_control_timeline"], 1)

    def test_missingness_uses_only_prior_snapshots(self):
        values = dict.fromkeys(FULL, 1)
        values.update(srtt_rate_us_per_s=None, rttvar_change_us=None, recent_loss_increment=None,
                      scheduled_growth_Bps=None, srtt_order_flip=None)
        first = {"connection_id": "c", "record_id": 1, "decision_attempt": 1, "candidates": [
            {"selected": True, "path_id": 0, "rtt_us": 6000, "rtt_age_us": 0, "sampled_us": 20, "features": values},
            {"selected": False, "path_id": 1, "rtt_us": 8000}]}
        second = copy.deepcopy(first)
        second.update(record_id=2, decision_attempt=2)
        second["candidates"][0]["features"].update(srtt_order_flip=0)
        causes = missing_causes([second, first])
        self.assertEqual(causes["c", 1]["recent_loss_increment"], "no_preceding_path_snapshot")
        self.assertEqual(causes["c", 2]["recent_loss_increment"], "no_distinct_sample_interval")
        self.assertEqual(causes["c", 1]["srtt_order_flip"], "no_preceding_comparable_order")

    def test_fixed_threshold_error_directions_and_task_type(self):
        out = error_summary(np.array([0, 1, 0, 1]), np.array([.5, .8, .2, .3]), .5)
        self.assertEqual((out["false_on_time"], out["false_late"]), (1, 1))
        self.assertEqual(task_type({"kind": 2, "priority": 5}, config()), "urgent_event")
        self.assertEqual(task_type({"kind": 1, "priority": 6}, config()), "state")

    def test_pilot_schedule_is_fixed_and_isolated(self):
        rows = data()
        p = {"M1": np.full(10, .6), "M3": np.full(10, .6)}
        prior = {"validation_environments": ["val-a", "val-b"]}
        settings = {**config()["pilot"], "min_scheduled_seeds_per_environment": 4}
        seal = json.loads((ROOT / "results/stage92/sealed-manifest.json").read_text())
        with tempfile.TemporaryDirectory() as temporary:
            plan = build_plan(rows, p, prior, settings, [], seal, Path(temporary))
            self.assertEqual(plan["schedule_sha256"], fingerprint(plan["cases"]))
            self.assertEqual(len(plan["cases"]), 16)
            self.assertEqual(plan["runs_executed"], 0)
            for field in ("seed", "connection_id", "workload_sha256", "trajectory_sha256"):
                cases = copy.deepcopy(plan["cases"])
                one = next(c for c in cases if c["split"] == "train")
                other = next(c for c in cases if c["split"] == "validation")
                other[field] = one[field]
                with self.subTest(field=field), self.assertRaisesRegex(ValueError, "overlap|reused"):
                    validate_schedule(cases, [], seal)


if __name__ == "__main__":
    unittest.main()
