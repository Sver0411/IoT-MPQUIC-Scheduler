"""Replay application attempts and transport path state from integrity events."""

from collections import defaultdict

from verify_stage2 import require


def sequence(rows, role):
    require(bool(rows) and rows[0]["event"] == "iot91_open" and rows[0]["role"] == role,
            f"{role}: missing trace start")
    require(rows[-1]["event"] == "exit", f"{role}: missing trace end")
    previous = 0
    for index, row in enumerate(rows, 1):
        timestamp = int(row["t_us"])
        require(int(row["audit_seq"]) == index, f"{role}: trace sequence gap or reorder at {index}")
        require(timestamp >= previous, f"{role}: timestamp regressed at {index}")
        previous = timestamp


class Paths:
    """XQUIC transport state, application status and rebinding are separate axes."""

    def __init__(self):
        self.state = defaultdict(int)
        self.status = defaultdict(lambda: 2)
        self.migration = {}
        self.epoch = defaultdict(int)
        self.transitions = defaultdict(int)
        self.commands = {}
        self.observed_commands = []

    def available(self, path):
        return self.state[path] == 2 and self.status[path] != 3

    def eligible(self):
        return {path for path in self.state if self.available(path)}

    def observe(self, row):
        kind = row["kind"]
        path = row["path"]
        if kind == "path_state":
            old, new = int(row["before"]), int(row["after"])
            allowed = {0: {1, 2, 3, 4}, 1: {2, 3, 4}, 2: {3, 4}, 3: {4}}
            require(self.state[path] == old and new in allowed.get(old, ()),
                    f"path {path}: invalid transport transition {old}->{new}")
            self.state[path] = new
            if new == 4:
                self.transitions["removed"] += 1
            if new == 2:
                self.transitions["available"] += 1
        elif kind == "path_status":
            old, new = int(row["before"]), int(row["after"])
            require(self.status[path] == old and old != new and new in (1, 2, 3) and self.state[path] < 3,
                    f"path {path}: invalid freeze/recovery transition {old}->{new}")
            self.status[path] = new
            if new == 3:
                self.transitions["frozen"] += 1
            elif old == 3:
                self.transitions["recovered"] += 1
        elif kind == "migration_start":
            require(self.state[path] == 2 and path not in self.migration, f"path {path}: invalid migration start")
            self.migration[path] = "validating"
            self.transitions["migration_started"] += 1
        elif kind == "migration_commit":
            require(self.state[path] == 2 and self.migration.get(path) == "validating",
                    f"path {path}: migration without validation")
            self.migration[path] = "committed"
            self.transitions["migrated"] += 1
        elif kind == "migration_end":
            require(path in self.migration, f"path {path}: migration end without start")
            if self.migration.pop(path) == "validating":
                self.transitions["migration_aborted"] += 1
        else:
            raise ValueError(f"unknown path event {kind}")
        self.epoch[path] += 1

    def command(self, row):
        path, action = row["path"], row["action"]
        if row["event"] == "iot91_path_command":
            require(path not in self.commands and self.state[path] == 2 and
                    ((action == "recover" and self.status[path] == 3) or
                     (action in ("freeze", "remove") and self.available(path))),
                    f"path {path}: illegal {action} command")
            self.commands[path] = action, self.epoch[path]
        else:
            require(path in self.commands, f"path {path}: result without command")
            pending, epoch = self.commands.pop(path)
            expected = {"freeze": self.status[path] == 3, "recover": self.available(path),
                        "remove": self.state[path] >= 3}
            require(action == pending and row["rc"] == "0" and expected[action] and self.epoch[path] > epoch,
                    f"path {path}: {action} lacks an observed successful transition")
            self.observed_commands.append(action)

    def choose(self, row, home, cross):
        chosen, repair, cause = row["path"], row["repair"] == "1", row["cause"]
        require(row["home"] == home and self.available(chosen), "choice contradicts binding or path state")
        if chosen != home:
            ready, pto = row["home_ready"] == "1", int(row["home_pto"])
            allowed = {
                "unavailable": not self.available(home) and not ready,
                "cwnd": self.available(home) and not ready,
                "pto": self.available(home) and ready and pto > 0,
                "budget": self.available(home) and ready and not pto and
                          float(row["home_time"]) >= int(row["slack"]),
            }
            require(cross and repair and allowed.get(cause, False), "cross repair has no allowed cause")
        return self.epoch[chosen]


class Attempts:
    """Cross-check actual XQUIC stream opens against each application opportunity."""

    def __init__(self):
        self.count = self.blocked = 0
        self.active = None
        self.decision = None
        self.opened = None
        self.result = None
        self.bound = {}
        self.submitted = {}

    def observe(self, row):
        event = row["event"]
        if event == "iot91_attempt":
            self.count += 1
            require(self.active is None and int(row["attempt"]) == self.count, "missing or duplicate attempt")
            self.active = row
        elif event == "iot_app_decision":
            require(self.active is not None and self.decision is None and self.active["id"] == row["id"],
                    "decision outside its attempt or repeated")
            self.decision = row
        elif event == "stream_open" and row["direction"] == "tx":
            require(self.active is not None and self.decision is not None and self.opened is None and
                    self.result is None, "stream opened outside a decision")
            self.opened = row["stream_id"]
        elif event == "iot91_create":
            require(self.active is not None and self.decision is not None and self.result is None and
                    all(row[k] == self.active[k] for k in ("attempt", "id")), "create outcome lacks its decision")
            if row["outcome"] == "blocked":
                require(self.opened is None and row["stream"] == "0", "blocked attempt opened a stream")
                self.blocked += 1
                self.finish()
            else:
                require(row["outcome"] == "created" and row["stream"] == self.opened and int(self.opened) % 4 == 2,
                        "created stream does not match the XQUIC open")
                self.result = row
        elif event == "iot91_bind":
            stream = row["stream"]
            require(self.result is not None and stream not in self.bound and
                    all(row[k] == self.result[k] for k in ("attempt", "id", "stream")),
                    "binding does not follow successful creation")
            self.bound[stream] = row
        elif event == "iot_submit":
            stream = row["stream_id"]
            require(self.result is not None and self.result["id"] == row["id"] and
                    self.result["stream"] == stream and stream in self.bound and stream not in self.submitted,
                    "submission lacks successful creation and unique binding")
            self.submitted[stream] = row
            self.finish()

    def finish(self):
        self.active = self.decision = self.opened = self.result = None

    def totals(self, row):
        require(self.active is None and self.count == int(row["attempts"]) and
                self.blocked == int(row["blocked"]) and self.count == self.blocked + len(self.submitted),
                "attempt totals or final state disagree")
