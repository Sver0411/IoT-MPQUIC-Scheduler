"""Stop tcpdump on request and record its own process resource use."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

output, *command = sys.argv[1:]
with subprocess.Popen(command) as capture:
    def stop(signum, _):
        try:
            os.kill(capture.pid, signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    _, status, usage = os.wait4(capture.pid, 0)
    capture.returncode = os.waitstatus_to_exitcode(status)
    Path(output).write_text(json.dumps({"cpu_us": round((usage.ru_utime + usage.ru_stime) * 1e6),
                                       "peak_rss_kib": usage.ru_maxrss, "exit_code": capture.returncode}) + "\n")
    raise SystemExit(capture.returncode)
