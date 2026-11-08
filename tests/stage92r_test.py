"""Check timing evidence, paired cases, queue effects and data isolation."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))
from stage92_analysis import paired_overhead
from stage92r_analysis import edge_quality, effect, queue_effect, validate_labels
from run_stage92r import SEALED, design, engineering_guard, schedule
from stage92_design import load_design, validate_isolation, verify_seal
from stage92r_control import NLMSG, TCMSG, request


class TimingTests(unittest.TestCase):
    def test_packet_measurement_uses_probe_timestamps(self):
        groups = [(i*1000, 5000 if i < 6 else 22000, 100, 0) for i in range(20)]
        self.assertEqual(effect(groups, "rtt", 1, [5], [22], 6000), (5000, 6000, 10000))
        self.assertIsNone(effect(groups[:10], "rtt", 1, [5], [22], 6000))

    def test_joint_error_uses_outer_skew_bound(self):
        case = [{"planned_us": 10000, "last_old_probe_us": 7000, "first_new_probe_us": 9000},
                {"planned_us": 10000, "last_old_probe_us": 11000, "first_new_probe_us": 13000}]
        self.assertEqual([r["pass"] for r in edge_quality(case, 5000, 5000)], [False, False])
        case[0]["last_old_probe_us"] = 8000
        self.assertEqual([r["pass"] for r in edge_quality(case, 5000, 5000)], [True, True])
        case[0]["first_new_probe_us"] = None
        self.assertEqual([r["pass"] for r in edge_quality(case, 5000, 5000)], [False, False])

    def test_netlink_request_binds_root_and_sequence(self):
        packet = request(3, [22, 4, 50], 42, 17)
        self.assertEqual(NLMSG.unpack_from(packet), (len(packet), 36, 5, 17, 0))
        self.assertEqual(TCMSG.unpack_from(packet, NLMSG.size), (0, 0, 0, 3, 0, 0xffffffff, 0))


class DesignTests(unittest.TestCase):
    def test_schedule_excludes_sealed_identity_and_relabeling(self):
        sealed = json.loads(SEALED.read_text())
        verify_seal(sealed)
        self.assertEqual(len(sealed["cases"]), 48)
        pilot = schedule("pilot", design(), load_design())
        validate_isolation([*pilot, *sealed["cases"]])
        self.assertEqual(len(pilot), 128)
        self.assertEqual(len({case["seed"] for case in pilot}), 128)
        for row in pilot:
            engineering_guard(row, sealed)
        with self.assertRaisesRegex(ValueError, "closed"):
            engineering_guard(sealed["cases"][0], sealed)
        with self.assertRaisesRegex(ValueError, "identity"):
            engineering_guard({**sealed["cases"][0], "split": "train"}, sealed)

    def test_overhead_matched_and_requires_all_three_arms(self):
        base = load_design()
        cases = schedule("overhead", design(), base)[:6]
        rows = [(dict(case, workload_sha256=str(case["seed"])),
                 {"goodput_bps": 100 if case["trace_arm"] != "quiet" else 101}) for case in cases]
        result = paired_overhead(rows, base["quality"], ("off", "full", "quiet"), "trace_arm")
        self.assertEqual(result["effects_full_and_components_vs_off"]["quiet"]["goodput_delta_percent"]["n"], 2)
        self.assertEqual(paired_overhead(rows[1:], base["quality"], ("off", "full", "quiet"),
                                         "trace_arm")["incomplete_blocks"], [0])
        rows[0][0]["workload_sha256"] = "bad"
        with self.assertRaisesRegex(ValueError, "changed its inputs"):
            paired_overhead(rows, base["quality"], ("off", "full", "quiet"), "trace_arm")


class EvidenceTests(unittest.TestCase):
    def test_queue_requires_asymmetric_delay_and_recovery(self):
        burst = {"side": "client", "link": "a", "at_us": 200000, "duration_us": 85000}
        raw = {side: [] for side in ("client", "server")}
        for side, peer in (("client", "server"), ("server", "client")):
            for link in ("a", "b"):
                for t in range(0, 1100000, 5000):
                    extra = 12000 if side == "client" and link == "a" and 210000 <= t < 280000 else 0
                    for offset in range(4):
                        seq = (t // 5000)*4 + offset
                        raw[side].append(("tx", link, seq, t+offset, t+offset))
                        raw[peer].append(("rx", link, seq, t+offset, t+offset+4000+extra))
        for seq, t in enumerate(range(200000, 285000, 5000), 10000000):
            raw["client"].append(("burst_tx", "a", seq, t, t))
            raw["server"].append(("burst_rx", "a", seq, t, t+18000))
        self.assertTrue(queue_effect(raw, burst, 0)["passed"])
        raw["server"] = [(kind, link, seq, sent, sent+4000 if kind == "rx" else received)
                         for kind, link, seq, sent, received in raw["server"]]
        self.assertFalse(queue_effect(raw, burst, 0)["passed"])
        raw["server"] = [row for row in raw["server"] if row[0] != "burst_rx"]
        with self.assertRaisesRegex(ValueError, "no correlated burst"):
            queue_effect(raw, burst, 0)

    def test_label_late_and_unobserved(self):
        a = {"selected": True, "assignment_probability": .5, "effective_label": 0, "absolute_label": 1,
             "received_us": 110}
        b = {"selected": False, "assignment_probability": .5, "effective_label": None, "absolute_label": None,
             "received_us": None}
        row = {"run": "fixture", "record_id": 1, "received_us": 110, "status": "received", "decision_us": 50,
               "effective_slack_us": 40, "absolute_slack_us": 80, "candidates": [a, b]}
        validate_labels([row])
        a["effective_label"] = 1
        with self.assertRaisesRegex(ValueError, "differs from delivery"):
            validate_labels([row])
        a["effective_label"], b["effective_label"] = 0, 0
        with self.assertRaisesRegex(ValueError, "unobserved path"):
            validate_labels([row])


if __name__ == "__main__":
    unittest.main(verbosity=2)
