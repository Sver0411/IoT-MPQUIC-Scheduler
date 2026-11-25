# IoT-MPQUIC-Scheduler IEEE Manuscript

Standalone 8-page IEEE Transactions/Journal two-column manuscript (`IEEEtran`) documenting real C XQUIC multipath scheduling (`FAP-MPQUIC`) against MinRTT, Fixed MinRTT, ECF, BLEST, Stage 8 Probability, and ACK-Adaptive across 1,596+ audited dual-path Linux `netns` + `tc/netem` connections.

## Build Instructions

```bash
python3 papers/iot-mpquic-scheduler/build_figures.py
cd papers/iot-mpquic-scheduler
tectonic manuscript.tex
```

Outputs:
- `manuscript.pdf` — 8-page IEEEtran PDF with 5 native figures (1 TikZ + 4 7.16-in vector PDFs), 2 theoretical propositions with proofs, and 29 verified peer-reviewed citations.
- `graphical_abstract.png` / `graphical_abstract.pdf` — 3-panel summary graphic.
