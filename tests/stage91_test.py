"""State-machine tests and adversarial replay of the engineering evidence."""

import argparse
import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
from oracle import app_scheduler_oracle as app
from oracle.stage91_lifecycle import Paths
from verify_stage2 import read_workload
from verify_stage91 import log_rows, verify_endpoint, verify_migration, verify_run, verify_xquic_log


def path_event(kind, **fields):
    return {"kind": kind, "path": "1", **fields}


def active_path():
    paths = Paths()
    paths.observe(path_event("path_state", before="0", after="1"))
    paths.observe(path_event("path_state", before="1", after="2"))
    return paths


class PathStateTests(unittest.TestCase):
    def test_migration_commit(self):
        paths = active_path()
        for kind in ("migration_start", "migration_commit", "migration_end"):
            paths.observe(path_event(kind))
        self.assertEqual(paths.transitions["migrated"], 1)
        self.assertTrue(paths.available("1"))
        self.assertFalse(paths.migration)

    def test_migration_abort(self):
        paths = active_path()
        paths.observe(path_event("migration_start"))
        paths.observe(path_event("migration_end"))
        self.assertEqual(paths.transitions["migration_aborted"], 1)
        with self.assertRaisesRegex(ValueError, "migration without validation"):
            paths.observe(path_event("migration_commit"))

    def test_migration_without_start(self):
        with self.assertRaisesRegex(ValueError, "migration without validation"):
            active_path().observe(path_event("migration_commit"))

    def test_removed_path_cannot_recover(self):
        paths = active_path()
        paths.observe(path_event("path_state", before="2", after="3"))
        paths.observe(path_event("path_state", before="3", after="4"))
        with self.assertRaisesRegex(ValueError, "invalid transport transition"):
            paths.observe(path_event("path_state", before="4", after="2"))

    def test_cross_repair_requires_prior_eligible_state(self):
        paths = active_path()
        paths.observe({"kind": "path_state", "path": "0", "before": "0", "after": "2"})
        row = {"home": "1", "path": "0", "repair": "1", "cause": "unavailable", "home_ready": "0",
               "home_pto": "0", "home_time": "0", "slack": "1000"}
        with self.assertRaisesRegex(ValueError, "cross repair has no allowed cause"):
            paths.choose(row, "1", True)
        paths.observe(path_event("path_status", before="2", after="3"))
        self.assertEqual(paths.choose(row, "1", True), paths.epoch["0"])
        with self.assertRaisesRegex(ValueError, "cross repair has no allowed cause"):
            paths.choose({**row, "repair": "0"}, "1", True)


def index(rows, event, **fields):
    return next(i for i, row in enumerate(rows) if row["event"] == event and
                all(row.get(key) == value for key, value in fields.items()))


def renumber(rows):
    for seq, row in enumerate(rows, 1):
        row["audit_seq"] = str(seq)


class MutationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.traces = {
            (case, role): log_rows(cls.evidence / case / f"{role}.log")
            for case, role in (("attempts", "client"), ("attempts", "server"),
                               ("repair", "client"), ("lifecycle", "client"))
        }

    def rows(self, case="attempts", role="client"):
        return copy.deepcopy(self.traces[case, role])

    def test_delete_single_decision(self):
        rows = self.rows()
        rows.pop(index(rows, "iot_app_decision"))
        with self.assertRaisesRegex(ValueError, "trace sequence gap"):
            verify_endpoint(rows, "client", False)

    def test_delete_paired_blocked_logs_and_renumber(self):
        rows = self.rows()
        old = index(rows, "iot_stream_create", blocked="1")
        decision = max(i for i in range(old) if rows[i]["event"] == "iot_app_decision")
        del rows[old]
        del rows[decision]
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "create outcome lacks its decision"):
            verify_endpoint(rows, "client", False)

    def test_delete_entire_blocked_attempt_and_renumber(self):
        rows = self.rows()
        old = max(i for i, row in enumerate(rows) if row["event"] == "iot_stream_create" and row["blocked"] == "1")
        start = max(i for i in range(old) if rows[i]["event"] == "iot91_attempt")
        end = old + 1
        if rows[end]["event"] == "iot91_create":
            end += 1
        del rows[start:end]
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "missing or duplicate attempt|attempt totals"):
            verify_endpoint(rows, "client", False)

    def test_change_actual_path_id(self):
        rows = self.rows()
        frame = rows[index(rows, "iot91_xquic", kind="wire_tx")]
        frame["path"] = str(1 - int(frame["path"]))
        with self.assertRaisesRegex(ValueError, "wire send disagrees with socket path"):
            verify_endpoint(rows, "client", False)

    def test_swap_attempt_and_decision_after_renumbering(self):
        rows = self.rows()
        left, right = index(rows, "iot91_attempt"), index(rows, "iot_app_decision")
        timestamp = rows[left]["t_us"]
        rows[left], rows[right] = rows[right], rows[left]
        rows[left]["t_us"] = rows[right]["t_us"] = timestamp
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "decision outside its attempt"):
            verify_endpoint(rows, "client", False)

    def test_duplicate_decision_after_renumbering(self):
        rows = self.rows()
        i = index(rows, "iot_app_decision")
        rows.insert(i, rows[i].copy())
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "decision outside its attempt or repeated"):
            verify_endpoint(rows, "client", False)

    def test_missing_ack_log_after_renumbering(self):
        rows = self.rows()
        rows.pop(index(rows, "iot_ack"))
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "receive close disagrees with delivery|ACK missing after FIN"):
            verify_endpoint(rows, "client", False)

    def test_early_fin_before_authenticated_fin(self):
        rows = self.rows(role="server")
        fin = rows.pop(index(rows, "iot91_fin_read"))
        wire = index(rows, "iot91_xquic", kind="wire_rx", stream=fin["stream"], fin="1")
        opened = index(rows, "stream_open", direction="rx", stream_id=fin["stream"])
        if opened > wire:
            rows.insert(wire, rows.pop(opened))
            rows[wire]["t_us"] = rows[wire + 1]["t_us"]
            wire += 1
        fin["t_us"] = rows[wire]["t_us"]
        rows.insert(wire, fin)
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "completion precedes complete authenticated bytes"):
            verify_endpoint(rows, "server", False)

    def test_forged_recovery_differs_from_raw_xquic(self):
        rows = self.rows("lifecycle")
        i = index(rows, "iot91_xquic", kind="path_status", before="2", after="3")
        rows.insert(i + 1, {**rows[i], "before": "3", "after": "2"})
        renumber(rows)
        with self.assertRaisesRegex(ValueError, "XQUIC trace and endpoint telemetry differ"):
            verify_xquic_log(rows, self.evidence / "lifecycle/client.xquic.log")

    def test_cross_repair_original_packet_tampering(self):
        rows = self.rows("repair")
        i = index(rows, "iot91_xquic", kind="wire_tx", repair="1")
        rows[i]["orig_pn"] = "999999999"
        with self.assertRaisesRegex(ValueError, "repair lacks original STREAM send"):
            verify_endpoint(rows, "client", True)

    def test_queue_operation_removed_after_renumbering(self):
        rows = self.rows()
        rows.pop(index(rows, "iot_input"))
        renumber(rows)
        directory = self.evidence / "attempts"
        origin = int(rows[index(rows, "iot_start")]["origin"])
        case = json.loads((directory / "case.json").read_text())
        with self.assertRaisesRegex(ValueError, "application oracle"):
            app.verify(read_workload(directory / "workload.bin"), rows, origin, case)

    def test_migration_commit_on_wrong_socket(self):
        client, server = [verify_endpoint(log_rows(self.evidence / "migration" / f"{role}.log"), role, False)
                          for role in ("client", "server")]
        start, = client["events"]["iot_start"]
        commit = next(row for row in server["events"]["iot91_xquic"] if row["kind"] == "migration_commit")
        server["rx"][commit["rx_datagram"]]["peer_port"] = "4434"
        case = json.loads((self.evidence / "migration/case.json").read_text())
        with self.assertRaisesRegex(ValueError, "rebinding lacks candidate datagram evidence"):
            verify_migration(client, server, case["migration_at_us"], int(start["origin"]))


class ArchiveTests(unittest.TestCase):
    def test_archive_files_and_case_fingerprints(self):
        evidence = MutationTests.evidence
        expected = json.loads((evidence / "sha256.json").read_text())
        actual = {file.relative_to(evidence).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
                  for file in evidence.rglob("*") if file.is_file() and file != evidence / "sha256.json"}
        self.assertEqual(actual, expected)
        self.assertTrue(json.loads((evidence / "provenance-check.json").read_text())["source_and_binary_unchanged"])
        manifest = json.loads((evidence / "manifest.json").read_text())
        for case in manifest["cases"]:
            folder = evidence / case["name"]
            self.assertEqual(json.loads((folder / "case.json").read_text()), case)
            self.assertIn(manifest["binary_sha256"], (folder / "environment.txt").read_text())


def engineering_test(case):
    def check(self):
        summary = verify_run(MutationTests.evidence / case)
        saved = json.loads((MutationTests.evidence / case / "summary.json").read_text())
        self.assertEqual(summary, saved)
    return check


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    loader = unittest.defaultTestLoader
    suite = loader.loadTestsFromTestCase(PathStateTests)
    if args.evidence is not None:
        MutationTests.evidence = args.evidence
        suite.addTests(loader.loadTestsFromTestCase(MutationTests))
        suite.addTests(loader.loadTestsFromTestCase(ArchiveTests))
        fixture_tests = type("EngineeringTests", (unittest.TestCase,), {
            f"test_{case}": engineering_test(case)
            for case in ("attempts", "repair", "lifecycle", "timeout", "migration")
        })
        suite.addTests(loader.loadTestsFromTestCase(fixture_tests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
