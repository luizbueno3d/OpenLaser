#!/usr/bin/env python3
"""Check an OPENMOPA_USB_TRACE file for the patched prototype's hardware session.

Reads the trace only; it never touches USB. Usage:

    .venv/bin/python scripts/check_usb_trace.py ~/openlaser-traces/hil-p.jsonl

Prints one line per finding and exits 1 if any check FAILs:

* job end:     each list's Fiber_SetMo(0) came after the board reported BUSY clear
* first move:  each list's first motion command is a JumpTo
* stop:        time from a Stop request to StopExecute (target <= 0.25 s)
* faults:      watchdog stops ("board not READY/idle within N s")
* USB resets:  reconnects in the middle of a session (the reset storm)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BUSY = 0x04
STOP_TARGET_S = 0.25


def load(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def status_word(record: dict) -> int | None:
    value = record.get("status")
    return int(value, 16) if isinstance(value, str) else None


def is_single(record: dict, op: str, value: int | None = None) -> bool:
    if record.get("dir") != "W" or record.get("op") != op:
        return False
    return value is None or record["words"][1] == value


def check(records: list[dict]) -> list[tuple[str, str]]:
    findings: list[tuple[str, str]] = []
    by_pid: dict[int, list[dict]] = {}
    for record in records:
        by_pid.setdefault(record.get("pid", 0), []).append(record)

    for pid, stream in by_pid.items():
        aborting = False
        executing_since: float | None = None
        last_status: int | None = None
        list_open = False
        session_started = False
        # galvoplotter retries a failed transfer by closing (USB reset) and
        # reopening without re-initialising; a new session re-initialises.
        reopened: dict | None = None
        for record in stream:
            direction = record.get("dir")
            if reopened is not None and direction in ("W", "R"):
                findings.append(("FAIL", f"pid {pid}: USB reset and reconnect mid-session at {reopened['ts']}"))
                reopened = None
            if direction == "R":
                last_status = status_word(record)
            elif direction == "LOG":
                message = str(record.get("message", ""))
                if message == "Initializing Laser":
                    session_started = True
                    reopened = None
                elif message == "Attempting connection to USB." and session_started:
                    reopened = record
            elif direction == "FAULT":
                findings.append(("INFO", f"pid {pid}: watchdog stop at {record['ts']}: {record.get('reason')}"))
            elif direction == "W":
                if is_single(record, "StopExecute"):
                    aborting = True
                    executing_since = None
                elif is_single(record, "WritePort", 0) and aborting:
                    aborting = False
                elif is_single(record, "ResetList"):
                    list_open = not aborting
                elif is_single(record, "ExecuteList") and not aborting:
                    executing_since = record["mono"]
                elif is_single(record, "Fiber_SetMo", 0) and executing_since is not None and not aborting:
                    if last_status is not None and not last_status & BUSY:
                        findings.append(("PASS", f"pid {pid}: MO closed after BUSY cleared "
                                                 f"({record['mono'] - executing_since:.2f} s after ExecuteList)"))
                    else:
                        findings.append(("FAIL", f"pid {pid}: MO closed at {record['ts']} while the board "
                                                 f"still reported {record.get('status') or 'BUSY/no reply'}"))
                    executing_since = None
                elif record.get("op") == "list" and list_open:
                    motion = [c[0] for c in record.get("cmds", []) if c[0] in ("listJumpTo", "listMarkTo")]
                    if motion:
                        verdict = "PASS" if motion[0] == "listJumpTo" else "FAIL"
                        findings.append((verdict, f"pid {pid}: list starts with {motion[0]}"))
                        list_open = False

    # Stop latency across processes: a Stop request (STOP from the server or
    # the job child) to the StopExecute that follows it.
    pending: float | None = None
    for record in sorted(records, key=lambda r: r["mono"]):
        if record.get("dir") == "STOP":
            pending = record["mono"] if pending is None else pending
        elif pending is not None and is_single(record, "StopExecute"):
            latency = record["mono"] - pending
            verdict = "PASS" if latency <= STOP_TARGET_S else "FAIL"
            findings.append((verdict, f"stop: StopExecute {latency * 1000:.0f} ms after the Stop request"))
            pending = None
    return findings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip())
        return 2
    findings = check(load(Path(argv[1]).expanduser()))
    for verdict, message in findings:
        print(f"{verdict:4}  {message}")
    failed = sum(1 for verdict, _ in findings if verdict == "FAIL")
    print(f"{len(findings)} finding(s), {failed} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
