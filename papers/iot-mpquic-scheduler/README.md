# IoT-MPQUIC-Scheduler Manuscript (*Computer Communications*, Elsevier)

Standalone Elsevier `elsarticle` (`preprint,12pt,number`, `elsarticle-num`) manuscript documenting real C `XQUIC` multipath scheduling (`FAP-MPQUIC`) against `MinRTT`, `Fixed-MinRTT`, `ECF`, `BLEST`, `Prob-Only`, and internal ablations (`FAP-NoProbe`, `FAP-Cross`) across 1,596+ dual-path Linux `netns` + `tc-netem` connections, including mechanistic curve explanations and a cross-regime operating-envelope synthesis.

## Build Instructions

```bash
python3 papers/iot-mpquic-scheduler/build_figures.py
cd papers/iot-mpquic-scheduler
tectonic -X compile manuscript.tex
```

Outputs:
- `manuscript.pdf` — Compiled 18-page Elsevier preprint vector PDF (with line numbers, Highlights, 5 native figures, 3 tables including SOTA comparison and cross-regime operating-envelope synthesis, 2 theoretical propositions with proofs, Elsevier end-of-manuscript declarations, and 31 verified peer-reviewed citations).
- `highlights.txt` — Elsevier submission highlights (5 bullets, each <= 80 characters).
- `graphical_abstract.png` / `graphical_abstract.pdf` — 3-panel summary graphic.
