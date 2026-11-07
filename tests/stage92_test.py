"""Test experiment design, paired overhead, edge evidence and label coverage."""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / directory) for directory in ("evaluation", "experiments")]
from run_stage92 import assert_development
from stage92_analysis import coverage, paired_overhead, selected, short
from stage92_design import ARMS, freeze, load_design, matrix, overhead, validate_isolation, verify_seal, write_workload
from stage92_timing import infer_edge, probe_series
from verify_stage2 import read_workload
from verify_stage92 import record_status


class DesignTests(unittest.TestCase):
    def setUp(self):
        self.config = load_design()
        self.cases = matrix(self.config)

    def test_cross_product_and_partition(self):
        expected = {(load, budget, timing) for load in ("low", "high") for budget in ("regular", "short")
                    for timing in ("early", "late")}
        for environment in self.config["environments"]:
            cases = [r for r in self.cases if r["environment"] == environment["id"]]
            self.assertEqual({(r["load"], r["budget"], r["timing"]) for r in cases}, expected)
            self.assertEqual(len(cases), len(expected) * self.config["pilot_repeats"])
        for field in ("family", "seed", "network_seed", "assignment_seed"):
            changed = copy.deepcopy(self.cases)
            sealed = next(r for r in changed if r["split"] == "sealed")
            sealed[field] = changed[0][field]
            with self.assertRaisesRegex(ValueError, f"overlap: {field}"):
                validate_isolation(changed)

    def test_seal_tampering_and_runner_gate(self):
        seal = freeze(self.config, self.cases, {"binary_sha256": "fixture"})
        verify_seal(seal)
        with self.assertRaisesRegex(ValueError, "sealed test is closed"):
            assert_development(seal["cases"][0])
        seal["statistics"]["unit"] = "record"
        with self.assertRaisesRegex(ValueError, "manifest was modified"):
            verify_seal(seal)

    def test_seeded_pair_reuses_identical_payload(self):
        cases = [c for c in overhead(self.config) if c["block"] == 0]
        self.assertEqual({c["trace"] for c in cases}, ARMS.keys())
        with tempfile.TemporaryDirectory() as temporary:
            files = [Path(temporary) / c["trace"] for c in cases]
            for path, case in zip(files, cases):
                write_workload(path, case)
            self.assertEqual(len({p.read_bytes() for p in files}), 1)
            r = read_workload(files[0])
            self.assertEqual(list(r), [str(i) for i in range(1, 41)])
            self.assertLessEqual(max(row["deadline"]-row["generated"] for row in r.values()), 100_000)


class TimingTests(unittest.TestCase):
    def test_probe_evidence_differs_from_command(self):
        series = [(i * 1000, 5000 if i < 6 else 17000, 300, 0) for i in range(12)]
        self.assertEqual(infer_edge(series, "rtt", 1, 5, 17), (5000, 6000, 8000))
        self.assertIsNone(infer_edge(series[:7], "rtt", 1, 5, 17))

    def test_restore_requires_observed_outage(self):
        steady = [(i * 7000, 5000, 300, 0) for i in range(14)]
        self.assertIsNone(infer_edge(steady, "outage", 2, 5, 5))
        for i in range(4, 8):
            steady[i] = (i * 7000, None, None, 4)
        self.assertEqual(infer_edge(steady, "outage", 1, 5, 5)[1], 28_000)
        self.assertEqual(infer_edge(steady, "outage", 2, 5, 5)[1], 56_000)

    def test_probe_rejects_unmatched_packet(self):
        sent = [["tx", "a", i, i, i] for i in range(120)]
        with self.assertRaisesRegex(ValueError, "invalid probe capture"):
            probe_series(sent, [["rx", "a", 8, 7, 50]], "a")


class AnalysisTests(unittest.TestCase):
    def test_paired_effect_and_missing_block(self):
        config = load_design()
        cases = [{**r, "workload_sha256": f"block-{r['block']}"} for r in overhead(config)]
        runs = [(case, {"goodput_bps": 1000, "event_on_time_pp": 50 + (case["trace"] == "full"),
                        "client_choice_to_send_mean_us": 10 + (case["trace"] == "full") * 4})
                for case in cases if (case["block"], case["trace"]) != (1, "logs")]
        result = paired_overhead(runs, config["quality"])
        self.assertEqual(result["incomplete_blocks"], [1])
        self.assertEqual(result["effects_full_and_components_vs_off"]["full"]["client_choice_to_send_mean_us"]["mean"], 4)
        cases[0]["workload_sha256"] = "wrong"
        with self.assertRaisesRegex(ValueError, "changed its inputs"):
            paired_overhead(runs, config["quality"])

    def test_short_budget_boundaries_and_coverage(self):
        row = {"split": "train", "environment": "fixture", "load": "low", "budget": "short", "timing": "early",
               "status": "received", "seed": 1, "blocked_seen": False, "no_candidate_seen": False, "timeout": False,
               "kind": 2, "event_on_time": 1, "near_planned_edge": False,
               "candidates": [{"selected": True, "link": "A", "effective_label": 1},
                              {"selected": False, "link": "B", "effective_label": None}]}
        rows = [{**row, "effective_slack_us": t} for t in (-1, 0, 1, 100_000, 100_001)]
        self.assertEqual([short(r) for r in rows], [False, False, True, True, False])
        result = next(r for r in coverage(rows) if r["cell"] == "all")
        self.assertEqual((result["short_dual_n"], result["short_positive"], result["event_generated"]), (2, 2, 5))
        self.assertEqual(selected(rows[2])["link"], "A")

    def test_missing_fin_and_unsubmitted_labels(self):
        self.assertEqual(record_status("1", {"1"}, set(), False, set(), set(), set(), set()), "submitted-no-fin")
        self.assertEqual(record_status("1", set(), set(), False, {"1"}, set(), set(), set()), "stream-create-blocked")
        self.assertEqual(record_status("1", {"1"}, {"1"}, True, set(), set(), set(), set()), "after-window")


if __name__ == "__main__":
    unittest.main(verbosity=2)
