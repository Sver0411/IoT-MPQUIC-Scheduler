"""Apply prebuilt netem requests on persistent sockets in each namespace."""

import json
import socket
import struct
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from stage5 import clock_us

NLMSG = struct.Struct("=IHHII")
TCMSG = struct.Struct("=BBHiIII")
RTM_NEWQDISC, NLMSG_ERROR = 36, 2
NLM_F_REQUEST_ACK, TC_H_ROOT = 5, 0xffffffff
TCA_KIND, TCA_OPTIONS = 1, 2
NETEM_RATE, NETEM_LATENCY64, NETEM_JITTER64, NETEM_PRNG_SEED = 6, 10, 11, 14


def attr(kind, value):
    size = 4 + len(value)
    return struct.pack("=HH", size, kind) + value + bytes(-size % 4)


def request(ifindex, profile, seed, seq):
    delay, rate, loss, *jitter = profile
    if delay < 0 or rate <= 0 or not 0 <= loss <= 100 or len(jitter) > 1:
        raise ValueError("invalid netem profile")
    # A zero handle changes the existing root netem. Delay and jitter use nanoseconds.
    options = struct.pack("=6I", 0, 1000, round(loss * 0xffffffff / 100), 0, 0, 0)
    options += attr(NETEM_RATE, struct.pack("=IiIi", int(rate * 125_000), 0, 0, 0))
    options += attr(NETEM_LATENCY64, struct.pack("=q", int(delay * 1_000_000)))
    options += attr(NETEM_JITTER64, struct.pack("=q", int(jitter[0] * 1_000_000) if jitter else 0))
    if seed is not None:
        options += attr(NETEM_PRNG_SEED, struct.pack("=Q", seed))
    body = TCMSG.pack(0, 0, 0, ifindex, 0, TC_H_ROOT, 0)
    body += attr(TCA_KIND, b"netem\0") + attr(TCA_OPTIONS, options)
    return NLMSG.pack(NLMSG.size + len(body), RTM_NEWQDISC, NLM_F_REQUEST_ACK, seq, 0) + body


def wait_until(target):
    delay = target - clock_us() - 220
    if delay > 0:
        time.sleep(delay / 1e6)
    while clock_us() < target:
        pass


def worker(link, tc):
    interface = f"iot-{link}"
    jobs = json.load(sys.stdin)
    index = socket.if_nametoindex(interface)
    prepared = [request(index, job["profile"], job["seed"], seq) for seq, job in enumerate(jobs, 1)]
    with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE) as sock:
        sock.bind((0, 0))
        sock.settimeout(2)
        for seq, (job, message) in enumerate(zip(jobs, prepared), 1):
            wait_until(job["planned_us"])
            started = clock_us()
            sock.sendto(message, (0, 0))
            submitted = clock_us()
            ack = sock.recv(4096)
            done = clock_us()
            _, kind, _, actual_seq, _ = NLMSG.unpack_from(ack)
            error, = struct.unpack_from("=i", ack, NLMSG.size)
            if kind != NLMSG_ERROR or actual_seq != seq or error:
                raise RuntimeError(f"invalid netlink ACK for {interface}: {kind}/{actual_seq}/{error}")
            snapshot = subprocess.check_output([tc, "qdisc", "show", "dev", interface], text=True).strip()
            print(json.dumps({**job, "start_us": started, "submitted_us": submitted, "done_us": done,
                              "snapshot_us": clock_us(), "qdisc": snapshot}), flush=True)


def transitions(namespaces, phases, origin, seed, tc):
    jobs = {}
    for i, phase in enumerate(phases):
        for side, namespace in zip(("client", "server"), namespaces):
            if phase.get("side", "both") not in ("both", side):
                continue
            for link, profile in phase["links"].items():
                direction = f"{side}-{link}"
                direction_seed = seed + 4 * i + (side == "server") + 2 * (link == "b")
                jobs.setdefault((namespace, link, direction), []).append({
                    "index": i, "planned_us": origin + phase["at_us"], "profile": profile,
                    "seed": direction_seed if 0 < profile[2] < 100 or len(profile) > 3 else None})
    children = {}
    try:
        for (namespace, link, direction), schedule in jobs.items():
            child = subprocess.Popen(["ip", "netns", "exec", namespace, "python3", "-B", __file__, link, tc],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            children[direction] = child
            child.stdin.write(json.dumps(schedule))
            child.stdin.close()
            child.stdin = None
        rows = [{} for phase in phases]
        for direction, child in children.items():
            output, _ = child.communicate(timeout=8)
            if child.returncode:
                raise RuntimeError(f"netlink worker failed: {direction} ({child.returncode})")
            for line in output.splitlines():
                edge = json.loads(line)
                rows[edge.pop("index")][direction] = edge
        for phase, directions in zip(phases, rows):
            if not directions:
                raise ValueError("missing qdisc acknowledgements")
            yield {"offset_us": phase["at_us"], "links": phase["links"], "side": phase.get("side", "both"),
                   "begin_us": min(r["start_us"] for r in directions.values()),
                   "end_us": max(r["snapshot_us"] for r in directions.values()), "directions": directions,
                   "qdiscs": {key: row["qdisc"] for key, row in directions.items()}}
    finally:
        for child in children.values():
            if child.poll() is None:
                child.kill()
                child.wait()


def live(case, root, namespaces, origin, tc):
    from stage92_probe import start_probes

    if case["split"] not in ("engineering", "train", "validation"):
        raise ValueError("sealed test is closed")
    with ExitStack() as stack:
        children = []
        if "queue_burst" in case:
            log = stack.enter_context((root / "probes.stderr").open("w"))
            children = start_probes(stack, root, namespaces, origin,
                                    {"interval": 5000, "size": 72, "horizon": case["horizon_us"],
                                     "burst": case["queue_burst"]}, log)
        with (root / "network.jsonl").open("a") as file:
            for row in transitions(namespaces, case["phases"], origin, case["network_seed"], tc):
                file.write(json.dumps(row) + "\n")
                file.flush()
        for child in children:
            if child.wait(timeout=8):
                raise RuntimeError("queue probe failed")


if __name__ == "__main__":
    worker(*sys.argv[1:])
