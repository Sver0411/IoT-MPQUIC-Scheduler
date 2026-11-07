"""Fixtures for independent decision replay and deliberate trace corruption."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from oracle import app_scheduler_oracle as app, path_scheduler_oracle as path
from stage8_statistics import fit_logistic, holm, model_metrics, sigmoid


def state(source, generated=0, serial=1, deadline=100_000):
    return dict(kind=1, source=source, serial=serial, generated=generated, release=generated,
                deadline=deadline, maximum_age=100_000, priority=1, severity=0)


def event():
    return dict(state(3), kind=2, priority=4, severity=4)


CASE = dict(policy="proposed-app", recovery_k=2, tuning=dict(event_burst=3, severity_weight=2, waiting_weight=1))


class OracleTest(unittest.TestCase):
    def test_application_ranking(self):
        data = {"1": state(1), "2": event(), "3": state(2), "4": state(4, deadline=10)}
        self.assertEqual(app.select(data, data, {}, 11, CASE, 0, 0)[:2], ("2", app.EVENT))
        self.assertEqual(app.select(data, data, {}, 11, CASE, 0, 3)[:2], ("1", app.NORMAL))
        self.assertEqual(app.select(data, data, {1: (0, 1)}, 11, CASE, 0, 3)[:2], ("3", app.NORMAL))
        self.assertEqual(app.select(data, data, {}, 11, CASE, 2, 0)[:2], ("4", app.RECOVERY))

    def test_replay_and_corrupt_choice(self):
        data = {"1": state(1), "2": state(1, serial=2), "3": event()}
        rows = [
            dict(event="iot_admission", elapsed="0", t_us="1000"),
            dict(event="iot_input", id="1"),
            dict(event="iot_input", id="2"),
            dict(event="iot_replace", input="2", dropped="1"),
            dict(event="iot_input", id="3"),
            dict(event="iot_app_decision", id="3", kind="2", **{"class": "3"}, score="2.9",
                 decision_us="0", t_us="1000", normal="0", events="0"),
            dict(event="iot_submit", id="3", submitted="1001"),
            dict(event="iot_app_decision", id="2", kind="1", **{"class": "1"}, score="2",
                 decision_us="0", t_us="1002", normal="0", events="1"),
            dict(event="iot_submit", id="2", submitted="1003"),
            dict(event="iot_feedback", id="2", feedback_before="-1/0", feedback_after="0/2",
                 aoi_before="-1", aoi_after="20"),
            dict(event="iot_ack", id="2", latest_generated="1000", latest_serial="2", client_received="1020"),
        ]
        self.assertEqual(app.verify(data, rows, 1000, CASE)["decisions"], 2)
        for index, key, wrong in ((3, "dropped", "2"), (5, "id", "2"),
                                  (5, "score", "2.8"), (9, "aoi_after", "21")):
            tampered = [dict(row) for row in rows]
            tampered[index][key] = wrong
            with self.assertRaises(ValueError):
                app.verify(data, tampered, 1000, CASE)

    def test_paths_and_corrupt_selection(self):
        samples = [dict(path_id=str(i), rtt_us=rtt, rttvar_us=1000, queued_bytes=0,
                        scheduled_bytes=0, in_flight_bytes=0, rate_Bps=1_000_000,
                        pto=0, recent_sent=80, recent_lost=0, observed_loss=.01, loss=.01,
                        estimate_us=rtt / 2 + 1200, probability=.99)
                   for i, rtt in enumerate((20_000, 10_000))]
        plan = dict(id="1", home="1", decision_us="0", slack_us="100000", candidates="2",
                    estimate_us=6200, probability=.99)
        record = dict(event(), payload_len=1148)
        self.assertEqual(path.forecast(samples[1], 1200, 100_000), (6200, .99, .01))
        for policy in ("fixed-minrtt", "earliest-completion", "proposed-path"):
            case = dict(path_scheduler=policy)
            path.validate(case, record, plan, samples, {0, 1}, plan)
            with self.assertRaises(ValueError):
                path.validate(case, record, dict(plan, home="0"), samples, {0, 1}, plan)
        seed = 12345
        selected = path.assignment(seed, 1, 100_000, 1148, 2)
        a = samples[selected]
        random_plan = dict(plan, home=str(selected), estimate_us=a["estimate_us"])
        path.validate(dict(path_scheduler="random-path", assignment_seed=seed), record,
                      random_plan, samples, {0, 1}, plan)
        corrupted = [dict(row) for row in samples]
        corrupted[0]["in_flight_bytes"] = 100
        with self.assertRaisesRegex(ValueError, "queue sum"):
            path.validate(dict(path_scheduler="random-path", assignment_seed=seed), record,
                          random_plan, corrupted, {0, 1}, plan)

    def test_calibration_fit_and_holm(self):
        intercept, slope = fit_logistic([-1, -1, 1, 1], [0, 0, 1, 1])
        self.assertAlmostEqual(intercept, 0)
        self.assertGreater(sigmoid(intercept + slope), .5)
        result, _ = model_metrics([(0, 0), (1, 1)])
        self.assertEqual(result["brier"], 0)
        self.assertGreater(result["log_loss"], 0)
        rows = [{"sign_flip_p": p} for p in (.025, .02, .5)]
        holm(rows)
        self.assertEqual([row["holm_p"] for row in rows], [.06, .06, .5])


if __name__ == "__main__":
    unittest.main()
