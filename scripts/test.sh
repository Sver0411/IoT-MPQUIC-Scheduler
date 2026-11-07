#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build=$(realpath -m -- "${IOT_BUILD_DIR:-$root/build}")
bash "$root/scripts/build.sh"
cmake --build "$build/app" --target iot-scheduler-test -j "${JOBS:-$(nproc)}"
ctest --test-dir "$build/app" --output-on-failure
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/stage7_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/stage8_test.py"
PYTHONDONTWRITEBYTECODE=1 python3 "$root/tests/stage91_test.py" "$@"
