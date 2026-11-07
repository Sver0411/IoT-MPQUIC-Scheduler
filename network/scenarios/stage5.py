"""Apply and record timed netem changes in both isolated namespaces."""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def clock_us():
    return time.monotonic_ns() // 1000


def configure(namespaces, links, offset, seed, scope="both", seeded_tc=None):
    begin = clock_us()
    snapshots = {}
    for side, namespace in zip(("client", "server"), namespaces):
        if scope != "both" and scope != side:
            continue
        for link, (delay, rate, loss) in links.items():
            require_link = link in ("a", "b") and 0 <= loss <= 100 and delay >= 0 and rate > 0
            if not require_link:
                raise ValueError("invalid netem profile")
            base = ["ip", "netns", "exec", namespace, seeded_tc or "tc", "qdisc"]
            interface = f"iot-{link}"
            command = base + ["replace", "dev", interface, "root", "netem", "limit", "1000",
                              "delay", f"{delay}ms", "rate", f"{rate}mbit"]
            if loss:
                command += ["loss", "random", f"{loss}%"]
            if 0 < loss < 100 and seed is not None:
                direction = (side == "server") + (2 if seeded_tc and link == "b" else 0)
                command += ["seed", str(seed + direction)]
            subprocess.run(command, check=True)
            snapshots[f"{side}-{link}"] = subprocess.check_output(
                base + ["show", "dev", interface], text=True,
            ).strip()
    row = {"offset_us": offset, "begin_us": begin, "end_us": clock_us(),
           "links": links, "qdiscs": snapshots}
    if scope != "both":
        row["side"] = scope
    return row


def run(mode, root, namespaces):
    case = json.loads((root / "case.json").read_text())
    seeded_tc = os.environ.get("IOT_NETEM_TC", "tc") if "network_seed" in case else None
    path = root / "network.jsonl"
    if mode == "initial":
        row = configure(namespaces, case["initial"], None, None, seeded_tc=seeded_tc)
        with path.open("x") as output:
            output.write(json.dumps(row) + "\n")
        return

    start = re.search(r"event=iot_start origin=(\d+) ", (root / "client.log").read_text())
    if start is None:
        raise ValueError("client replay origin missing")
    origin = int(start[1])
    netem_help = subprocess.run([seeded_tc or "tc", "qdisc", "add", "netem", "help"],
                                capture_output=True, text=True, check=False)
    seed_supported = re.search(r"\bseed\b", netem_help.stderr + netem_help.stdout)
    seed = case.get("network_seed", case["seed"]) if seed_supported else None
    with path.open("a") as output:
        for index, phase in enumerate(case["phases"]):
            offset = phase["at_us"]
            delay = (origin + offset - clock_us()) / 1_000_000
            if delay > 0:
                time.sleep(delay)
            partial_loss = any(0 < p[2] < 100 for p in phase["links"].values())
            phase_seed = seed + 4 * index if seeded_tc and seed is not None else seed
            if seeded_tc and partial_loss and phase_seed is None:
                raise ValueError("Stage 7 partial loss requires a seed-capable tc")
            row = configure(namespaces, phase["links"], offset, phase_seed,
                            phase.get("side", "both"), seeded_tc)
            row["random_loss_seed"] = phase_seed if partial_loss else None
            output.write(json.dumps(row) + "\n")
            output.flush()


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] not in {"initial", "timeline"}:
        sys.exit("Usage: stage5.py {initial|timeline} RESULTS_DIR CLIENT_NS SERVER_NS")
    run(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
