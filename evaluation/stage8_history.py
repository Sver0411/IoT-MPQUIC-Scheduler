"""Re-evaluate the seven Stage 7 filesystem-timed runs without changing them."""

import argparse
import csv
import json
import math
from pathlib import Path

from stage7 import extract
from verify_stage2 import read_events, require
from verify_stage7 import verify_run

TIMED = {
    ("loss", 7106, "D"), ("loss", 7106, "B"), ("loss", 7110, "D"),
    ("repair", 7105, "Cross"), ("repair", 7101, "Cross"), ("repair", 7107, "Cross"),
    ("ablation", 7103, "no-replace"),
}


def audit(root):
    with (root / "runs.csv").open(newline="", encoding="utf-8") as file:
        special = [row for row in csv.DictReader(file)
                   if row.get("time_source") == "case-file-and-terminal-log-mtimes"]
    require({(row["suite"], int(row["seed"]), row["cell"]) for row in special} == TIMED and
            len(special) == 7, "Stage 7 special-run identities differ")
    findings = []
    for row in special:
        raw = root / row["raw_run"]
        case = json.loads((raw / "case.json").read_text())
        evidence = verify_run(raw)
        require(evidence == json.loads((raw / "summary.json").read_text()), f"{raw.name}: saved evidence differs")
        client, server = (read_events(raw / f"{role}.log") for role in ("client", "server"))
        origin = int(client["iot_start"][0]["origin"])
        cutoff = origin + case["horizon_us"]
        window = client["iot_window_end"][0]
        require(int(window["t_us"]) >= cutoff, f"{raw.name}: cutoff marker too early")
        endpoints = {}
        for role, trace in (("client", client), ("server", server)):
            closing = trace["conn_closed"][0]
            end = trace["exit"][0]
            exceptions = [*trace.get("timeout", []),
                          *(ev for ev in trace.get("stream_closed", []) if ev["fin"] == "0")]
            require(closing["error"] == "0" and int(closing["t_us"]) >= cutoff and
                    int(end["t_us"]) >= int(closing["t_us"]) and not trace.get("invalid_stream") and
                    all(int(ev["t_us"]) >= cutoff for ev in exceptions), f"{raw.name}: {role} cutoff evidence differs")
            endpoints[role] = {
                "closed_after_cutoff_us": int(closing["t_us"]) - cutoff,
                "exit_after_cutoff_us": int(end["t_us"]) - cutoff,
                "exit_ok": int(end["ok"]),
                "exception_evidence": [{"event": ev["event"], "stream_id": ev.get("stream_id"),
                                        "after_cutoff_us": int(ev["t_us"]) - cutoff} for ev in exceptions],
                "stderr": (raw / f"{role}.stderr").read_text().strip(),
            }

        metrics, records, _, _, _ = extract(raw, {"scenario": row["scenario"], "horizon_us": case["horizon_us"]})
        compared = 0
        for field, actual in metrics.items():
            if field in ("scenario", "horizon_us"):
                continue
            original = row[field]
            require((actual is None and original == "") or (isinstance(actual, (int, float)) and original and
                    math.isclose(actual, float(original), rel_tol=1e-10, abs_tol=1e-10)),
                    f"{raw.name}: archived metric differs: {field}")
            compared += 1
        log = root / "raw" / raw.parent.parent.name / "execution" / f"{raw.name}.log"
        if row["suite"] == "ablation":
            require("res: command not found" in log.read_text() and
                    all(endpoint["exit_ok"] for endpoint in endpoints.values()), "missing shell-only evidence")
        findings.append({
            "raw_run": row["raw_run"], "suite": row["suite"], "seed": int(row["seed"]), "cell": row["cell"],
            "start_utc": row["start_utc"], "end_utc": row["end_utc"], "time_source": row["time_source"],
            "window_end_lag_us": int(window["t_us"]) - cutoff, "endpoints": endpoints,
            "metrics_recomputed_equal": compared,
            "record_fin_after_window": sum(r["actual_completion_us"] is not None and
                                           not r["complete_in_window"] for r in records),
            "submitted_unfinished": metrics["unfinished"], "lifetime_cpu_includes_teardown": True,
            "recommendation": "retain-window-results-and-flag-wall-clock-and-lifetime-cpu",
        })
    return {"status": "passed", "source": "Stage 7 archive", "findings": findings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage7", type=Path)
    print(json.dumps(audit(parser.parse_args().stage7), indent=2))
