"""Test netem PRNG by sending the same numbered UDP train with three seed pairs."""

import argparse
import hashlib
import json
import re
import subprocess
import sys
import uuid

RECEIVER = """
import json, socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('10.88.0.2', 8080))
s.settimeout(.15)
print('ready', flush=True)
received = []
end = time.monotonic() + .8
while time.monotonic() < end:
    try:
        received.append(int(s.recv(64)))
    except TimeoutError:
        pass
print(json.dumps(received))
"""
SENDER = """
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for n in range(192):
    s.sendto(str(n).encode(), ('10.88.0.2', 8080))
    time.sleep(.002)
"""


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def trial(tx, rx, interface, tc, seed):
    base = ["ip", "netns", "exec", tx, tc, "qdisc"]
    command(*base, "add", "dev", interface, "root", "netem", "loss", "random", "12%", "seed", str(seed))
    qdisc = command(*base, "show", "dev", interface)
    if not re.search(rf"\bseed {seed}\b", qdisc):
        raise ValueError(f"kernel did not confirm seed {seed}: {qdisc}")
    server = ["ip", "netns", "exec", rx, sys.executable, "-c", RECEIVER]
    with subprocess.Popen(server, stdout=subprocess.PIPE, text=True) as receiver:
        if receiver.stdout.readline().strip() != "ready":
            raise ValueError("seed probe receiver failed")
        command("ip", "netns", "exec", tx, sys.executable, "-c", SENDER)
        received = json.loads(receiver.communicate(timeout=3)[0])
    command(*base, "del", "dev", interface, "root")
    mask = sorted(set(range(192)) - set(received))
    if not 0 < len(mask) < 192:
        raise ValueError("partial loss probe did not exercise both outcomes")
    return {"seed": seed, "qdisc": qdisc, "sent": 192, "dropped_ordinals": mask,
            "mask_sha256": hashlib.sha256(bytes(mask)).hexdigest()}


def verify(tc):
    suffix = uuid.uuid4().hex[:8]
    tx, rx = f"ns-tx-{suffix}", f"ns-rx-{suffix}"
    a, b = f"a-{suffix}", f"b-{suffix}"
    command("ip", "netns", "add", tx)
    try:
        command("ip", "netns", "add", rx)
        try:
            command("ip", "-n", tx, "link", "add", a, "type", "veth", "peer", "name", b, "netns", rx)
            for namespace, link, ip in ((tx, a, "10.88.0.1/30"), (rx, b, "10.88.0.2/30")):
                command("ip", "netns", "exec", namespace, "sysctl", "-qw",
                        "net.ipv6.conf.all.disable_ipv6=1")
                command("ip", "-n", namespace, "addr", "add", ip, "dev", link)
                command("ip", "-n", namespace, "link", "set", link, "up")
            command("ip", "netns", "exec", tx, "ping", "-c", "1", "-W", "1", "10.88.0.2")
            trials = [trial(tx, rx, a, tc, seed) for seed in (71001, 71002, 71003, 71001, 71002, 71003)]
            masks = [row["mask_sha256"] for row in trials]
            passed = masks[:3] == masks[3:] and len(set(masks)) == 3
            return {"status": "passed" if passed else "failed", "kernel": command("uname", "-r"),
                    "tc": command(tc, "-V"), "trials": trials}
        finally:
            command("ip", "netns", "del", rx)
    finally:
        command("ip", "netns", "del", tx)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tc", default="tc")
    result = verify(parser.parse_args().tc)
    print(json.dumps(result, indent=2))
    sys.exit(result["status"] != "passed")
