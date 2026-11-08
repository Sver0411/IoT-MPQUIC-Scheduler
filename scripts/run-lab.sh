#!/usr/bin/env bash
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
stage=${1:-}
windowed=false
if [[ $stage == 7 || $stage == 8 || $stage == 91 || $stage == 92 ]]; then windowed=true; fi
if ! { [[ $stage == 1 || $stage == 2 ]] && (( $# <= 2 )); } &&
   ! { [[ $stage == 3 ]] && (( $# == 4 )); } &&
   ! { [[ $stage == 4 ]] && (( $# == 5 )); } &&
   ! { { [[ $stage == 5 ]] || $windowed; } && (( $# == 7 )); }; then
    echo "Usage: run-lab.sh {1|2} [DIRECTORY] | run-lab.sh 3 DIRECTORY POLICY WORKLOAD | run-lab.sh 4 DIRECTORY POLICY WORKLOAD K | run-lab.sh {5|7|8|91|92} DIRECTORY POLICY WORKLOAD K CASE ABLATION" >&2
    exit 2
fi
shift
exe="${IOT_BUILD_DIR:-$root/build}/app/iot-mpquic"
out=$(realpath -m -- "${1:-$root/results/runs/stage$stage-$(date -u +%Y%m%dT%H%M%SZ)-$$}")
client_ns="iotc-$$"
server_ns="iots-$$"
client_created=0
server_created=0
client_pid=
server_pid=
captures=()

[[ $EUID == 0 ]] || { echo "Run this script with sudo for netns, tc and tcpdump." >&2; exit 1; }
[[ -x "$exe" ]] || { echo "Missing $exe. Run bash scripts/build.sh with the same IOT_BUILD_DIR." >&2; exit 1; }
exe=$(realpath -e -- "$exe")
for command in ip tc ping tcpdump openssl python3 git; do
    command -v "$command" >/dev/null || { echo "Missing runtime tool: $command" >&2; exit 1; }
done

mkdir -p -- "$(dirname -- "$out")"
mkdir -- "$out"
echo "Stage $stage artifacts: $out"

stop_captures() {
    local pid
    for pid in "${captures[@]}"; do kill -INT "$pid" 2>/dev/null || true; done
    for pid in "${captures[@]}"; do wait "$pid" || true; done
    captures=()
}

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$client_pid" ]]; then kill "$client_pid" 2>/dev/null || true; wait "$client_pid" || true; fi
    if [[ -n "$server_pid" ]]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" || true; fi
    stop_captures
    if ((server_created)); then ip netns del "$server_ns" || status=1; fi
    if ((client_created)); then ip netns del "$client_ns" || status=1; fi
    rm -f -- "$out/lab-key.pem" "$out/lab-cert.pem"
    if [[ -n "${SUDO_UID:-}" && -n "${SUDO_GID:-}" ]]; then
        find "$out" -maxdepth 1 -exec chown --no-dereference "$SUDO_UID:$SUDO_GID" {} +
    fi
    if ((status)); then echo "Stage $stage failed. Inspect $out" >&2; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

wait_log() {
    local file=$1 pattern=$2 pid=$3
    local until=$((SECONDS + 45))
    until [[ -f "$file" ]] && grep -q -- "$pattern" "$file"; do
        if ! kill -0 "$pid" 2>/dev/null || ((SECONDS >= until)); then
            echo "Timed out or process exited waiting for $pattern in $file" >&2
            return 1
        fi
        sleep 0.05
    done
}

source "$root/network/topology/dual-veth.sh"
source "$root/network/scenarios/performance-reversal.sh"
setup_topology
if $windowed && [[ -n ${IOT_NETEM_TC:-} ]]; then
    for ns in "$client_ns" "$server_ns"; do
        ip netns exec "$ns" sysctl -qw net.ipv6.conf.all.disable_ipv6=1
    done
fi
{
    if $windowed; then uname -srvm; else uname -a; fi
    cat /etc/os-release
    ip -Version
    tc -Version
    if $windowed && [[ -n ${IOT_NETEM_TC:-} ]]; then
        "$IOT_NETEM_TC" -Version
        sha256sum "$IOT_NETEM_TC"
    fi
    tcpdump --version
    sha256sum "$exe"
    local_xquic="$root/third_party/xquic"
    local_tls="$(dirname -- "$(dirname -- "$exe")")/boringssl"
    xquic_rev=$(git -c safe.directory="$local_xquic" -C "$local_xquic" rev-parse HEAD)
    tls_rev=$(git -c safe.directory="$local_tls" -C "$local_tls" rev-parse HEAD)
    printf 'xquic=%s\nboringssl=%s\n' "$xquic_rev" "$tls_rev"
    if [[ $stage == 4 || $stage == 5 ]] || $windowed; then
        printf 'stream_pto_patch=%s\n' "$(sha256sum < "$root/patches/xquic/stream-pto-scheduler.patch" | cut -d ' ' -f 1)"
    fi
    if [[ $stage == 91 || $stage == 92 ]]; then
        sha256sum "$root/patches/xquic/stage91-integrity-trace.patch"
    fi
    if [[ $stage == 92 ]]; then
        sha256sum "$root/patches/xquic/stage92-measurement.patch"
    fi
    for ns in "$client_ns" "$server_ns"; do
        echo "namespace=$ns"
        ip -n "$ns" -brief addr
        ip -n "$ns" route
    done
} > "$out/environment.txt"

measure_profile() {
    local phase=$1 ns link
    apply_profile "$phase"
    {
        date -u --iso-8601=ns
        for ns in "$client_ns" "$server_ns"; do
            for link in a b; do
                echo "namespace=$ns interface=iot-$link"
                ip netns exec "$ns" tc -s qdisc show dev "iot-$link"
            done
        done
    } > "$out/netem-phase-$phase.txt"
    ip netns exec "$client_ns" ping -n -I iot-a -c 5 -i 0.1 -W 1 10.47.1.2 > "$out/ping-A-$phase.txt"
    ip netns exec "$client_ns" ping -n -I iot-b -c 5 -i 0.1 -W 1 10.47.2.2 > "$out/ping-B-$phase.txt"
}

if [[ $stage == 5 ]] || $windowed; then
    cp -- "$5" "$out/case.json"
    python3 "$root/network/scenarios/stage5.py" initial "$out" "$client_ns" "$server_ns"
else
    measure_profile 1
fi
server_role=server
client_role=client
client_args=()
if [[ $stage != 1 ]]; then
    server_role=iot-server
    client_role=iot-client
    seed=${IOT_SEED:-17}
    if [[ $stage == 2 ]]; then
        python3 "$root/src/workload/generate.py" "$out/workload.bin" --seed "$seed"
    else
        cp -- "$3" "$out/workload.bin"
        printf 'policy=%s\n' "$2" >> "$out/environment.txt"
    fi
    printf 'seed=%s\n' "$seed" >> "$out/environment.txt"
    sha256sum "$out/workload.bin" >> "$out/environment.txt"
    client_args+=("$out/workload.bin")
    if [[ $stage != 2 ]]; then client_args+=("$2"); fi
    if [[ $stage == 4 ]] || { $windowed && [[ $2 == proposed-app ]]; }; then client_args+=("$4"); fi
    if { [[ $stage == 5 ]] || $windowed; } && [[ $2 == proposed-cross ]]; then client_args+=("$4" "$6"); fi
fi
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 \
    -subj "/CN=iot-stage$stage" -keyout "$out/lab-key.pem" -out "$out/lab-cert.pem" 2> "$out/openssl.log"

snaplen=96
capture_filter='udp and port 4433 and port 4434'
if [[ $stage == 91 || $stage == 92 ]]; then snaplen=0; fi
if [[ $stage == 91 && -n ${IOT_STAGE91_MIGRATION:-} ]]; then
    capture_filter='udp and port 4433 and (port 4434 or port 4435)'
fi
for link in a b; do
    if [[ $stage == 92 && -z ${IOT_STAGE92_CAPTURE:-} ]]; then break; fi
    capture_prefix=()
    if [[ $stage == 92 && -n ${IOT_STAGE92R:-} ]]; then
        capture_prefix=(python3 -B "$root/scripts/capture_usage.py" "$out/capture-server-$link.json")
    fi
    "${capture_prefix[@]}" ip netns exec "$server_ns" tcpdump -nn -i "iot-$link" -s "$snaplen" -U --immediate-mode -B 4096 -Z root \
        -w "$out/link-$link.pcap" "$capture_filter" \
        2> "$out/tcpdump-$link.log" &
    captures+=("$!")
    wait_log "$out/tcpdump-$link.log" 'listening on' "$!"
    if [[ $stage == 91 || $stage == 92 ]]; then
        if [[ $stage == 92 && -n ${IOT_STAGE92R:-} ]]; then
            capture_prefix=(python3 -B "$root/scripts/capture_usage.py" "$out/capture-client-$link.json")
        fi
        "${capture_prefix[@]}" ip netns exec "$client_ns" tcpdump -nn -i "iot-$link" -s 0 -U --immediate-mode -B 4096 -Z root \
            -w "$out/client-link-$link.pcap" "$capture_filter" \
            2> "$out/client-tcpdump-$link.log" &
        captures+=("$!")
        wait_log "$out/client-tcpdump-$link.log" 'listening on' "$!"
    fi
done

ip netns exec "$server_ns" "$exe" "$server_role" "$out/server.xquic.log" "$out/lab-cert.pem" "$out/lab-key.pem" \
    > "$out/server.log" 2> "$out/server.stderr" &
server_pid=$!
wait_log "$out/server.log" 'event=ready role=server' "$server_pid"
ip netns exec "$client_ns" "$exe" "$client_role" "$out/client.xquic.log" "${client_args[@]}" \
    > "$out/client.log" 2> "$out/client.stderr" &
client_pid=$!

if [[ $stage == 1 ]]; then
    wait_log "$out/client.log" 'event=phase_acked phase=1 ' "$client_pid"
    measure_profile 2
    kill -USR1 "$client_pid"
fi
if [[ $stage == 4 ]]; then
    wait_log "$out/client.log" 'event=iot_start ' "$client_pid"
    ip netns exec "$client_ns" tc qdisc replace dev iot-a root netem limit 1000 delay 4ms rate 30mbit loss 100%
    python3 -c 'import time; print("outage_us", time.monotonic_ns() // 1000)' > "$out/transition.txt"
    ip netns exec "$client_ns" tc qdisc show dev iot-a >> "$out/transition.txt"
    sleep 0.9
    python3 -c 'import time; print("restore_begin_us", time.monotonic_ns() // 1000)' >> "$out/transition.txt"
    measure_profile 2
fi
if [[ $stage == 5 ]] || $windowed; then
    wait_log "$out/client.log" 'event=iot_start ' "$client_pid"
    python3 "$root/network/scenarios/stage5.py" timeline "$out" "$client_ns" "$server_ns"
fi
if $windowed; then
    # The fixed-window verifier distinguishes a lost close or unfinished stream after cutoff.
    wait "$client_pid" || echo "Client exited with status $?"
    client_pid=
    wait "$server_pid" || echo "Receiver exited with status $? after the client closed"
else
    wait "$client_pid"
    client_pid=
    wait "$server_pid"
fi
server_pid=
stop_captures
python3 "$root/evaluation/verify_stage$stage.py" "$out" | tee "$out/summary.json"
