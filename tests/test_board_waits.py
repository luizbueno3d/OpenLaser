"""B1: per-wait, progress-aware board deadlines (no 3 s cap, never proceed on expiry).

Every test runs against tests.fake_lmc.FakeLmcBoard; no USB is opened.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from galvo.consts import listJumpDelay, listJumpSpeed, listJumpTo, listMarkSpeed, listMarkTo

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, FakeLmcBoard, MARK_BOX10_LIST_SHA, VirtualClock, fake_board, params

SQUARE20 = [[[-10.0, -10.0], [10.0, -10.0], [10.0, 10.0], [-10.0, 10.0], [-10.0, -10.0]]]


def call_with_deadline(test: unittest.TestCase, fn, seconds: float = 15.0):
    """Run fn on a worker thread; fail instead of hanging the suite if it never returns."""
    outcome: dict = {}

    def run():
        try:
            outcome["result"] = fn()
        except BaseException as exc:  # handed back to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(seconds)
    test.assertFalse(worker.is_alive(), "the main thread never returned")
    return outcome.get("result"), outcome.get("error")


def mo_events(board: FakeLmcBoard, value: int) -> list[dict]:
    return [e for e in board.singles("Fiber_SetMo") if e["words"][1] == value]


def wait_until(predicate, seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class LongJobTests(unittest.TestCase):
    def test_long_job_on_a_20s_busy_board_closes_mo_only_after_busy_clears(self):
        clock = VirtualClock()
        board = FakeLmcBoard(clock, busy_s=20.0)
        lines = [[[-20.0, float(y)], [20.0, float(y)]] for y in range(-25, 25)]
        job = params(power=10.0, speed=140.0)
        with fake_board(board, virtual_clock=clock):
            estimate = live.estimate_mark_seconds(CFG, job, lines)
            result, error = call_with_deadline(
                self, lambda: live.mark_polylines(CFG, job, lines, arm=True, confirm="ARM")
            )
        self.assertIsNone(error)
        self.assertTrue(result["ok"])
        # The board runs a third slower than estimated; the budget still covers it.
        self.assertGreater(estimate, 14.0)
        self.assertLess(estimate, 17.0)
        execute_at = board.singles("ExecuteList")[0]["t"]
        first_close = [e for e in mo_events(board, 0) if e["t"] > execute_at][0]
        self.assertGreaterEqual(first_close["t"] - execute_at, 20.0)
        self.assertEqual([e for e in board.singles("StopExecute") if e["t"] < execute_at + 20.0], [])
        self.assertEqual(board.violations, [])

    def test_packet_estimates_match_the_outer_timeout_estimate(self):
        lines = [[[-20.0, float(y)], [20.0, float(y)]] for y in range(-25, 25)]
        job = params(power=10.0, speed=140.0)
        list_totals: list[float] = []
        original_reset = live.BoardWaits.reset_list

        def capture(waits):
            if waits.packets:
                list_totals.append(sum(waits.packets))
            original_reset(waits)

        board = FakeLmcBoard()
        with fake_board(board), mock.patch.object(live.BoardWaits, "reset_list", capture):
            estimate = live.estimate_mark_seconds(CFG, job, lines)
            live.mark_polylines(CFG, job, lines, arm=True, confirm="ARM")
        self.assertAlmostEqual(list_totals[0], estimate, delta=0.02 * estimate)


class StuckBoardTests(unittest.TestCase):
    def test_never_ready_board_gets_no_packet_and_mo_closes_within_2_5s(self):
        clock = VirtualClock()
        board = FakeLmcBoard(clock, ready=False)
        with fake_board(board, virtual_clock=clock):
            _, error = call_with_deadline(
                self, lambda: live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
            )
        self.assertIsInstance(error, live.BoardWaitTimeout)
        self.assertIn("not READY within 2.0 s", str(error))
        self.assertEqual(board.lists(), [])
        mo_open = mo_events(board, 1)[0]["t"]
        mo_close = [e["t"] for e in mo_events(board, 0) if e["t"] > mo_open]
        port_off = [e["t"] for e in board.singles("WritePort") if e["words"][1] == 0 and e["t"] > mo_open]
        self.assertTrue(mo_close and port_off)
        self.assertLessEqual(mo_close[0] - mo_open, 2.5)
        self.assertLessEqual(port_off[0] - mo_open, 2.5)

    def test_stop_by_user_still_releases_a_spooler_wait_within_a_poll(self):
        board = FakeLmcBoard(ready=False)
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            controller.submit(live._guard_job(controller, lambda c: c.wait_ready()))
            time.sleep(0.05)
            started = time.monotonic()
            live.stop_controller(controller)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([e for e in board.lists() if e["n_real"]], [])

    def test_a_failed_status_read_never_counts_as_ready(self):
        board = FakeLmcBoard()
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            try:
                with mock.patch.object(controller, "status", return_value=-1):
                    with self.assertRaises(live.BoardWaitTimeout):
                        controller.wait_ready()
            finally:
                controller.shutdown()


class FrameLoopWatchdogTests(unittest.TestCase):
    def test_frame_loop_runs_60s_on_a_board_that_drains_a_packet_per_second(self):
        clock = VirtualClock()
        board = FakeLmcBoard(clock, capacity=4, drain_s=1.0)
        loop = live.FrameLoop()
        with fake_board(board, virtual_clock=clock):
            loop.start(CFG, params(), SQUARE20)
            try:
                self.assertTrue(wait_until(lambda: clock.now() >= 60.0 or not loop.running, 30.0))
                status = loop.status()
            finally:
                loop.stop()
        self.assertTrue(status["running"], status)
        self.assertIsNone(status["fault"])
        self.assertGreaterEqual(len(board.lists()), 60)
        self.assertEqual(board.violations, [])

    def test_frame_loop_with_ready_stuck_is_stopped_within_its_budget(self):
        clock = VirtualClock()
        board = FakeLmcBoard(clock, capacity=4, drain_s=None)
        loop = live.FrameLoop()
        with fake_board(board, virtual_clock=clock):
            loop.start(CFG, params(), SQUARE20)
            waits = live._ACTIVE_CONTROLLER._openlaser_waits
            try:
                self.assertTrue(wait_until(lambda: not loop.running))
                self.assertTrue(wait_until(lambda: waits.shut_down and board.singles("StopExecute")))
            finally:
                loop.stop()
        status = loop.status()
        self.assertIn("not READY", status["fault"])
        last_real_packet = [e for e in board.lists() if e["n_real"]][-1]["t"]
        self.assertLessEqual(waits.fault_at - last_real_packet, waits.last_budget_s + 0.05)
        self.assertGreater(waits.last_budget_s, live.PRE_EXECUTE_WAIT_S)
        self.assertTrue([e for e in board.singles("StopExecute") if e["t"] >= waits.fault_at])
        self.assertTrue([e for e in mo_events(board, 0) if e["t"] >= waits.fault_at])
        # Nothing but the abort's empty end-of-list packet goes out after expiry.
        self.assertEqual([e for e in board.lists() if e["t"] >= waits.fault_at and e["n_real"]], [])
        self.assertEqual(board.violations, [])


class EstimateTests(unittest.TestCase):
    def test_jump_and_mark_times_follow_the_list_settings(self):
        timer = live.ListTimeEstimator(0xFFFF / 236.68, jump_mm_s=3000.0, mark_mm_s=1000.0)
        timer.command_seconds(listJumpSpeed, 830)
        timer.command_seconds(listJumpDelay, 8)
        timer.command_seconds(listMarkSpeed, 276)
        jump = timer.command_seconds(listJumpTo, 0x8000 + 8300, 0x8000)
        self.assertAlmostEqual(jump, 8300 / 830 / 1000 + 8 * live.LIST_DELAY_UNIT_S + live.LIST_COMMAND_OVERHEAD_S)
        mark = timer.command_seconds(listMarkTo, 0x8000 + 8300 + 2760, 0x8000)
        self.assertAlmostEqual(mark, 2760 / 276 / 1000 + live.LIST_COMMAND_OVERHEAD_S)

    def test_outer_timeout_is_twice_the_estimate_plus_a_minute(self):
        self.assertEqual(live.hardware_job_timeout_s(0.0), 60.0)
        self.assertEqual(live.hardware_job_timeout_s(100.0), 260.0)

    def test_frame_estimate_counts_every_retrace(self):
        with fake_board(FakeLmcBoard()):
            repeats = live.frame_once_repeats(SQUARE20, 5)
            seconds = live.estimate_frame_seconds(CFG, SQUARE20)
        self.assertGreater(repeats, 1)
        self.assertGreaterEqual(seconds, live.FRAME_ONCE_SECONDS * 0.9)


class UsbTraceTests(unittest.TestCase):
    def test_trace_records_timestamped_writes_and_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trace.jsonl"
            board = FakeLmcBoard()
            with fake_board(board), mock.patch.dict(os.environ, {"OPENMOPA_USB_TRACE": str(path)}):
                live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all("ts" in r and "mono" in r and "pid" in r for r in records))
        monos = [r["mono"] for r in records]
        self.assertEqual(monos, sorted(monos))
        writes = [r for r in records if r["dir"] == "W"]
        self.assertIn(["Fiber_SetMo", 1], [[r["op"], r["words"][1]] for r in writes if r["op"] == "Fiber_SetMo"])
        packets = [r for r in writes if r["op"] == "list"]
        self.assertEqual(packets[0]["sha256"], MARK_BOX10_LIST_SHA)
        self.assertEqual(packets[0]["cmds"][0][0], "listReadyMark")
        self.assertTrue(any(r["dir"] == "R" and r["status"] == "0x0020" for r in records))

    def test_no_trace_file_without_the_env_var(self):
        with mock.patch.dict(os.environ, {"OPENMOPA_USB_TRACE": ""}):
            self.assertIsNone(live.UsbTrace.from_env())


if __name__ == "__main__":
    unittest.main()
