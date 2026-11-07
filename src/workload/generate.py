"""Generate the seeded Stage 2 replay, with times relative to connection readiness."""

import argparse
import random
import struct
from pathlib import Path

PREFIX = struct.Struct("!QI")
RECORD = struct.Struct("!4sBBBBIIIQQQQ")


def baseline_workload(rng, profile):
    records = []

    def state(sensor, sequence, generated, release, size):
        max_age = 120_000
        if profile == "baselines":
            max_age = {1: 80_000, 3: 200_000}.get(sensor, max_age)
        records.append((release, 1, 1, 0, sensor, sequence, generated, generated + 160_000,
                        max_age, rng.randbytes(size)))

    state(1, 1, 0, 0, 16384)
    state(2, 1, 0, 0, 4096)
    state(2, 2, 0, 0, 4096)  # Same replay tick replaces a pending version.
    state(9, 2, 0, 0, 4096)

    for event_id in range(100, 120):
        severity = rng.randint(1, 5)
        deadline = 2_000 if event_id % 5 == 0 else 20_000_000
        if profile == "baselines":
            deadline = (100_000, 750_000, 240_000, 1_100_000, 420_000)[event_id % 5]
        content = rng.randbytes(rng.randint(12288, 16384))
        event = (2, severity + 1, severity, 1 + event_id % 3, event_id, 0, deadline, 0, content)
        records.append((0, *event))
        if event_id == 101:
            records.append((400_000, *event))

    sensors = (1, 2, 3, 9) if profile == "baselines" else (1, 2, 3)
    for period in range(1, 11):
        generated = 40_000 + (period - 1) * 80_000
        for sensor in sensors:
            state(sensor, period + 2, generated, generated, rng.randint(4096, 8192))
    state(9, 1, 0, 600_000, 4096)  # Same timestamp, older sequence delivered late.
    return records


def generate(seed, output, profile="stage2"):
    rng = random.Random(seed)
    if profile in {"proposed", "repair-probe"}:
        records = proposed_workload(rng, probe_budget_us=25_000 if profile == "repair-probe" else 310_000)
    elif profile in {"mixed", "state-only", "event-burst", "high-mixed"}:
        records = experiment_workload(rng, profile)
    else:
        records = baseline_workload(rng, profile)
    with output.open("wb") as file:
        for id_, record in enumerate(sorted(records, key=lambda row: row[0]), 1):
            release, kind, priority, severity, source, serial, generated, deadline, max_age, data = record
            header = RECORD.pack(b"IOT1", kind, priority, severity, 0, len(data), id_, source,
                                 serial, generated, deadline, max_age)
            file.write(PREFIX.pack(release, len(header) + len(data)))
            file.write(header)
            file.write(data)


def proposed_workload(rng, probe_budget_us):
    records = [(240_000, 2, 6, 5, 1, 900, 240_000, 240_000 + probe_budget_us, 0, rng.randbytes(4096))]
    for i in range(16):
        release = 330_000 + (i // 4) * 160_000
        severity = rng.randint(1, 5)
        records.append((release, 2, severity + 1, severity, 2, 901 + i, release,
                        release + 280_000 + (i % 3) * 150_000, 0, rng.randbytes(3200)))
    for period in range(5):
        release = 340_000 + period * 240_000
        for sensor in (1, 2, 3, 9):
            records.append((release, 1, 1, 0, sensor, period + 2, release,
                            release + 1_500_000, 1_250_000, rng.randbytes(2048)))
    records.append((560_000, 1, 2, 0, 7, 1, 0, 80_000, 80_000, rng.randbytes(2048)))
    records.append((5_000_000, 1, 1, 0, 9, 1, 0, 80_000, 80_000, rng.randbytes(2048)))
    return records


def experiment_workload(rng, profile):
    records = []
    high = profile == "high-mixed"
    if profile != "event-burst":
        period = 160_000 if high else 260_000
        sensors = range(1, 5) if high else range(1, 4)
        for sequence in range(1, 9):
            release = 240_000 + (sequence - 1) * period
            for sensor in sensors:
                size = rng.randint(3400, 4900) if high else rng.randint(1400, 2400)
                records.append((release, 1, 1, 0, sensor, sequence, release,
                                release + 650_000, 220_000 + 70_000 * sensor, rng.randbytes(size)))
        if high:
            records.append((530_000, 1, 1, 0, 7, 1, 0, 150_000, 150_000, rng.randbytes(2400)))
    if profile != "state-only":
        bursts = (220_000, 400_000, 650_000, 950_000) if high else (220_000, 620_000, 1_160_000)
        width = 5 if high else (6 if profile == "event-burst" else 3)
        for i, release in enumerate(bursts):
            for j in range(width):
                severity = rng.randint(1, 5)
                size = rng.randint(2800, 4600) if high else rng.randint(1600, 3600)
                budget = (300_000, 650_000, 1_100_000)[j % 3]
                records.append((release, 2, severity + 1, severity, 1 + i, 100 + i * width + j,
                                release, release + budget, 0, rng.randbytes(size)))
    return records


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--profile", choices=("stage2", "baselines", "proposed", "repair-probe", "mixed", "state-only",
                                               "event-burst", "high-mixed"), default="stage2")
    args = parser.parse_args()
    generate(args.seed, args.output, args.profile)
