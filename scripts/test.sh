#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build=$(realpath -m -- "${IOT_BUILD_DIR:-$root/build}")
if [[ ! -f "$build/app/CMakeCache.txt" ]]; then
    bash "$root/scripts/build.sh"
fi
cmake --build "$build/app" --target iot-mpquic iot-scheduler-test -j "${JOBS:-$(nproc)}"
ctest --test-dir "$build/app" --output-on-failure
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/stage7_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/stage8_test.py"
