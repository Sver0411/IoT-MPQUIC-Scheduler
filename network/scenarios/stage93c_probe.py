"""Single-datagram, dual-path probe for a live XQUIC connection."""

import argparse
import hashlib
import json
import os
import select
import socket
import struct
import time
from pathlib import Path

PORT = 49792
PACKET = struct.Struct("!4sBBHQ16sQ8x")
TIMESTAMPNS = 35
TIMESPEC = struct.Struct("@ll")
SIDES = ("client", "server")
LINKS = ("a", "b")


def identity(case):
    return {key: case[key] for key in ("name", "connection_id", "environment", "seed")}


def token_for(case):
    return hashlib.sha256(json.dumps(identity(case), sort_keys=True).encode()).digest()[:16]


def packet(side, link, seq, token, sent_ns):
    return PACKET.pack(b"I93C", SIDES.index(side), LINKS.index(link), 0, seq, token, sent_ns)


def decode(payload):
    magic, side, link, reserved, seq, token, sent_ns = PACKET.unpack(payload)
    if magic != b"I93C" or side >= 2 or link >= 2 or reserved or sent_ns <= 0 or payload[-8:] != bytes(8):
        raise ValueError("invalid probe payload")
    return SIDES[side], LINKS[link], seq, token, sent_ns


def clock_sample():
    candidates = []
    for _ in range(12):
        before = time.monotonic_ns()
        real = time.time_ns()
        after = time.monotonic_ns()
        candidates.append((before, real, after))
    before, real, after = min(candidates, key=lambda row: row[2] - row[0])
    return {"monotonic_before_ns": before, "realtime_ns": real, "monotonic_after_ns": after,
            "offset_ns": real - (before + after) // 2, "mapping_uncertainty_ns": (after - before + 1) // 2}


def probe(side, folder, period_us, marker):
    case = json.loads((folder / "case.json").read_text())
    token = token_for(case)
    own, peer = (1, 2) if side == "client" else (2, 1)
    sockets = {}
    evidence = {"identity": identity(case), "side": side, "payload_bytes": PACKET.size,
                "period_us": period_us, "clock": [clock_sample()], "tx": [], "rx": [], "error": None}
    try:
        for i, link in enumerate(LINKS, 1):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sockets[sock] = link
            sock.setsockopt(socket.SOL_SOCKET, TIMESTAMPNS, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            sock.bind((f"10.47.{i}.{own}", PORT))
            sock.connect((f"10.47.{i}.{peer}", PORT))
        if marker is not None:
            before = time.monotonic_ns()
            os.write(marker, f"stage93c {token.hex()} {side}\n".encode())
            evidence["marker_bracket_ns"] = [before, time.monotonic_ns()]
            os.close(marker)
        (folder / f"probe-{side}.ready").touch()
        gate = folder / "probe-gate.json"
        timeout = time.monotonic() + 8
        while not gate.exists():
            if time.monotonic() >= timeout:
                raise TimeoutError("probe has no verified iot_start")
            time.sleep(.001)
        start = json.loads(gate.read_text())
        if start["identity"] != evidence["identity"]:
            raise ValueError("probe gate identity mismatch")
        origin = start["origin_us"] * 1000
        evidence["origin_us"] = start["origin_us"]
        evidence["started_ns"] = time.monotonic_ns()
        evidence["clock"].append(clock_sample())
        interval = period_us * 1000
        end = origin + case["horizon_us"] * 1000
        tick, seq = evidence["started_ns"], 0
        next_clock = tick + 100_000_000
        while (now := time.monotonic_ns()) < end + 100_000_000:
            if now >= tick and now < end:
                for sock, link in sockets.items():
                    before = time.monotonic_ns()
                    sent = sock.send(packet(side, link, seq, token, before))
                    after = time.monotonic_ns()
                    if sent != PACKET.size:
                        raise RuntimeError("short UDP send")
                    evidence["tx"].append((link, seq, before, after))
                seq += 1
                # Skip missed ticks instead of sending bursts that hide spacing gaps.
                tick += max(1, (after - tick) // interval + 1) * interval
            if now >= next_clock:
                evidence["clock"].append(clock_sample())
                next_clock = now + 100_000_000
            timeout = min(.001, max(0, tick - time.monotonic_ns()) / 1e9) if tick < end else .001
            for sock in select.select(sockets, [], [], timeout)[0]:
                # Drain only until the next send is due, even if the peer's queue stays readable.
                while tick >= end or time.monotonic_ns() < tick:
                    try:
                        payload, ancillary, flags, _ = sock.recvmsg(128, 64, socket.MSG_DONTWAIT)
                    except BlockingIOError:
                        break
                    seen = time.monotonic_ns()
                    remote, link, number, run, sent_ns = decode(payload)
                    stamps = [data for level, kind, data in ancillary
                              if level == socket.SOL_SOCKET and kind == TIMESTAMPNS]
                    if flags or len(stamps) != 1 or run != token or link != sockets[sock] or remote == side:
                        raise ValueError("invalid incoming probe or timestamp")
                    seconds, nanos = TIMESPEC.unpack(stamps[0])
                    evidence["rx"].append((link, number, sent_ns, seconds * 10**9 + nanos, seen))
    except (OSError, ValueError, RuntimeError) as error:
        evidence["error"] = str(error)
        raise
    finally:
        evidence["ended_ns"] = time.monotonic_ns()
        evidence["cpu_us"] = time.process_time_ns() // 1000
        evidence["clock"].append(clock_sample())
        (folder / f"probe-{side}.json").write_text(json.dumps(evidence, separators=(",", ":")) + "\n")
        for sock in sockets:
            sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("side", choices=SIDES)
    parser.add_argument("folder", type=Path)
    parser.add_argument("period_us", type=int)
    parser.add_argument("--marker", type=int)
    args = parser.parse_args()
    if not 0 < args.period_us <= 1000:
        parser.error("probe period must be positive and no greater than 1000 us")
    probe(args.side, args.folder, args.period_us, args.marker)
