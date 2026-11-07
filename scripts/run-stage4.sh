#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if (( $# > 1 )); then echo "Usage: run-stage4.sh [NEW_RESULTS_DIRECTORY]" >&2; exit 2; fi
if (( EUID != 0 )); then echo "Run this script with sudo for netns, tc and tcpdump." >&2; exit 1; fi
k=${IOT_RECOVERY_K:-2}
if ! [[ $k =~ ^([1-9]|[1-5][0-9]|6[0-4])$ ]]; then
    echo "IOT_RECOVERY_K must be an integer from 1 to 64." >&2
    exit 2
fi
out=$(realpath -m -- "${1:-$root/results/runs/stage4-$(date -u +%Y%m%dT%H%M%SZ)-$$}")
mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
    trap 'find "$out" -maxdepth 1 -exec chown --no-dereference "$SUDO_UID:$SUDO_GID" {} +' EXIT
fi

export IOT_SEED=${IOT_SEED:-17}
python3 "$root/src/workload/generate.py" "$out/workload.bin" --profile proposed --seed "$IOT_SEED"
for mode in proposed-strict proposed-cross; do
    bash "$root/scripts/run-lab.sh" 4 "$out/$mode" "$mode" "$out/workload.bin" "$k"
done
python3 "$root/evaluation/verify_stage4.py" --compare "$out" | tee "$out/comparison.json"
