#!/usr/bin/env bash
# Sourced by the Stage 1 runner after it has registered its cleanup handler.

setup_topology() {
    ip netns add "$client_ns"
    client_created=1
    ip netns add "$server_ns"
    server_created=1
    ip -n "$client_ns" link set lo up
    ip -n "$server_ns" link set lo up

    local link subnet
    for link in a b; do
        subnet=1
        [[ "$link" == a ]] || subnet=2
        ip -n "$client_ns" link add "iot-$link" type veth peer name "iot-$link" netns "$server_ns"
        ip -n "$client_ns" addr add "10.47.$subnet.1/30" dev "iot-$link"
        ip -n "$server_ns" addr add "10.47.$subnet.2/30" dev "iot-$link"
        ip -n "$client_ns" link set "iot-$link" up
        ip -n "$server_ns" link set "iot-$link" up
    done
}
