"""Record queue and discard events for only the calibration probe processes."""

import json
import os
import re
import time
from pathlib import Path

TRACEFS = Path("/sys/kernel/tracing")


def prepare_loss_trace(stack, folder):
    instance = TRACEFS / "instances" / f"iot-{os.getpid()}"
    instance.mkdir()
    stack.callback(instance.rmdir)
    (instance / "tracing_on").write_text("0")
    (instance / "trace_clock").write_text("mono")
    (instance / "buffer_size_kb").write_text("1024")
    queue = instance / "events/net/net_dev_queue"
    drop = instance / "events/skb/kfree_skb"
    reason = re.search(r'\{\s*(\d+),\s*"QDISC_DROP"\s*\}', (drop / "format").read_text())
    if not reason:
        raise ValueError("kernel does not expose the QDISC_DROP reason")
    (queue / "filter").write_text('name == "iot-a"')
    (drop / "filter").write_text(f"reason == {reason[1]}")
    def save():
        (instance / "tracing_on").write_text("0")
        (folder / "kernel-events.log").write_text((instance / "trace").read_text())
        (queue / "enable").write_text("0")
        (drop / "enable").write_text("0")

    stack.callback(save)
    (instance / "tracing_on").write_text("1")
    return instance


IDENTITY = re.compile(r"-(\d+)\s+\[\d+\].*tracing_mark_write: iot-probe (client|server)\b")


def enable_loss_trace(instance, folder):
    # tracefs filters on initial PID namespace IDs. A marker written by each
    # probe identifies its kernel PID without reading unrelated task records.
    deadline = time.monotonic() + .18
    while time.monotonic() < deadline:
        matches = IDENTITY.findall((instance / "trace").read_text())
        if len(matches) == 2 and {role for _, role in matches} == {"client", "server"}:
            pids = {role: int(pid) for pid, role in matches}
            (folder / "kernel-pids.json").write_text(json.dumps(pids) + "\n")
            (instance / "set_event_pid").write_text(" ".join(map(str, pids.values())))
            (instance / "events/net/net_dev_queue/enable").write_text("1")
            (instance / "events/skb/kfree_skb/enable").write_text("1")
            return
        time.sleep(.001)
    raise TimeoutError("probe did not publish its kernel tracing identity")


EVENT = re.compile(r"-(\d+)\s+\[\d+\]\s+\S+\s+(\d+)\.(\d{6}):\s+(net_dev_queue|kfree_skb):.*?skbaddr=(\S+)")


def trace_drops(file, pid):
    text = file.read_text()
    counts = re.search(r"entries-in-buffer/entries-written:\s*(\d+)/(\d+)", text)
    if not counts or counts[1] != counts[2]:
        raise ValueError("kernel event buffer was truncated")
    queued, drops = {}, []
    for line in text.splitlines():
        match = EVENT.search(line)
        if not match or int(match[1]) != pid:
            continue
        timestamp = int(match[2]) * 1_000_000 + int(match[3])
        kind, address = match[4], match[5]
        if kind == "net_dev_queue":
            queued[address] = timestamp
        elif address in queued and "QDISC_DROP" in line:
            drops.append((queued.pop(address), timestamp))
    return drops


def identify_first_drop(drops, sent, received, start, first_missing):
    attempts = [(seq, begin, end) for kind, link, seq, begin, end in sent if kind == "tx" and link == "a"]
    observed = {seq for kind, link, seq, _, _ in received if kind == "rx" and link == "a"}
    if not drops or first_missing is None or any(drop < start for _, drop in drops):
        return None
    queued, drop = min(drops, key=lambda row: row[1])
    matching = [(seq, begin) for seq, begin, end in attempts if begin <= queued <= drop <= end]
    if len(matching) == 1 and matching[0][0] not in observed and matching[0][1] == first_missing:
        return {"probe_seq": matching[0][0], "kernel_queue_us": queued, "kernel_drop_us": drop}
    return None
