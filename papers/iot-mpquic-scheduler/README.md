# IoT-MPQUIC-Scheduler Manuscript (Computer Networks, Elsevier)

Standalone two-column manuscript documenting real C XQUIC multipath scheduling (`FAP-MPQUIC`) against MinRTT, Fixed-MinRTT, ECF, BLEST, Prob-Only, and internal ablations (`FAP-NoProbe`, `FAP-Cross`) across 1,596+ dual-path Linux `netns` + `tc-netem` connections.

## Build Instructions

```bash
python3 papers/iot-mpquic-scheduler/build_figures.py
cd papers/iot-mpquic-scheduler
tectonic manuscript.tex
```

Outputs:
- `manuscript.pdf` — Compiled vector PDF with 5 native figures (1 TikZ + 4 7.16-in vector PDFs), 2 theoretical propositions with proofs, Elsevier end-of-manuscript declarations, and 31 verified peer-reviewed citations.
- `highlights.txt` — Elsevier submission highlights (4 bullets, each <= 85 characters).
- `graphical_abstract.png` / `graphical_abstract.pdf` — 3-panel summary graphic.
