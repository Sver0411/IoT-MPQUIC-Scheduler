# Stage 7 scheduling and measurement specification

## Application and transport interfaces

The application queue admits records at their replay release times. FIFO preserves order and keeps pending state records. Proposed-App replaces a pending value by the newest `(generation time, sequence)` for its sensor. It discards pending state already known to be obsolete through receiver ACKs. It categorizes remaining candidates as normal state, recovery state or event.

Let `sat(x) = x/(1+x)`, `H = 100,000 µs` and `s` denote remaining budget. Event budget is deadline minus decision time. State budget is the smaller of that value and the maximum age less the candidate's current age. Define `G` and `X` as the saturated potential age improvement and age excess from the latest ACK. A new sensor starts with `G = X = 1`. Events use `G = X = 0`.

For Proposed-App, normal state and event score is the sum of saturated priority, weighted saturated event severity, `G`, weighted saturated waiting time and `H/(H + max(0,s))`. Recovery state uses priority, `G`, `X` and weighted waiting time. The existing recovery quota, event quota and tie breaks still apply. A pure application decision accepts no transport input. Transport feedback through actual application ACKs remains part of this closed loop. In the factorial design the path layer can change those ACK timings.

After a record is selected, Proposed-Path estimates completion on active, unfrozen paths and chooses maximum probability, with smaller estimated completion time breaking ties. The transport binds the selected record's new stream before the first write. FIFO with Proposed-Path goes through the same binding callback as Proposed-App with Proposed-Path. MinRTT uses the original XQUIC callback for client data, control frames and repair. A server sends ACK streams with native MinRTT in every cell.

## Legacy joint policy and repair

`proposed-cross` and `proposed-strict` retain the Stage 4 joint ranking. In the normal class that score uses `P · (1 + H/(H + max(0,s)))` in place of the application-only urgency term. Recovery scoring does not include this factor. The `no-probability` ablation removes it while retaining the same initial path rule. This ablation differs from Proposed-App, which still uses its path-independent urgency term.

Strict affinity applies to initial and repair transmissions. Cross adds a repair rule. It can try an alternative if the original path is unavailable, congestion-window blocked, has an outstanding PTO or is estimated to exceed the remaining budget. The alternative must be available and able to send within its congestion window. Decisions are checked against actual XQUIC transmissions. Offset repetition is a practical packet-level overhead measure and does not reconstruct partial overlap across repacketized STREAM frames.

## Tunable parameters

| Parameter | Default | Sensitivity values | Scope |
| --- | ---: | ---: | --- |
| Recovery K | 2 | 1, 4 | Normal state submissions required between preferential recovery opportunities |
| Event burst limit | 3 | 1, 6 | Maximum consecutive event submissions while state waits |
| Severity weight | 2 | 1, 4 | Event severity score |
| Waiting weight | 1 | 0.5, 2 | Waiting score |
| Slack margin (µs) | 0 | 25,000, 50,000 | Initial path model subtracts margin from its budget. Receiver deadlines and repair conditions retain their original values |
| Loss multiplier | 1 | 0.5, 2 | Initial path estimate scales the smoothed recent loss, capped at 0.95 |

The selected-path prediction retains the Stage 4 formula. The queue term is scheduled bytes plus bytes in flight. Stage 7 records both components separately. PTO adds two SRTTs to estimated duration, and the independent packet-loss approximation discounts the success estimate by the number of 1,200-byte segments. Prediction outcomes are server FIN by the absolute deadline and by the effective state freshness budget. An incomplete record is a failure for both targets. This association is available only for the selected path. Offline calibration uses disjoint seeds and the fitted mapping is kept out of the online policy.

## Receiver and experiment window

Events are scoped to `(source, sequence)`. A repeat ID within the same source remains a duplicate. A sensor's receiver value advances only when the received `(generation time, sequence)` is newer. A state received with FIN after its deadline still updates the value if it is newer. Receiver ACKs return the accepted snapshot and absolute arrival time.

Stage 7 uses `[origin, origin + 4 s)` for all outcome metrics. The client stops submissions and begins closing at the horizon, including runs with outstanding work. Pending, replaced, obsolete, unfinished, stale and duplicate records have distinct output fields. A replay record scheduled outside the horizon stays outside generation denominators. Legacy entry points retain their existing completion rule.
