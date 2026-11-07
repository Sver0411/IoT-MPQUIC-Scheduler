"""Apply and record timed netem changes in both isolated namespaces."""

import json
import re
import subprocess
import sys
import time
from pathlib import Path


def clock_us():
    return time.monotonic_ns() // 1000


def configure(namespaces, links, offset, seed, scope="both"):
    begin = clock_us()
    snapshots = {}
    for side, namespace in zip(("client", "server"), namespaces):
        if scope != "both" and scope != side:
            continue
        for link, (delay, rate, loss) in links.items():
            require_link = link in ("a", "b") and 0 <= loss <= 100 and delay >= 0 and rate > 0
            if not require_link:
                raise ValueError("invalid netem profile")
            base = ["ip", "netns", "exec", namespace, "tc", "qdisc"]
            interface = f"iot-{link}"
            command = base + ["replace", "dev", interface, "root", "netem", "limit", "1000",
                              "delay", f"{delay}ms", "rate", f"{rate}mbit"]
            if loss:
                command += ["loss", "random", f"{loss}%"]
            if 0 < loss < 100 and seed is not None:
                command += ["seed", str(seed + (side == "server"))]
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
    path = root / "network.jsonl"
    if mode == "initial":
        row = configure(namespaces, case["initial"], None, None)
        with path.open("x") as output:
            output.write(json.dumps(row) + "\n")
        return

    start = re.search(r"event=iot_start origin=(\d+) ", (root / "client.log").read_text())
    if start is None:
        raise ValueError("client replay origin missing")
    origin = int(start[1])
    netem_help = subprocess.run(["tc", "qdisc", "add", "netem", "help"],
                                capture_output=True, text=True, check=False)
    seed = case["seed"] if re.search(r"\bseed\b", netem_help.stderr + netem_help.stdout) else None
    with path.open("a") as output:
        for phase in case["phases"]:
            offset = phase["at_us"]
            delay = (origin + offset - clock_us()) / 1_000_000
            if delay > 0:
                time.sleep(delay)
            row = configure(namespaces, phase["links"], offset, seed, phase.get("side", "both"))
            row["random_loss_seed"] = seed if any(0 < p[2] < 100 for p in phase["links"].values()) else None
            output.write(json.dumps(row) + "\n")
            output.flush()


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] not in {"initial", "timeline"}:
        sys.exit("Usage: stage5.py {initial|timeline} RESULTS_DIR CLIENT_NS SERVER_NS")
    run(sys.argv[1], Path(sys.argv[2]), sys.argv[3:])
