"""Acquire only Stage 9.3-C engineering witnesses in isolated namespaces."""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from run_stage92 import checksums, provenance, stamp
from stage92_design import ROOT, file_hash, fingerprint, write_workload

sys.path[:0] = [str(ROOT / "evaluation"), str(ROOT / "network/scenarios")]
from stage5 import configure
from stage92_probe import fixed_rate_reversal
from stage92r_control import transitions
from stage93a import write_json
from stage93c import limits, make_schedule, pair_plan, validate_frozen
from stage93c_probe import LINKS, SIDES, identity, token_for

TRACEFS = Path("/sys/kernel/tracing/instances")


def wait_for(process, predicate, description, timeout=5):
    stop = time.monotonic() + timeout
    while time.monotonic() < stop:
        if (result := predicate()):
            return result
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{description}: process exited {process.returncode}")
        time.sleep(.002)
    raise TimeoutError(description)


def stop_process(child, sig=signal.SIGTERM):
    if child.poll() is None:
        child.send_signal(sig)
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def child(stack, args, **kwargs):
    proc = subprocess.Popen(args, **kwargs)
    stack.callback(stop_process, proc)
    return proc


def wait_log(process, path, pattern):
    return wait_for(process, lambda: re.search(pattern, path.read_text(errors="replace")) if path.exists() else None,
                    f"missing {pattern} in {path.name}")


def trace_instance(stack, folder, name):
    instance = TRACEFS / name
    instance.mkdir()
    stack.callback(instance.rmdir)
    (instance / "tracing_on").write_text("0")
    (instance / "trace_clock").write_text("mono")
    (instance / "buffer_size_kb").write_text("2048")
    queue = instance / "events/net/net_dev_queue"
    drop = instance / "events/skb/kfree_skb"
    reason = re.search(r'\{\s*(\d+),\s*"QDISC_DROP"\s*\}', (drop / "format").read_text())
    if not reason:
        raise ValueError("kernel does not expose QDISC_DROP")
    (queue / "filter").write_text('name == "iot-a" || name == "iot-b"')
    (drop / "filter").write_text(f"reason == {reason[1]}")
    (instance / "tracing_on").write_text("1")
    marker = stack.enter_context((instance / "trace_marker").open("wb", buffering=0))
    pids = {}

    def save_trace():
        (instance / "tracing_on").write_text("0")
        (folder / "kernel-events.log").write_text((instance / "trace").read_text())
        stats = {file.parent.name: file.read_text() for file in sorted(instance.glob("per_cpu/cpu*/stats"))}
        lost = sum(int(x) for value in stats.values()
                   for x in re.findall(r"^(?:overrun|commit overrun|dropped events):\s*(\d+)", value, re.M))
        active = re.search(r"\[(\w+)\]", (instance / "trace_clock").read_text())
        write_json(folder / "tracefs.json", {"clock": active[1] if active else None, "pids": pids,
                                             "lost_events": lost, "per_cpu_stats": stats})
        (queue / "enable").write_text("0")
        (drop / "enable").write_text("0")

    stack.callback(save_trace)
    return instance, marker, pids


def arm_trace(instance, case, pids):
    pattern = rf"-(\d+)\s+\[\d+\].*tracing_mark_write: stage93c {token_for(case).hex()} (client|server)\b"
    matches = re.findall(pattern, (instance / "trace").read_text())
    if len(matches) != 2 or {role for _, role in matches} != set(SIDES):
        raise ValueError("tracefs probe markers do not match both endpoints")
    pids.update({role: int(pid) for pid, role in matches})
    (instance / "set_event_pid").write_text(" ".join(map(str, pids.values())))
    (instance / "events/net/net_dev_queue/enable").write_text("1")
    (instance / "events/skb/kfree_skb/enable").write_text("1")


def run_case(folder, case, build, tc, cpus, settings, execution_log):
    if (case.get("split") != "engineering" or not case.get("name", "").startswith("c93-")
            or not case.get("connection_id", "").startswith("c93-engineering-")):
        raise ValueError("Stage 9.3-C can execute only isolated engineering identities")
    folder.mkdir()
    workload = folder / "workload.bin"
    write_workload(workload, case)
    case = {**case, "workload_sha256": file_hash(workload)}
    write_json(folder / "case.json", case)
    names = tuple(f"c93-{os.getpid()}-{case['serial']}-{side[0]}" for side in SIDES)
    namespaces = dict(zip(SIDES, names))
    if set(names) & {line.split()[0] for line in subprocess.check_output(["ip", "netns", "list"], text=True).splitlines()}:
        raise ValueError("engineering namespace already exists")
    executable = build / "app/iot-mpquic"
    with ExitStack() as stack:
        for name in names:
            stack.callback(subprocess.run, ["ip", "netns", "del", name], stdout=execution_log, stderr=execution_log)
        subprocess.run(["bash", "-ec", 'client_ns=$1; server_ns=$2; source "$3"; setup_topology',
                        "stage93c", *names, ROOT / "network/topology/dual-veth.sh"], check=True,
                       stdout=execution_log, stderr=execution_log)
        for name in names:
            subprocess.run(["ip", "netns", "exec", name, "sysctl", "-qw", "net.ipv6.conf.all.disable_ipv6=1"], check=True)
        delay_leaf = case["scenario"] == "reversal"
        if delay_leaf:
            initial = fixed_rate_reversal(names, str(tc), case["initial"])
        else:
            initial = configure(names, case["initial"], None, None, seeded_tc=str(tc), detailed=True)
        (folder / "network.jsonl").write_text(json.dumps(initial) + "\n")
        with tempfile.TemporaryDirectory(prefix="stage93c-tls-") as tls:
            cert, key = Path(tls) / "cert.pem", Path(tls) / "key.pem"
            with (folder / "openssl.log").open("w") as log:
                subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
                                "-nodes", "-days", "1", "-subj", "/CN=iot-stage93c", "-keyout", key, "-out", cert],
                               check=True, stderr=log)
            capture_filter = "udp and port 4433 and port 4434"
            timestamp_options = []
            if case["arm"] != "E0":
                capture_filter = "udp and ((port 4433 and port 4434) or port 49792)"
                timestamp_options = ["-j", "host", "--time-stamp-precision=nano"]
            captures = []
            for i, side in enumerate(SIDES):
                prefix = "client-" if side == "client" else ""
                for link in LINKS:
                    log_path = folder / f"{prefix}tcpdump-{link}.log"
                    log = stack.enter_context(log_path.open("w"))
                    args = ["taskset", "-c", str(cpus[i + 2]), sys.executable, "-B", ROOT / "scripts/capture_usage.py",
                            folder / f"capture-{side}-{link}.json", "ip", "netns", "exec", namespaces[side],
                            "tcpdump", "-nn", *timestamp_options, "--immediate-mode",
                            "-i", f"iot-{link}", "-s", "0", "-B", "4096", "-Z", "root",
                            "-w", folder / f"wire-{side}-{link}.pcap", capture_filter]
                    proc = child(stack, args, stderr=log, stdout=execution_log)
                    captures.append(proc)
                    wait_log(proc, log_path, "listening on")
            trace = trace_instance(stack, folder, f"stage93c-{os.getpid()}-{case['serial']}") if case["arm"] == "E2" else None
            probes = []
            if case["arm"] != "E0":
                for i, side in enumerate(SIDES):
                    args = ["chrt", "-f", "1", "taskset", "-c", str(cpus[i + 2]), "ip", "netns", "exec",
                            namespaces[side], sys.executable,
                            "-B", ROOT / "network/scenarios/stage93c_probe.py", side, folder, str(settings["probe_period_us"])]
                    fds = (trace[1].fileno(),) if trace else ()
                    if fds:
                        args += ["--marker", str(fds[0])]
                    probes.append(child(stack, args, pass_fds=fds, stderr=execution_log, stdout=execution_log))
                for side, proc in zip(SIDES, probes):
                    wait_for(proc, (folder / f"probe-{side}.ready").exists, f"{side} probe socket readiness")
                if trace:
                    arm_trace(trace[0], case, trace[2])
            env = {key: val for key, val in os.environ.items() if not key.startswith(("IOT_STAGE", "IOT_TUNING"))}
            env.update(IOT_SEED=str(case["seed"]), IOT_PATH_POLICY=case["path_scheduler"],
                       IOT_STAGE8_SEED=str(case["assignment_seed"]), IOT_HORIZON_US=str(case["horizon_us"]),
                       IOT_STAGE91_TRACE="1", IOT_STAGE92_PROFILE="1", IOT_STAGE92_DIGEST="1",
                       IOT_STAGE92_FEATURES="1", IOT_STAGE92R2="1")
            endpoints = {}
            launched = {}
            for i, side in ((1, "server"), (0, "client")):
                log_path = folder / f"{side}.log"
                log = stack.enter_context(log_path.open("w"))
                err = stack.enter_context((folder / f"{side}.stderr").open("w"))
                args = [cert, key] if side == "server" else [workload, case["policy"], str(case["recovery_k"])]
                launched[side] = time.monotonic_ns()
                endpoints[side] = child(stack, ["taskset", "-c", str(cpus[i]), "ip", "netns", "exec", namespaces[side],
                                               executable, f"iot-{side}", folder / f"{side}.xquic.log", *args],
                                        stdout=log, stderr=err, env=env)
                if side == "server":
                    wait_log(endpoints[side], log_path, "event=ready role=server")
            origin = int(wait_log(endpoints["client"], folder / "client.log", r"event=iot_start origin=(\d+) ")[1])
            observed = time.monotonic_ns()
            if probes:
                gate = folder / "probe-gate.pending"
                write_json(gate, {"identity": identity(case), "origin_us": origin})
                gate.rename(folder / "probe-gate.json")
            with (folder / "network.jsonl").open("a") as timeline:
                for row in transitions(names, case["phases"], origin, case["network_seed"], str(tc), delay_leaf):
                    timeline.write(json.dumps(row) + "\n")
                    timeline.flush()
            results = {side: proc.wait(timeout=10) for side, proc in endpoints.items()}
            probe_status = [proc.wait(timeout=8) for proc in probes]
            for proc in captures:
                stop_process(proc, signal.SIGINT)
            write_json(folder / "run-clock.json", {"identity": identity(case), "origin_us": origin,
                                                  "client_origin_seen_ns": observed, "launched_ns": launched,
                                                  "endpoint_exit": results, "probe_exit": probe_status,
                                                  "finished_ns": time.monotonic_ns(), "cpu_affinity": cpus})
            if any(probe_status) or any(proc.returncode for proc in captures):
                raise RuntimeError("probe or capture process failed")


def acquire(folder, build, tc):
    if os.geteuid() != 0:
        raise ValueError("Stage 9.3-C requires Linux network namespace and tracefs privileges")
    quality, settings = limits()
    frozen = validate_frozen()
    planning = pair_plan(quality, settings)
    schedule = make_schedule(frozen, planning, settings)
    fingerprint_before = provenance(build, tc)
    folder.mkdir(parents=True)
    (folder / "runs").mkdir()
    (folder / "execution").mkdir()
    cpus = sorted(os.sched_getaffinity(0))[:4]
    if len(cpus) != 4:
        raise ValueError("four CPUs required for the frozen instrumentation layout")
    manifest = {"stage": "9.3-C", "frozen_plan_sha256": frozen["file_sha256"], "quality": quality,
                "settings": settings, "pair_plan": planning, "schedule": schedule, "schedule_sha256": fingerprint(schedule),
                "build": fingerprint_before, "cpu_affinity": cpus, "sealed_executions": 0, "sealed_payload_reads": 0,
                "frozen_pilot_executions": 0, "kernel": os.uname().release}
    write_json(folder / "manifest.json", manifest)
    inventory = []
    for case in schedule:
        status = {"name": case["name"], "started_utc": stamp(), "exit_code": 0}
        print(f"{case['name']} {case['environment']} {case['load']} {case['arm']}", flush=True)
        with (folder / "execution" / f"{case['name']}.log").open("w") as log:
            try:
                run_case(folder / "runs" / case["name"], case, build, tc, cpus, settings, log)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                status.update(exit_code=1, error=str(error))
        inventory.append({**status, "ended_utc": stamp()})
        write_json(folder / "execution.json", inventory)
    unchanged = fingerprint_before == provenance(build, tc) and validate_frozen()["file_sha256"] == frozen["file_sha256"]
    write_json(folder / "manifest.json", {**manifest, "source_build_and_frozen_plan_unchanged": unchanged})
    write_json(folder / "sha256.json", checksums(folder))
    return unchanged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--tc", type=Path, default=Path("/usr/sbin/tc"))
    args = parser.parse_args()
    raise SystemExit(0 if acquire(args.directory.resolve(), args.build.resolve(), args.tc.resolve()) else 1)
