# Stage 7 experimental plan

## Pre-change audit

Baseline Git commit `ab12f7cf828da9e64065dd6791ee67a2cf91728a`. The Stage 6 tree is `aeaa46f68fad7446c89650ba33875ad490b42e34` and the Stage 5 archive tree is `e276386f9546f403a388fa6199c6e06a0724c78a`. All 733 files in the archived Stage 6 source checksum list passed SHA-256 verification. The seven baseline CTests passed before changes.

| Input | Fingerprint |
| --- | --- |
| XQUIC | `dffd32c42a511edae544b87169976516b7c4cbfa` |
| BoringSSL | `b69a6175320af9b6fca7295fdbb4fb130f4804a2` |
| STREAM PTO patch SHA-256 | `2f1d8d47a4adcceb34f201fae129730ec80417d3a5a3ec22f6285825c5e3007bd` |
| Cached endpoint SHA-256 | `e5da22d0276f52b253fb6c24751434a39bbf3e510bd0dbe1b5d440e5f09298d7` |
| Stage 5 configuration SHA-256 | `eb9131d68f01ec84a776861add36f446de85bc82d39ab580a4a9b2671b6d661f` |

The pinned endpoint admits all released inputs, selects one pending entry, creates a unidirectional stream, commits selection state and calls `xqc_stream_send`. Receiver FIN produces an application ACK. The legacy Proposed implementation estimates all available paths for each eligible record and uses the winning probability in its application score. One policy flag also installs the affinity callback, making the historical FIFO-versus-Proposed contrast a bundled intervention. Initial affinity runs only on client record streams. The PTO patch opts those streams into the custom callback for repairs. Control frames and server ACK streams use upstream MinRTT.

The receiver historically keys events by sequence alone. Scope event identity to `(source, serial)` and retain legacy archive validation with its historical assumptions. Stage 6 AoI starts each source at generation time zero and integrates arrival order. Stage 7 will report observed receiver age after the first update with coverage, plus unknown-or-over-age time on the fixed full window.

## Planned contrasts

| Cell | Application | Path | Repair |
| --- | --- | --- | --- |
| A | FIFO | Native MinRTT | Native XQUIC |
| B | Proposed-App | Native MinRTT | Native XQUIC |
| C | FIFO | Proposed-Path | Strict affinity |
| D | Proposed-App | Proposed-Path | Strict affinity |

Proposed-App uses record metadata, receiver feedback and application-only urgency. It can pick a record without path samples. Proposed-Path estimates one already-selected record and binds its stream. MinRTT can spread a stream across paths, so the transport contrast estimates the effect of choosing and binding a home path together. It includes any difference from native retransmission induced by strict affinity. Study the added cross-path repair rule separately with the same bound transport in both arms. Retain the legacy joint algorithm and compare probability weighting against an otherwise identical joint-policy ablation.

Per seed, estimate application effects B−A and D−C, path effects C−A and D−B, interaction D−B−C+A, and the combined contrast D−A. Use whole-seed paired bootstrap intervals and report each scenario separately. Compare five baselines under high mixed load. For each ablation or sensitivity contrast vary one mechanism or parameter against the joint reference.

## Matrix and fixed conditions

Use the historical initial links, `stable`, `reversal`, `outage-reversal`, `outage` and `repair-probe` timelines from `configs/stage5.json`. Stable and dynamic heterogeneous use balanced traffic, high-load uses congesting mixed traffic, and state-heavy and event-heavy use mixed traffic dominated by their named class. Pair by scenario and workload seed, shuffle policy order within each block, and hold CUBIC, pacing, stream credit, application concurrency, send queue, executable, patch and 4 s measurement horizon constant. Run the factorial and the repair pair on a pilot seed first. The formal factorial requires ten seeds per scenario. Extend high load with ten additional seeds. Run the baseline, repair, ablation and sensitivity comparisons in separate suites using ten seeds each. Any lower repetition count remains explicitly labeled in the report.

Pair workload bytes and directional network configuration. The installed WSL userspace reports iproute2 6.1 with no `netem seed` argument. Loss-free phases and 100% drops have deterministic configured drop decisions. For the separate 12% partial-loss factorial, verify a seed-capable `tc` by measuring repeated and different seed packet-ordinal masks in a controlled probe. A common random stream is indexed by qdisc ingress order. Algorithms can produce different packets at the same ordinal. Keep partial-loss results separate from the six deterministic configured profiles. [iproute2 netem manual](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-netem.8)

## Outcomes and verification

Record configuration, workload and network seeds, execution order, UTC start/end, kernel, qdiscs and build hashes. Keep endpoint traces, packet captures and workload bytes under the Stage 7 archive. For repairs, verify alternate-path STREAM bytes against actual XQUIC send entries, and measure completed records before restoration and latency by traffic class.

Report generated and on-time counts, useful delivery, latency quantiles conditional on complete reception, per-source receiver age and coverage, maximum-age violations, Jain delivery and freshness indices, useful goodput, UDP payload bytes, link utilization, QUIC loss, repeated STREAM offsets and process CPU. Preserve replaced, obsolete, stale, duplicate and unfinished outcomes separately. Compare the unmodified path model against both deadline and effective freshness budget. Include missing completions in outcomes and group calibration by scene, class, payload and slack. Report Brier score, equal-width-bin ECE and reliability data. Any fitted calibration must use disjoint training and test seeds. Observational predictions for chosen paths cannot establish outcomes on unchosen paths.

First validate the pilot traces and equality of each paired block. Archive new evidence independently and audit historical Git trees and file checksums again at acceptance.
