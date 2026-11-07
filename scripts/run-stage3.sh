#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if (( $# > 1 )); then echo "Usage: run-stage3.sh [NEW_RESULTS_DIRECTORY]" >&2; exit 2; fi
if (( EUID != 0 )); then echo "Run this script with sudo for netns, tc and tcpdump." >&2; exit 1; fi
out=$(realpath -m -- "${1:-$root/results/runs/stage3-$(date -u +%Y%m%dT%H%M%SZ)-$$}")
mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"
if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
    trap 'find "$out" -maxdepth 1 -exec chown --no-dereference "$SUDO_UID:$SUDO_GID" {} +' EXIT
fi

export IOT_SEED=${IOT_SEED:-17}
python3 "$root/src/workload/generate.py" "$out/workload.bin" --profile baselines --seed "$IOT_SEED"
cp -- "$root/configs/stage3-policies.txt" "$out/policies.txt"
while IFS= read -r policy || [[ -n "$policy" ]]; do
    policy=${policy%$'\r'}
    [[ $policy =~ ^[a-z][a-z-]*$ ]] || { echo "Invalid policy name: $policy" >&2; exit 2; }
    bash "$root/scripts/run-lab.sh" 3 "$out/$policy" "$policy" "$out/workload.bin"
done < "$out/policies.txt"
python3 "$root/evaluation/verify_stage3.py" --compare "$out" | tee "$out/comparison.json"
