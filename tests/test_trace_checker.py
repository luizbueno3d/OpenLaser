"""scripts/check_usb_trace.py on traces recorded from the fake board (no USB)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, DOT, FakeLmcBoard, VirtualClock, fake_board, params

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_usb_trace.py"
spec = importlib.util.spec_from_file_location("check_usb_trace", SCRIPT)
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


class TraceCheckerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "trace.jsonl"
        patcher = mock.patch.dict(os.environ, {"OPENMOPA_USB_TRACE": str(self.path)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def findings(self):
        return checker.check(checker.load(self.path))

    def test_completed_jobs_pass(self):
        with fake_board(FakeLmcBoard(busy_s=0.05)):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
            live.mark_polylines(CFG, params(power=1.0), DOT, arm=True, confirm="ARM")
        findings = self.findings()
        self.assertEqual([v for v, _ in findings if v == "FAIL"], [])
        self.assertEqual(sum(1 for v, m in findings if v == "PASS" and "MO closed after BUSY" in m), 2)
        self.assertEqual(sum(1 for v, m in findings if v == "PASS" and "starts with listJumpTo" in m), 2)

    def test_mo_closed_while_busy_fails(self):
        with fake_board(FakeLmcBoard()):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        records = checker.load(self.path)
        execute = next(i for i, r in enumerate(records) if r.get("op") == "ExecuteList")
        mo_close = next(i for i, r in enumerate(records)
                        if i > execute and r.get("op") == "Fiber_SetMo" and r["words"][1] == 0)
        last_read = max(i for i in range(mo_close) if records[i]["dir"] == "R")
        records[last_read]["status"] = "0x0024"  # READY | BUSY
        self.assertIn("FAIL", [v for v, _ in checker.check(records)])

    def test_frame_loop_stop_latency_is_reported(self):
        loop = live.FrameLoop()
        with fake_board(FakeLmcBoard(capacity=4, drain_s=0.2)):
            loop.start(CFG, params(), BOX10)
            time.sleep(0.2)
            loop.stop()
        stops = [(v, m) for v, m in self.findings() if m.startswith("stop:")]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0][0], "PASS")

    def test_watchdog_fault_is_reported(self):
        clock = VirtualClock()
        with fake_board(FakeLmcBoard(clock, ready=False), virtual_clock=clock):
            with self.assertRaises(live.BoardWaitTimeout):
                live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        self.assertTrue(any(v == "INFO" and "not READY" in m for v, m in self.findings()))

    def test_mid_session_reconnect_fails(self):
        with fake_board(FakeLmcBoard()):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        records = checker.load(self.path)
        at = next(i for i, r in enumerate(records) if r.get("op") == "ResetList")
        storm = dict(records[at], dir="LOG", message="Attempting connection to USB.")
        storm.pop("op"), storm.pop("words")
        self.assertIn("FAIL", [v for v, _ in checker.check(records[:at] + [storm] + records[at:])])

    def test_cli_exit_codes(self):
        with fake_board(FakeLmcBoard()):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(checker.main(["check", str(self.path)]), 0)
        records = checker.load(self.path)
        for record in records:
            if record.get("dir") == "R":
                record["status"] = "0x0024"
        self.path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(checker.main(["check", str(self.path)]), 1)
        self.assertIn("1 FAIL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
