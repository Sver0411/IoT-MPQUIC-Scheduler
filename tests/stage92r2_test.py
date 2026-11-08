"""Tests for fixed sampling, timing evidence and independent coverage accounting."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
from stage92r2_analysis import SEALED, configured_state, coverage_table, design, engineering_guard, load_design, schedule
from stage92r2_timing import loss_evidence, paired_probes, rate_windows, stable_edge
from stage92r2_kernel import identify_first_drop, trace_drops


class DesignTests(unittest.TestCase):
    def test_fixed_blocks_and_only_remaining_timing_scenarios(self):
        config, base = design(), load_design()
        cases = schedule("overhead", config, base)
        self.assertEqual(len(cases), 144)
        for start in range(0, len(cases), 3):
            group = cases[start:start+3]
            self.assertEqual({r["trace_arm"] for r in group}, {"off", "full", "quiet"})
            self.assertEqual(len({(r["seed"], r["assignment_seed"], r["load"], r["path_scheduler"]) for r in group}), 1)
        timing = schedule("timing", config, base)
        self.assertEqual(len(timing), 30)
        self.assertEqual({r["scenario"] for r in timing}, {"loss", "bandwidth", "reversal"})

    def test_development_schedule_excludes_relabelled_seal(self):
        sealed = json.loads(SEALED.read_text())
        cases = schedule("pilot", design(), load_design())
        self.assertEqual(len(cases), len({r["seed"] for r in cases}))
        for case in cases:
            engineering_guard(case, sealed)
        with self.assertRaisesRegex(ValueError, "identity"):
            engineering_guard({**sealed["cases"][0], "split": "train"}, sealed)


class TimingTests(unittest.TestCase):
    def test_stable_edge_preserves_gap_and_rejects_second_old_state(self):
        baseline = [(t, -9000) for t in range(1000, 5000, 1000)]
        change = [(5000, None)] + [(t, 9000) for t in range(6000, 15000, 1000)]
        old = lambda value: value is not None and value < -7000
        new = lambda value: value is not None and value > 7000
        self.assertEqual(stable_edge(baseline+change, 5000, old, new), (4000, 6000, 10000))
        self.assertIsNone(stable_edge(baseline+change+[(15000, -9000)], 5000, old, new))

    def test_ranking_requires_paired_path_samples(self):
        tx = [("tx", "a", 0, 1000, 1000), ("tx", "b", 0, 1050, 1050), ("tx", "a", 4, 2000, 2000)]
        rx = [("rx", "a", 0, 1000, 19000), ("rx", "b", 0, 1050, 6050), ("rx", "a", 4, 2000, 20000)]
        self.assertEqual(paired_probes(tx, rx, 1000), [(1050, 13000), (2000, None)])

    def test_bandwidth_requires_sustained_receives_and_queue_growth(self):
        tx, rx = [], []
        for start, spacing in ((155000, 270), (615000, 1684)):
            for i in range(150):
                tx.append(("burst_tx", "a", start+i, start+i*100, start+i*100))
                rx.append(("burst_rx", "a", start+i, start+i*100, start+5000+i*spacing))
        self.assertTrue(all(r["pass"] for r in rate_windows(tx, rx, 0)))
        rx.pop()
        self.assertFalse(rate_windows(tx, rx, 0)[1]["pass"])

    def test_loss_ack_and_first_missing_probe_are_separate(self):
        planned, ack = 400000, 400100
        tx = [("tx", "a", i, t, t) for i, t in enumerate(range(325000, 475000, 250))]
        rx = [("rx", link, seq, t, t+5000) for _, link, seq, t, _ in tx if t < 401000 or seq % 2]
        evidence = loss_evidence(tx, rx, "a", planned, ack)
        self.assertTrue(evidence["rate_verified"])
        self.assertGreater(evidence["first_missing_probe_send_us"], ack)

    def test_loss_witness_requires_matching_socket_send_and_kernel_drop(self):
        text = ("# entries-in-buffer/entries-written: 2/2   #P:4\n"
                "python3-123 [000] b.... 0.400102: net_dev_queue: dev=iot-a skbaddr=hash len=114\n"
                "python3-123 [000] ..... 0.400104: kfree_skb: skbaddr=hash reason: QDISC_DROP\n")
        sent = [("tx", "a", 77, 400100, 400110)]
        with tempfile.TemporaryDirectory() as temporary:
            file = Path(temporary) / "kernel.log"
            file.write_text(text)
            drops = trace_drops(file, 123)
            self.assertEqual(identify_first_drop(drops, sent, [], 400000, 400100)["probe_seq"], 77)
            self.assertIsNone(identify_first_drop(drops, sent, [("rx", "a", 77, 400100, 405000)], 400000, 400100))
            self.assertIsNone(identify_first_drop(drops, sent, [], 400000, 400101))
            self.assertIsNone(identify_first_drop(drops, sent, [], 400105, 400100))
            file.write_text(text.replace("2/2", "2/3"))
            with self.assertRaisesRegex(ValueError, "truncated"):
                trace_drops(file, 123)


class CoverageTests(unittest.TestCase):
    def test_coverage_counts_only_observed_selected_path(self):
        selected = {"selected": True, "link": "A", "effective_label": 1, "current_probability": .5}
        other = {"selected": False, "link": "B", "effective_label": None}
        row = {"environment": "fixture", "split": "train", "effective_slack_us": 100000,
               "candidates": [selected, other], "seed": 1, "near_planned_edge": True,
               "label_state": "on-time", "configured_network_state": "initial", "status": "received"}
        unknown = {**row, "seed": 2, "label_state": "unknown",
                   "candidates": [{**selected, "effective_label": None}, other]}
        result, = coverage_table([row, unknown], design())
        self.assertEqual((result["short_dual_n"], result["short_positive"]), (1, 1))
        self.assertEqual(result["short_path"], {"A": 1, "B": 0})
        self.assertEqual(result["label_status"]["unknown"], 1)
        self.assertFalse(result["pass"])

    def test_state_uses_observed_configuration_window(self):
        phase = {"directions": {"client-a": {"start_us": 109000, "done_us": 111000}}}
        case = {"phases": [{"links": {"a": [4, 5, 100]}}]}
        states = [configured_state(t, [phase], 100000, case) for t in (None, 3999, 4000, 16000, 16001)]
        self.assertEqual(states, ["unsubmitted", "initial", "cutover-uncertain", "cutover-uncertain", "outage"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
