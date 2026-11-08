"""Exercise chronology, censoring, selection, isolation and descriptive summaries."""

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))

from stage93a import causal_order, check_partitions, extract_pilot, observed_label, validate_labels
from stage93a_features import add_features
from stage93a_statistics import association, budget_bin, distribution, ks
from stage92_design import load_design, matrix


def decision(attempt, sample, update, rtt, flight=100):
    candidate = dict(path_id=0, link="A", sampled_us=sample, rtt_age_us=sample - update,
                     rtt_us=rtt, rttvar_us=20, cwnd_bytes=1_000, in_flight_bytes=flight, scheduled_bytes=2,
                     rate_Bps=1_000_000_000 // rtt, loss=.05, total_lost=0, retransmission_rate=0, pto=0,
                     delivery_sample_age_us=0, current_probability=.2)
    return dict(record_id=attempt, decision_attempt=attempt, decision_us=sample - 1, effective_slack_us=50_000,
                payload_bytes=240, priority=1, kind=1, candidates=[candidate])


class Stage93ATest(unittest.TestCase):
    def test_label_deadline_and_window_boundaries(self):
        self.assertEqual(observed_label(True, 70, 70, 100), (1, "on-time"))
        self.assertEqual(observed_label(True, 71, 70, 100), (0, "late"))
        self.assertEqual(observed_label(True, 110, 70, 100), (0, "late"))
        self.assertEqual(observed_label(True, 110, 115, 100), (1, "on-time"))
        self.assertEqual(observed_label(True, None, 99, 100), (0, "unfinished"))
        self.assertEqual(observed_label(True, None, 100, 100), (None, "unknown"))
        self.assertEqual(observed_label(False, None, 99, 100), (None, "unsubmitted"))
        with self.assertRaisesRegex(ValueError, "without submission"):
            observed_label(False, 70, 99, 100)

    def test_unchosen_label_and_repeated_record_rejected(self):
        chosen = {"selected": True, "assignment_probability": .5, "effective_label": 1, "absolute_label": 1,
                  "received_us": 15}
        unchosen = {"selected": False, "assignment_probability": .5, "effective_label": None,
                    "absolute_label": None, "received_us": None}
        row = dict(run="run", record_id=1, candidates=[chosen, unchosen], received_us=15, status="received",
                   decision_us=5, effective_slack_us=20, absolute_slack_us=20)
        validate_labels([row])
        with self.assertRaisesRegex(ValueError, "duplicate record"):
            validate_labels([row, row])
        unchosen["effective_label"] = 0
        with self.assertRaisesRegex(ValueError, "unobserved path"):
            validate_labels([row])

    def test_causal_snapshot_wire_and_delivery(self):
        base = [1, [2, 3], 4, 5, 6, [7, 8], 9, 10, 11, 12, 13, 14]
        causal_order(*base)
        for index, bad in ((1, [2, 8]), (4, 4), (5, [4, 7]), (8, 9), (9, 10), (10, 11), (11, 12)):
            mutated = base.copy()
            mutated[index] = bad
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, "non-causal|predates"):
                causal_order(*mutated)

    def test_history_is_causal_and_never_crosses_connections(self):
        first = decision(1, 20, 15, 400)
        second = decision(2, 30, 25, 500)
        third = decision(3, 40, 35, 700)
        add_features([third, first, second], [20, 27, 39, 40, 42])
        a, b, c = [row["candidates"][0]["features"] for row in (first, second, third)]
        self.assertIsNone(a["srtt_rate_us_per_s"])
        self.assertIsNone(b["srtt_trend_us_per_s"])
        self.assertEqual(b["srtt_rate_us_per_s"], 10_000_000)
        self.assertEqual(c["srtt_trend_us_per_s"], 15_000_000)
        self.assertEqual(c["admissions_100ms"], 3)
        other = decision(1, 40, 35, 700)
        add_features([other], [])
        self.assertIsNone(other["candidates"][0]["features"]["srtt_rate_us_per_s"])
        with self.assertRaisesRegex(ValueError, "non-causal RTT"):
            add_features([decision(1, 10, 12, 100)], [])

    def test_isolation_and_reserved_identity(self):
        seal = json.loads((ROOT / "results/stage92/sealed-manifest.json").read_text())
        cases = [c for c in matrix(load_design()) if c["split"] in ("train", "validation")]
        check_partitions(cases, seal)
        train = cases[0]
        validation = next(c for c in cases if c["split"] != train["split"])
        for field in ("seed", "assignment_seed", "network_seed"):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "cross-partition overlap"):
                check_partitions([train, {**validation, field: train[field]}], seal)
        with self.assertRaisesRegex(ValueError, "reserved"):
            check_partitions([{**train, "seed": seal["cases"][0]["seed"]}], seal)

    def test_sealed_schedule_rejected_before_extraction(self):
        seal = json.loads((ROOT / "results/stage92/sealed-manifest.json").read_text())
        train = next(c for c in matrix(load_design()) if c["split"] == "train")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "stage92/raw/pilot.tar.gz"
            archive.parent.mkdir(parents=True)
            for cases, reason in (([{"split": "sealed"}], "non-development case"),
                                  ([train], "unlisted case")):
                with self.subTest(reason=reason):
                    with tarfile.open(archive, "w:gz") as stream:
                        files = {"pilot/manifest.json": json.dumps({"schedule": cases}).encode(),
                                 "pilot/runs/unlisted/sealed-result.log": b"must not be extracted"}
                        for name, content in files.items():
                            info = tarfile.TarInfo(name)
                            info.size = len(content)
                            stream.addfile(info, io.BytesIO(content))
                    (archive.parent.parent / "sha256.json").write_text(
                        json.dumps({"raw/pilot.tar.gz": hashlib.sha256(archive.read_bytes()).hexdigest()}))
                    with self.assertRaisesRegex(ValueError, reason):
                        extract_pilot(root, "stage92", root / "output", seal)
                    self.assertFalse((root / "output").exists())

    def test_budget_boundaries_distribution_and_ties(self):
        self.assertEqual([budget_bin(n) for n in (-1, 0, 1, 25_000, 25_001, 50_000, 50_001, 100_000, 100_001)],
                         ["nonpositive", "nonpositive", "0-25ms", "0-25ms", "25-50ms", "25-50ms",
                          "50-100ms", "50-100ms", ">100ms"])
        stats = distribution([0, 1, 2, 3, 20, None])
        self.assertEqual((stats["median"], stats["missing"], stats["outlier_fraction"]), (2, 1, .2))
        self.assertIsNone(distribution([0, 0, 0, 0, 1])["outlier_fraction"])
        self.assertEqual(ks([1, 2, None], [1, 2]), 0)
        self.assertEqual(ks([1, 1], [2, 2]), 1)
        sample = [dict(label=label, link="A", seed=7, features={"slack_us": 2, "rtt_us": 1, "pto": 0})
                  for label in (0, 1)]
        result = association(sample, "pto")
        self.assertEqual((result["rank_auc"], result["matched_rank_auc"], result["sparse"]), (.5, .5, True))


if __name__ == "__main__":
    unittest.main()
