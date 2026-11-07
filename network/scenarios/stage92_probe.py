"""Isolated directional UDP probes for command and data-plane calibration."""

import argparse
import json
import os
import select
import socket
import struct
import subprocess
import time
from pathlib import Path

from stage5 import clock_us, configure

INITIAL = {"a": [5, 25, 0], "b": [18, 25, 0]}
SCENARIOS = {
    "rtt": [(400_000, {"a": [22, 25, 0]})],
    "bandwidth": [(400_000, {"a": [5, 4, 0]})],
    "loss": [(400_000, {"a": [5, 25, 50]})],
    "reversal": [(400_000, {"a": [18, 25, 0], "b": [5, 25, 0]})],
    "outage": [(400_000, {"a": [5, 25, 100]}), (470_000, {"a": [5, 25, 0]})],
}
INTERVAL = 7000
GROUP = 4
HEADER = struct.Struct("!IQ")


def probe(side, origin):
    own, peer = (1, 2) if side == "client" else (2, 1)
    sockets = {}
    for link, subnet in (("a", 1), ("b", 2)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((f"10.47.{subnet}.{own}", 49792))
        sock.connect((f"10.47.{subnet}.{peer}", 49792))
        sockets[sock] = link
    rows = []
    tick, seq = origin, 0
    while clock_us() < origin + 1_250_000:
        if clock_us() >= tick and tick < origin + 1_100_000:
            for sock, link in sockets.items():
                for i in range(GROUP):
                    sent = clock_us()
                    sock.send(HEADER.pack(seq + i, sent) + bytes(1000 - HEADER.size))
                    rows.append(("tx", link, seq + i, sent, sent))
            seq += GROUP
            tick = max(tick + INTERVAL, clock_us() + 1000)
        for sock in select.select(sockets, [], [], .001)[0]:
            packet = sock.recv(1200)
            seqno, sent = HEADER.unpack_from(packet)
            rows.append(("rx", sockets[sock], seqno, sent, clock_us()))
    print(json.dumps(rows))


def trial(folder, scenario, replicate, tc):
    folder.mkdir(parents=True)
    root = Path(__file__).resolve().parents[2]
    token = f"{os.getpid()}-{replicate}"
    namespaces = (f"q92c-{token}", f"q92s-{token}")
    children, streams = [], []
    with (folder / "execution.log").open("w") as log:
        try:
            topology = 'client_ns=$1; server_ns=$2; source "$3"; setup_topology'
            subprocess.run(["bash", "-ec", topology, "probe", *namespaces,
                            root / "network/topology/dual-veth.sh"], check=True, stdout=log, stderr=log)
            for namespace in namespaces:
                subprocess.run(["ip", "netns", "exec", namespace, "sysctl", "-qw",
                                "net.ipv6.conf.all.disable_ipv6=1"], check=True, stderr=log)
            origin = clock_us() + 180_000
            initial = configure(namespaces, INITIAL, None, None, seeded_tc=tc, detailed=True)
            origin = clock_us() + 260_000
            for side, namespace in zip(("client", "server"), namespaces):
                fd = (folder / f"{side}.json").open("w")
                streams.append(fd)
                children.append(subprocess.Popen(["ip", "netns", "exec", namespace, "python3", "-B",
                                                  __file__, "--probe", side, str(origin)], stdout=fd, stderr=log))
            with (folder / "network.jsonl").open("w") as file:
                file.write(json.dumps({**initial, "origin_us": origin}) + "\n")
                file.flush()
                for offset, links in SCENARIOS[scenario]:
                    time.sleep(max(0, (origin + offset - clock_us()) / 1e6))
                    seed = 940_000 + replicate if tc and scenario == "loss" else None
                    edge = configure(namespaces, links, offset, seed, seeded_tc=tc, detailed=True)
                    file.write(json.dumps({**edge, "loss_seed": seed}) + "\n")
                    file.flush()
            if any([child.wait(timeout=8) for child in children]):
                raise RuntimeError("UDP probe exited unsuccessfully")
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)
            for stream in streams:
                stream.close()
            for namespace in reversed(namespaces):
                subprocess.run(["ip", "netns", "del", namespace], stdout=log, stderr=log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", nargs=2)
    args = parser.parse_args()
    side, epoch = args.probe
    probe(side, int(epoch))
