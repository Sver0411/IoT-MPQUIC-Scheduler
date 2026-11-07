#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
build=$(realpath -m -- "${IOT_BUILD_DIR:-$root/build}")
xquic_rev=dffd32c42a511edae544b87169976516b7c4cbfa
boring_rev=b69a6175320af9b6fca7295fdbb4fb130f4804a2

for command in git tar sha256sum cmake ninja cc c++; do
    command -v "$command" >/dev/null || { echo "Missing build tool: $command" >&2; exit 1; }
done
if [[ ! -f "$root/third_party/xquic/CMakeLists.txt" ]] ||
   [[ $(git -C "$root/third_party/xquic" rev-parse HEAD) != "$xquic_rev" ]] ||
   [[ -n $(git -C "$root/third_party/xquic" status --porcelain --untracked-files=no) ]]; then
    echo "Expected unmodified XQUIC $xquic_rev. Run git submodule update --init." >&2
    exit 1
fi

mkdir -p "$build"
# Export a separate tree for CMake-generated headers and the research patch.
patch="$root/patches/xquic/stream-pto-scheduler.patch"
integrity="$root/patches/xquic/stage91-integrity-trace.patch"
patch_hash=$(sha256sum < "$patch")
integrity_hash=$(sha256sum < "$integrity")
xquic_src="$build/xquic-$xquic_rev-${patch_hash%% *}-${integrity_hash%% *}"
if [[ ! -f "$xquic_src/.export-complete" ]]; then
    mkdir -p "$xquic_src"
    git -C "$root/third_party/xquic" archive "$xquic_rev" | tar -x -C "$xquic_src"
    # Run outside the parent repository even when the build directory is inside it.
    git --git-dir="$xquic_src/.git" init -q
    git -C "$xquic_src" apply "$patch" "$integrity"
    touch "$xquic_src/.export-complete"
fi

boring="$build/boringssl"
if [[ ! -d "$boring/.git" ]]; then
    git init -q "$boring"
fi
if ! git -C "$boring" cat-file -e "$boring_rev^{commit}" 2>/dev/null; then
    git -C "$boring" fetch --depth=1 https://github.com/google/boringssl.git "$boring_rev"
fi
if [[ $(git -C "$boring" rev-parse --verify HEAD 2>/dev/null || true) != "$boring_rev" ]]; then
    git -C "$boring" checkout --detach "$boring_rev"
fi
if [[ -n $(git -C "$boring" status --porcelain) ]]; then
    echo "BoringSSL checkout is dirty: $boring" >&2
    exit 1
fi

cmake -S "$boring" -B "$build/tls" -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_TESTING=OFF -DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON
cmake --build "$build/tls" --target ssl crypto -j "${JOBS:-$(nproc)}"
cmake -S "$root" -B "$build/app" -G Ninja -DCMAKE_BUILD_TYPE=Release -DXQUIC_SOURCE_DIR="$xquic_src" \
    -DSSL_TYPE=boringssl -DSSL_PATH="$boring" -DSSL_INC_PATH="$boring/include" \
    "-DSSL_LIB_PATH=$build/tls/libssl.a;$build/tls/libcrypto.a" -DXQC_ENABLE_EVENT_LOG=ON
cmake --build "$build/app" --target iot-mpquic -j "${JOBS:-$(nproc)}"
