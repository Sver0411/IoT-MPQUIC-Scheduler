"""Deterministic checks for measurement, pairing and calibration."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "evaluation"), str(ROOT / "experiments")]

from run_stage7 import build_workload
from stage7 import age_fields, aoi
from stage7_statistics import paired_deltas, score
from verify_stage2 import EVENT, read_workload
from verify_stage7 import endpoint_teardown


class Stage7Test(unittest.TestCase):
    def test_age_initialization_and_update(self):
        segments, violations = aoi([(20, 10, 25), (50, 45, 25)], 100)
        self.assertEqual(segments, [(10, 40), (5, 55)])
        self.assertEqual(violations, 65)
        self.assertEqual(age_fields(segments), dict(mean_aoi_ms=.028125, peak_aoi_ms=.055,
                                                     p95_aoi_ms=.051, covered_us=80))
        self.assertEqual(aoi([], 100), ([], 100))
        self.assertIsNone(age_fields([])["mean_aoi_ms"])

    def test_factorial_contrasts(self):
        values = (0.0, 0.25, 0.25, 1.0)
        rows = [dict(suite="factorial", scenario="stable", seed=1, cell=cell,
                     event_deadline_success=value) for cell, value in zip("ABCD", values)]
        effects = {row["contrast"]: row["delta"] for row in paired_deltas(rows)}
        self.assertEqual(effects, {"B-A": .25, "D-C": .75, "C-A": .25, "D-B": .75,
                                   "D-B-C+A": .5, "D-A": 1.0})
        with self.assertRaises(ValueError):
            list(paired_deltas(rows[:-1]))

    def test_probability_endpoints(self):
        summary, bins = score([(0, 0), (1, 1), (.5, 0), (.5, 1)])
        self.assertEqual(summary["brier"], .125)
        self.assertEqual(summary["ece"], 0)
        self.assertEqual(summary["roc_auc"], .875)
        self.assertEqual([row["n"] for row in bins], [1, 2, 1])

    def test_workload_identity_and_replay(self):
        with tempfile.TemporaryDirectory() as scratch:
            a, b, c = (Path(scratch) / name for name in ("a", "b", "c"))
            build_workload(a, "event-heavy", 7101)
            build_workload(b, "event-heavy", 7101)
            build_workload(c, "event-heavy", 7102)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertNotEqual(a.read_bytes(), c.read_bytes())
            events = [r for r in read_workload(a).values() if r["kind"] == EVENT]
            self.assertEqual(len(events), len({(r["source"], r["serial"]) for r in events}))
            self.assertLess(len({r["serial"] for r in events}), len(events))

    def test_measurement_survives_only_post_window_teardown(self):
        with tempfile.TemporaryDirectory() as scratch:
            directory = Path(scratch)
            (directory / "server.stderr").touch()
            trace = {"exit": [{"ok": "0"}], "conn_closed": [{"error": "0", "t_us": "30"}],
                     "stream_closed": [{"fin": "0", "t_us": "27"}]}
            self.assertEqual(endpoint_teardown(directory, "server", trace, 25), "unfinished-streams-at-close")
            with self.assertRaises(ValueError):
                endpoint_teardown(directory, "server", trace, 28)


if __name__ == "__main__":
    unittest.main()
