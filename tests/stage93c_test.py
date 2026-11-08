"""Exercise Stage 9.3-C joins and decision rules without running any network plans."""

import copy
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evaluation"))

from stage93c import (limits, make_schedule, outage_edges, overhead, pair_plan, paired_interval,
                      phase_at, reversal_edges, state_change, validate_frozen)
from stage93c_evidence import clock_mapping, join_probes, kernel_drops, max_gap_ns, read_capture, to_monotonic
from stage93c_probe import LINKS, SIDES, identity, packet, token_for
from stage92_design import freeze, load_design, matrix
from run_stage93c import run_case

CASE = {"name": "c93-test", "connection_id": "c93-test-connection", "environment": "engineering/test", "seed": 1}
TOKEN = token_for(CASE)
ORIGIN = 1_000_000
OFFSET = 2_000_000_000


def anchor(mono, offset=OFFSET):
    return {"monotonic_before_ns": mono - 10, "realtime_ns": mono + offset, "monotonic_after_ns": mono + 10,
            "offset_ns": offset, "mapping_uncertainty_ns": 10}


def probes():
    raw, stamps = {}, {}
    for side in SIDES:
        raw[side] = {"identity": identity(CASE), "side": side, "origin_us": ORIGIN, "error": None,
                     "started_ns": 1_001_000_000, "ended_ns": 1_010_000_000,
                     "clock": [anchor(1_000_000_000), anchor(1_011_000_000)], "tx": [], "rx": []}
        for link in LINKS:
            stamps[side, link] = {}
    for side, peer in (("client", "server"), ("server", "client")):
        for link in LINKS:
            for seq in range(2):
                sent = 1_001_000_000 + seq * 1_000_000
                rx = sent + 4_000_000
                raw[side]["tx"].append((link, seq, sent, sent + 100))
                raw[peer]["rx"].append((link, seq, sent, rx + OFFSET, rx + 500))
                stamps[side, link][side, link, seq] = (rx + OFFSET - 10, sent)
                stamps[peer, link][side, link, seq] = (rx + OFFSET + 10, sent)
    return raw, stamps


def capture_bytes(token):
    payload = packet("client", "a", 0, token, 1_001_000_000)
    ethernet = bytes(12) + b"\x08\x00"
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + 8 + len(payload), 0, 0, 64, 17, 0,
                     bytes((10, 47, 1, 1)), bytes((10, 47, 1, 2)))
    udp = struct.pack("!4H", 49792, 49792, 8 + len(payload), 0)
    raw = ethernet + ip + udp + payload
    header = struct.pack("<IHHIIII", 0xa1b23c4d, 2, 4, 0, 0, 65535, 1)
    return header + struct.pack("<4I", 3, 123456789, len(raw), len(raw)) + raw


def sequence(delay):
    return [{"sequence": i, "tx_before_ns": (ORIGIN + i*1000)*1000, "tx_after_ns": (ORIGIN + i*1000)*1000 + 900,
             "rx_ns": (ORIGIN + i*1000 + delay(i))*1000 if delay(i) is not None else None} for i in range(50)]


def control(at, keys, links):
    return {"offset_us": at, "links": links, "directions": {
        key: {"planned_us": ORIGIN + at, "start_us": ORIGIN + at + 25,
              "submitted_us": ORIGIN + at + 30, "done_us": ORIGIN + at + 60} for key in keys}}


class MeasurementTest(unittest.TestCase):
    def test_clock_uncertainty_and_offset_envelope(self):
        mapping = clock_mapping([anchor(10_000), anchor(20_000, OFFSET + 50)])
        self.assertEqual(mapping["offset_envelope_ns"], 70)
        self.assertEqual(mapping["mapping_uncertainty_ns"], 35)
        self.assertEqual(to_monotonic(OFFSET + 10_000, mapping), 9975)
        bad = anchor(10_000)
        bad["offset_ns"] += 1
        with self.assertRaisesRegex(ValueError, "clock"):
            clock_mapping([bad, anchor(20_000)])

    def test_pcap_nanosecond_payload_and_connection_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, quic = Path(tmp) / "input.pcap", Path(tmp) / "quic.pcap"
            source.write_bytes(capture_bytes(TOKEN))
            self.assertEqual(read_capture(source, quic, "a", CASE),
                             {("client", "a", 0): (3_123_456_789, 1_001_000_000)})
            self.assertEqual(len(quic.read_bytes()), 24)
            with self.assertRaisesRegex(ValueError, "identity"):
                read_capture(source, quic, "a", {**CASE, "connection_id": "different"})

    def test_bidirectional_probe_join_and_spacing(self):
        raw, stamps = probes()
        directions, clock = join_probes(CASE, ORIGIN, raw, stamps, 100000)
        self.assertEqual(set(directions), {"client-a", "client-b", "server-a", "server-b"})
        self.assertEqual(clock["pcap_pairs"], 8)
        self.assertEqual(clock["pcap_max_error_ns"], 10)
        self.assertEqual(max_gap_ns(directions["server-b"]), 1_000_000)
        del stamps["server", "a"]["client", "a", 0]
        with self.assertRaisesRegex(ValueError, "PCAP"):
            join_probes(CASE, ORIGIN, raw, stamps, 100000)

    def test_reversal_needs_sustained_delay_and_relative_order(self):
        _, settings = limits()
        directions = {f"{side}-{link}": sequence(lambda i, link=link: (4000 if link == "a" else 16000)
                       if i < 15 else (16000 if link == "a" else 4000)) for side in SIDES for link in LINKS}
        case = {"initial": {"a": [4, 25, 0], "b": [16, 25, 0]}}
        phase = control(15000, directions, {"a": [16, 25, 0], "b": [4, 25, 0]})
        output = reversal_edges(case, [{}, phase], directions, ORIGIN, settings, 10)
        self.assertEqual(len(output["reversal"]), 4)
        self.assertEqual(len(output["relative_ranking"]), 2)
        self.assertEqual(output["reversal"]["client-a"]["first_new_sequence"], 15)
        rows = directions["client-a"]
        with self.assertRaisesRegex(ValueError, "sustained"):
            state_change(rows, ["old"] * 15 + ["new"] + ["unknown"] * 34, ORIGIN + 15000, 5)

    def test_kernel_drop_sequence_pid_marker_and_buffer(self):
        raw = {side: {"marker_bracket_ns": [1_000_100_000, 1_000_100_900]} for side in SIDES}
        meta = {"clock": "mono", "lost_events": 0, "pids": {"client": 10, "server": 20}}
        trace = "\n".join(("# entries-in-buffer/entries-written: 4/4",
                           f"p-10 [000] .... 1.000100: tracing_mark_write: stage93c {TOKEN.hex()} client",
                           f"p-20 [001] .... 1.000100: tracing_mark_write: stage93c {TOKEN.hex()} server",
                           "p-10 [000] .... 1.001000: net_dev_queue: dev=iot-b skbaddr=0x123 len=90",
                           "p-10 [000] .... 1.001001: kfree_skb: skbaddr=0x123 reason: QDISC_DROP"))
        rows = {"client-b": [dict(sequence=3, tx_before_ns=1_001_000_100, tx_after_ns=1_001_001_900, rx_ns=None)]}
        drops, markers = kernel_drops(trace, meta, raw, rows, TOKEN)
        self.assertIn(3, drops["client-b"])
        self.assertEqual(set(markers), set(SIDES))
        with self.assertRaisesRegex(ValueError, "buffer"):
            kernel_drops(trace, {**meta, "lost_events": 1}, raw, rows, TOKEN)
        rows["client-b"][0]["rx_ns"] = 1_005_000_000
        with self.assertRaisesRegex(ValueError, "absent-receive"):
            kernel_drops(trace, meta, raw, rows, TOKEN)

    def test_outage_and_recovery_need_continuity(self):
        _, settings = limits()
        directions = {f"{side}-{link}": sequence(lambda i: 4000) for side in SIDES for link in LINKS}
        direction = "server-b"
        directions[direction] = sequence(lambda i: None if 15 <= i <= 24 else 4000)
        drops = {direction: {i: {"drop_ns": (ORIGIN+i*1000)*1000+100} for i in range(15, 25)}}
        start, end = control(15000, [direction], {"b": [4, 19, 100]}), control(25000, [direction], {"b": [4, 19, 0]})
        edges = outage_edges([{}, start, end], directions, drops, ORIGIN, settings, 10)
        self.assertEqual(edges["outage"][direction]["first_new_sequence"], 15)
        self.assertEqual(edges["recovery"][direction]["first_new_sequence"], 25)
        directions[direction][26]["rx_ns"] = None
        with self.assertRaisesRegex(ValueError, "recovery"):
            outage_edges([{}, start, end], directions, drops, ORIGIN, settings, 10)

    def test_phase_unknown_on_crossing_or_missing_bounds(self):
        self.assertEqual(phase_at(10, 12, None, 25), "UNKNOWN")
        self.assertEqual(phase_at(10, 21, (20, 24, None, None), 5), "UNKNOWN")
        self.assertEqual(phase_at(26, 27, (20, 24, None, None), 5), "POST_STABLE")

    def test_frozen_identity_and_engineering_only_schedule(self):
        frozen = validate_frozen()
        quality, settings = limits()
        plan = pair_plan(quality, settings)
        self.assertEqual(len(frozen["cases"]), 784)
        self.assertEqual(plan["formal_blocks_per_cell"], 0)
        schedule = make_schedule(frozen, plan, settings)
        self.assertEqual(len(schedule), 8)
        self.assertEqual({r["split"] for r in schedule}, {"engineering"})
        self.assertFalse({r["seed"] for r in schedule} & {r["seed"] for r in frozen["cases"]})
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "plan.json"
            fake.write_text(json.dumps(frozen))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_frozen(fake)
            out = Path(tmp) / "must-not-start"
            with self.assertRaisesRegex(ValueError, "engineering"):
                run_case(out, frozen["cases"][0], None, None, None, None, None)
            self.assertFalse(out.exists())

    def test_paired_equivalence_and_input_guard(self):
        self.assertTrue(paired_interval([-1, 0, 1, 0], 5, .9)["equivalent"])
        self.assertFalse(paired_interval([-6, 6], 5, .9)["equivalent"])
        self.assertFalse(paired_interval([0], 5, .9)["equivalent"])
        quality, settings = limits()
        plan = {"formal_blocks_per_cell": 2}
        schedule = make_schedule(validate_frozen(), plan, settings)
        cases = [{**r, "workload_sha256": f"block-{r['block']}"} for r in schedule]
        metrics = {r["name"]: {"goodput_bps": 900, "event_on_time_pp": 2, "client_choice_to_send_mean_us": 5}
                   for r in cases}
        result = overhead(cases, metrics, plan, quality, settings)
        self.assertTrue(result["pass"])
        self.assertEqual(len(result["formal_comparisons"]), 6)
        mutated = copy.deepcopy(cases)
        next(r for r in mutated if r["block"] is not None)["workload_sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "unequal inputs"):
            overhead(mutated, metrics, plan, quality, settings)


if __name__ == "__main__":
    if sys.argv[1:] == ["--historical"]:
        # Run the original isolation tests with a structurally real fixture and
        # reserved synthetic identities, without opening the sealed case file.
        design = load_design()
        design["environments"] = [env for env in design["environments"] if env["split"] != "sealed"]
        for i in range(3):
            design["environments"].append({"id": f"fixture-closed-{i}", "family": f"fixture-mechanism-{i}",
                                           "split": "sealed", "initial": {"a": [31+i, 41, 0], "b": [42, 42, 0]},
                                           "after": {"a": [55+i, 41, 0]}})
        rows = matrix(design)
        for row in rows:
            if row["split"] == "sealed":
                for key in ("seed", "network_seed", "assignment_seed"):
                    row[key] += 4_000_000
        fixture = json.dumps(freeze(design, rows, {}))
        sealed_path = (ROOT / "results/stage92/sealed-manifest.json").resolve()
        original = Path.read_text

        def read(path, *args, **kwargs):
            return fixture if path.resolve() == sealed_path else original(path, *args, **kwargs)

        with patch.object(Path, "read_text", read):
            suite = unittest.defaultTestLoader.loadTestsFromNames(
                ["stage92r_test", "stage92r2_test", "stage93a_test", "stage93b_test", "stage93b2_test"])
            raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())
    else:
        unittest.main()
