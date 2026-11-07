#!/usr/bin/env bash
# Apply the same egress model at both ends. Delay is one-way.

apply_profile() {
    local phase=$1 ns
    for ns in "$client_ns" "$server_ns"; do
        if [[ "$phase" == 1 ]]; then
            ip netns exec "$ns" tc qdisc replace dev iot-a root netem limit 1000 delay 4ms rate 30mbit
            ip netns exec "$ns" tc qdisc replace dev iot-b root netem limit 1000 delay 24ms rate 12mbit
        else
            ip netns exec "$ns" tc qdisc replace dev iot-a root netem limit 1000 delay 45ms rate 6mbit
            ip netns exec "$ns" tc qdisc replace dev iot-b root netem limit 1000 delay 5ms rate 30mbit
        fi
    done
}
