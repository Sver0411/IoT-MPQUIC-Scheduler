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


def probe(side, origin, interval=INTERVAL, size=1000, horizon=1_250_000, burst=None, readiness=None):
    own, peer = (1, 2) if side == "client" else (2, 1)
    sockets = {}
    for link, subnet in (("a", 1), ("b", 2)):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind((f"10.47.{subnet}.{own}", 49792))
        sock.connect((f"10.47.{subnet}.{peer}", 49792))
        sockets[sock] = link
    if readiness:
        other = "server" if side == "client" else "client"
        Path(readiness[side]).touch(exist_ok=False)
        while not Path(readiness[other]).exists():
            if clock_us() >= origin + 350_000:
                raise TimeoutError("peer probe sockets not ready")
            time.sleep(.001)
    rows = []
    tick, seq = origin, 0
    burst_tick = origin + burst["at_us"] if burst else 0
    burst_end = burst_tick + burst["duration_us"] if burst else 0
    burst_seq = 10_000_000
    while clock_us() < origin + horizon:
        if burst and burst_tick <= clock_us() < burst_end:
            sock = next(sock for sock, link in sockets.items() if link == burst["link"])
            for _ in range(burst["packets"]):
                sent = clock_us()
                sock.send(HEADER.pack(burst_seq, sent) + bytes(burst["bytes"] - HEADER.size))
                rows.append(("burst_tx", burst["link"], burst_seq, sent, clock_us()))
                burst_seq += 1
            burst_tick = max(burst_tick + burst["interval_us"], clock_us() + 100)
        if clock_us() >= tick and tick < origin + horizon - 150_000:
            for sock, link in sockets.items():
                for i in range(GROUP):
                    sent = clock_us()
                    sock.send(HEADER.pack(seq + i, sent) + bytes(size - HEADER.size))
                    rows.append(("tx", link, seq + i, sent, clock_us()))
            seq += GROUP
            tick = max(tick + interval, clock_us() + min(1000, interval // 3))
        timeout = min(.001, max(0, tick - clock_us()) / 1e6) if tick < origin + horizon - 150_000 else .001
        for sock in select.select(sockets, [], [], timeout)[0]:
            while True:
                try:
                    packet = sock.recv(1200, socket.MSG_DONTWAIT)
                except BlockingIOError:
                    break
                received = clock_us()
                seqno, sent = HEADER.unpack_from(packet)
                rows.append(("burst_rx" if seqno >= 10_000_000 else "rx", sockets[sock], seqno, sent, received))
    print(json.dumps(rows))


def start_probes(stack, folder, namespaces, origin, options, log):
    children = []
    readiness = {side: str(folder / f"probe-ready-{side}") for side in ("client", "server")}
    for side, namespace in zip(("client", "server"), namespaces):
        fd = stack.enter_context((folder / f"{side}.json").open("w"))
        kwargs = {**options, "readiness": readiness}
        if kwargs.get("burst", {}).get("side") != side:
            kwargs.pop("burst", None)
        child = subprocess.Popen(["ip", "netns", "exec", namespace, "python3", "-B", __file__, "--probe",
                                  side, str(origin), "--options", json.dumps(kwargs)], stdout=fd, stderr=log)
        stack.callback(stop_child, child)
        children.append(child)
    return children


def stop_child(child):
    if child.poll() is None:
        child.terminate()
        child.wait(timeout=3)


def trial(folder, scenario, replicate, tc, refined=False):
    from contextlib import ExitStack

    folder.mkdir(parents=True)
    root = Path(__file__).resolve().parents[2]
    token = f"{os.getpid()}-{replicate}"
    namespaces = (f"q92c-{token}", f"q92s-{token}")
    with (folder / "execution.log").open("w") as log, ExitStack() as stack:
        try:
            topology = 'client_ns=$1; server_ns=$2; source "$3"; setup_topology'
            subprocess.run(["bash", "-ec", topology, "probe", *namespaces,
                            root / "network/topology/dual-veth.sh"], check=True, stdout=log, stderr=log)
            for namespace in namespaces:
                subprocess.run(["ip", "netns", "exec", namespace, "sysctl", "-qw",
                                "net.ipv6.conf.all.disable_ipv6=1"], check=True, stderr=log)
            initial = configure(namespaces, INITIAL, None, None, seeded_tc=tc, detailed=True)
            origin = clock_us() + 260_000
            options = ({"interval": 3000, "size": 320} if scenario == "bandwidth" else
                       {"interval": 1000, "size": 72}) if refined else {}
            children = start_probes(stack, folder, namespaces, origin, options, log)
            with (folder / "network.jsonl").open("w") as file:
                file.write(json.dumps({**initial, "origin_us": origin, "probe_options": options}) + "\n")
                file.flush()
                if refined:
                    from stage92r_control import transitions
                    phases = [{"at_us": offset, "links": links} for offset, links in SCENARIOS[scenario]]
                    for edge in transitions(namespaces, phases, origin, 950_000 + 16 * replicate, tc):
                        file.write(json.dumps(edge) + "\n")
                        file.flush()
                else:
                    for offset, links in SCENARIOS[scenario]:
                        time.sleep(max(0, (origin + offset - clock_us()) / 1e6))
                        seed = 940_000 + replicate if tc and scenario == "loss" else None
                        edge = configure(namespaces, links, offset, seed, seeded_tc=tc, detailed=True)
                        file.write(json.dumps({**edge, "loss_seed": seed}) + "\n")
                        file.flush()
            if any([child.wait(timeout=8) for child in children]):
                raise RuntimeError("UDP probe exited unsuccessfully")
        finally:
            stack.close()
            for namespace in reversed(namespaces):
                subprocess.run(["ip", "netns", "del", namespace], stdout=log, stderr=log)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", nargs=2)
    parser.add_argument("--options", type=json.loads, default={})
    args = parser.parse_args()
    side, epoch = args.probe
    probe(side, int(epoch), **args.options)
