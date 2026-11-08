"""Independent probe, PCAP, and tracefs joins for Stage 9.3-C."""

import json
import re
import struct
from collections import defaultdict

from stage93c_probe import LINKS, PORT, SIDES, decode, identity, token_for


def clock_mapping(samples):
    bounds = []
    for row in samples:
        before, real, after = (row[k] for k in ("monotonic_before_ns", "realtime_ns", "monotonic_after_ns"))
        if (after < before or row["offset_ns"] != real - (before + after) // 2 or
                row["mapping_uncertainty_ns"] != (after - before + 1) // 2):
            raise ValueError("invalid clock sample")
        bounds.append((real - after, real - before))
    if len(bounds) < 2:
        raise ValueError("missing clock anchors")
    lo, hi = min(x[0] for x in bounds), max(x[1] for x in bounds)
    return {"offset_lower_ns": lo, "offset_upper_ns": hi, "offset_ns": (lo + hi) // 2,
            "offset_envelope_ns": hi - lo, "mapping_uncertainty_ns": (hi - lo + 1) // 2, "samples": len(bounds)}


def to_monotonic(realtime, mapping):
    return realtime - mapping["offset_ns"]


PCAP = {b"\xd4\xc3\xb2\xa1": ("<", 1000), b"\xa1\xb2\xc3\xd4": (">", 1000),
        b"\x4d\x3c\xb2\xa1": ("<", 1), b"\xa1\xb2\x3c\x4d": (">", 1)}


def read_capture(source, quic_output, link, case):
    """Keep only QUIC frames for the unchanged legacy verifier; return probe packet identities."""
    stamps = {}
    token = token_for(case)
    with source.open("rb") as inp, quic_output.open("wb") as out:
        header = inp.read(24)
        if len(header) != 24 or header[:4] not in PCAP:
            raise ValueError("invalid capture header")
        endian, scale = PCAP[header[:4]]
        if struct.unpack_from(endian + "I", header, 20)[0] != 1:
            raise ValueError("expected Ethernet PCAP")
        out.write(header)
        while frame := inp.read(16):
            sec, fraction, size, original = struct.unpack(endian + "4I", frame)
            raw = inp.read(size)
            if size != original or len(raw) != size or raw[12:14] != b"\x08\x00" or raw[23] != 17:
                raise ValueError("invalid UDP capture")
            udp = 14 + (raw[14] & 15) * 4
            sport, dport, udp_size = struct.unpack_from("!HHH", raw, udp)
            if (sport, dport) in ((4434, 4433), (4433, 4434)):
                out.write(frame + raw)
                continue
            if (sport, dport) != (PORT, PORT):
                raise ValueError("unexpected capture filter traffic")
            role, actual_link, sequence, run, sent = decode(raw[udp + 8:udp + udp_size])
            subnet = LINKS.index(link) + 1
            own, peer = (1, 2) if role == "client" else (2, 1)
            key = role, link, sequence
            if (actual_link != link or run != token or key in stamps or
                    raw[26:34] != bytes((10, 47, subnet, own, 10, 47, subnet, peer))):
                raise ValueError("probe PCAP identity, path or sequence mismatch")
            stamps[key] = (sec * 10**9 + fraction * scale, sent)
    return stamps


def join_probes(case, origin, raw, pcaps, max_error_ns):
    samples, directions = [], {}
    for side, log in raw.items():
        if (log["identity"] != identity(case) or log["side"] != side or log["origin_us"] != origin
                or log["error"] is not None or log["started_ns"] < origin * 1000):
            raise ValueError("probe does not belong to the XQUIC connection")
        if not (log["clock"][0]["monotonic_before_ns"] <= log["started_ns"] < log["ended_ns"]
                <= log["clock"][-1]["monotonic_after_ns"]):
            raise ValueError("clock anchors do not enclose acquisition")
        samples.extend(log["clock"])
    mapping = clock_mapping(samples)
    errors = []
    for side in SIDES:
        peer = SIDES[1 - SIDES.index(side)]
        for link in LINKS:
            key = f"{side}-{link}"
            sender, receiver = raw[side], raw[peer]
            sent, received = pcaps[side, link], pcaps[peer, link]
            arrivals = {}
            for path, seq, embedded, realtime, observed in receiver["rx"]:
                if path != link:
                    continue
                capture_key = side, link, seq
                if seq in arrivals or capture_key not in received or capture_key not in sent:
                    raise ValueError("UDP receipt lacks a unique pair of PCAP observations")
                pcap, pcap_tx = received[capture_key]
                source_pcap, source_tx = sent[capture_key]
                if embedded != pcap_tx or embedded != source_tx or source_pcap > pcap:
                    raise ValueError("probe payload timestamp differs across capture points")
                rx = to_monotonic(realtime, mapping)
                if not embedded <= rx <= observed + max_error_ns:
                    raise ValueError("mapped receive time is outside the causal bracket")
                errors.append(abs(pcap - realtime))
                arrivals[seq] = (embedded, rx)
            rows = []
            for path, seq, begin, end in sender["tx"]:
                if path != link:
                    continue
                if (seq != len(rows) or not sender["started_ns"] <= begin <= end <= sender["ended_ns"]
                        or rows and rows[-1]["tx_after_ns"] > begin):
                    raise ValueError("invalid probe sequence or send bracket")
                embedded, rx = arrivals.get(seq, (begin, None))
                if begin != embedded:
                    raise ValueError("receiver's embedded send timestamp differs")
                rows.append({"sequence": seq, "tx_before_ns": begin, "tx_after_ns": end, "rx_ns": rx})
            if not rows or any(seq >= len(rows) for seq in arrivals):
                raise ValueError("missing probe send record")
            for capture in (received, sent):
                for (role, path, seq), (_, embedded) in capture.items():
                    if role == side and (path != link or seq >= len(rows) or embedded != rows[seq]["tx_before_ns"]):
                        raise ValueError("PCAP packet has no originating send record")
            directions[key] = rows
    peak = max(errors, default=None)
    return directions, {**mapping, "pcap_pairs": len(errors), "pcap_max_error_ns": peak,
                        "clock_pass": mapping["offset_envelope_ns"] <= max_error_ns,
                        "pcap_pass": peak is not None and peak <= max_error_ns}


def max_gap_ns(rows):
    return max((b["tx_before_ns"] - a["tx_before_ns"] for a, b in zip(rows, rows[1:])), default=None)


TRACE_LINE = re.compile(r"-(\d+)\s+\[\d+\]\s+\S+\s+(\d+)\.(\d{6}):\s+(\w+):\s+(.*)")


def kernel_drops(trace, metadata, raw, directions, token):
    counts = re.search(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)", trace)
    if (not counts or counts[1] != counts[2] or metadata["clock"] != "mono" or
            metadata["lost_events"] or len(set(metadata["pids"].values())) != 2):
        raise ValueError("tracefs clock, PID or buffer integrity failed")
    pids = {int(pid): role for role, pid in metadata["pids"].items()}
    markers, queued, drops = {}, {}, defaultdict(dict)
    for line in trace.splitlines():
        row = TRACE_LINE.search(line)
        if not row or int(row[1]) not in pids:
            continue
        role, timestamp = pids[int(row[1])], (int(row[2]) * 10**6 + int(row[3])) * 1000
        kind, fields = row[4], row[5]
        if kind == "tracing_mark_write":
            lo, hi = raw[role]["marker_bracket_ns"]
            if fields != f"stage93c {token.hex()} {role}" or role in markers or not lo < timestamp + 1000 or timestamp > hi:
                raise ValueError("tracefs marker does not match userspace monotonic bracket")
            markers[role] = timestamp
        if kind not in ("net_dev_queue", "kfree_skb"):
            continue
        address = re.search(r"\bskbaddr=(\S+)", fields)[1]
        if kind == "net_dev_queue":
            device = re.search(r"\bdev=iot-([ab])\b", fields)
            if device:
                queued[address] = (role, device[1], timestamp)
        elif address in queued and "QDISC_DROP" in fields:
            sender, link, queue = queued.pop(address)
            direction = f"{role}-{link}"
            if sender != role:
                raise ValueError("kernel drop PID differs from queue PID")
            candidates = [r for r in directions[direction] if r["tx_before_ns"] < queue + 1000
                          and queue <= timestamp <= r["tx_after_ns"]]
            if len(candidates) != 1 or candidates[0]["rx_ns"] is not None:
                raise ValueError("QDISC_DROP has no unique absent-receive probe sequence")
            seq = candidates[0]["sequence"]
            if seq in drops[direction]:
                raise ValueError("duplicate kernel drop for one probe")
            drops[direction][seq] = {"queue_ns": queue, "drop_ns": timestamp}
    if set(markers) != set(SIDES):
        raise ValueError("kernel PID markers missing")
    return drops, markers
