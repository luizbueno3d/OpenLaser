"""The stop path holds up when a SIGTERM lands at a bad moment, or the trace fails.

The job child's SIGTERM handler runs on the main thread, in the middle of
whatever that thread was doing; these tests re-enter shutdown() the same
way. No USB is opened: the board is tests.fake_lmc.FakeLmcBoard.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, DUMMY_PACKET_SHA, FakeLmcBoard, fake_board, params
from tests.test_board_waits import call_with_deadline


class TraceFile:
    """Stands in for the trace's file handle; `on_write` runs inside the trace lock."""

    def __init__(self, on_write=None, fail=False):
        self.on_write = on_write
        self.fail = fail
        self.closed = False
        self.lines: list[str] = []

    def write(self, text):
        if self.fail:
            raise OSError("disk full")
        if self.on_write is not None:
            self.on_write(text)
        self.lines.append(text)

    def close(self):
        self.closed = True


def ends_with_mo_and_ports_closed(test, board):
    singles = [(e["op"], e["words"][1]) for e in board.singles()]
    test.assertEqual(singles[-2:], [("Fiber_SetMo", 0), ("WritePort", 0)])


class StopRobustnessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"OPENMOPA_USB_TRACE": str(Path(tmp.name) / "trace.jsonl")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_sigterm_while_the_trace_is_writing_does_not_deadlock(self):
        board = FakeLmcBoard()
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            fired = []

            def sigterm_inside_trace_lock(text):
                if '"StopExecute"' in text and not fired:
                    fired.append(True)
                    controller.shutdown()

            trace = controller._openlaser_waits.trace
            trace._fh.close()
            trace._fh = TraceFile(sigterm_inside_trace_lock)
            _, error = call_with_deadline(self, controller.shutdown, 5.0)
        self.assertIsNone(error)
        self.assertEqual(fired, [True])
        self.assertEqual(len(board.singles("StopExecute")), 2)
        ends_with_mo_and_ports_closed(self, board)

    def test_sigterm_during_the_usb_reset_never_reconnects(self):
        board = FakeLmcBoard()
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            original_close = board.close
            written_during_close = []

            def close_with_sigterm(index=0):
                before = len(board.wire())
                controller.shutdown()  # the handler, while galvoplotter is closing the device
                written_during_close.append(len(board.wire()) - before)
                original_close(index)

            board.close = close_with_sigterm
            _, error = call_with_deadline(self, controller.shutdown, 5.0)
            board.close = original_close
        self.assertIsNone(error)
        self.assertEqual(written_during_close, [0], "nothing may be written to a device being reset")
        self.assertEqual(len(board.singles("GetSerialNo")), 1, "the board was never re-initialised")
        self.assertEqual(board.events[-1]["kind"], "close")
        self.assertFalse(board.is_open())

    def test_a_failing_trace_never_blocks_a_usb_command(self):
        def broken_trace(trace, path):
            trace._fh = TraceFile(fail=True)  # every write raises OSError
            trace._lock = threading.RLock()

        board = FakeLmcBoard()
        with fake_board(board), mock.patch.object(live.UsbTrace, "__init__", broken_trace):
            result = live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
            controller = live.connect_controller(CFG, params())
            live.stop_controller(controller)
        self.assertTrue(result["ok"])
        self.assertEqual(len(board.singles("StopExecute")), 2)
        ends_with_mo_and_ports_closed(self, board)


class DriverSpeedCheckTests(unittest.TestCase):
    def test_zero_and_nan_speeds_are_refused_before_the_board(self):
        board = FakeLmcBoard()
        with fake_board(board):
            for speed in (0.0, -5.0, float("nan")):
                with self.subTest(speed=speed), self.assertRaisesRegex(PermissionError, "positive number"):
                    live.mark_polylines(CFG, params(speed=speed), BOX10, arm=True, confirm="ARM")
        self.assertEqual(board.events, [])


class FrameLoopFaultTests(unittest.TestCase):
    def test_an_old_loops_fault_is_not_reported_by_a_new_loop(self):
        loop = live.FrameLoop()
        loop._controller = object()  # a newer loop is running
        old = mock.Mock()
        loop._on_fault(old, "board not READY within 9.0 s")
        deadline = time.monotonic() + 5.0
        while not old.shutdown.called and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(old.shutdown.called)
        self.assertIsNone(loop.status()["fault"])
        self.assertTrue(loop.running)


class SlowOpenBoard(FakeLmcBoard):
    """A board whose USB open() blocks until the test lets it finish."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.opening = threading.Event()
        self.release = threading.Event()

    def open(self, index=0):
        self.opening.set()
        self.release.wait(10.0)
        return super().open(index)


class FrameLoopStopDuringConnectTests(unittest.TestCase):
    def test_stop_while_start_is_connecting_wins(self):
        board = SlowOpenBoard()
        loop = live.FrameLoop()
        self.addCleanup(loop.stop)  # never leave a loop running if an assertion fails
        outcome: dict = {}

        def start():
            try:
                outcome["result"] = loop.start(CFG, params(), BOX10)
            except BaseException as exc:  # handed back to the test
                outcome["error"] = exc

        with fake_board(board):
            worker = threading.Thread(target=start, daemon=True)
            worker.start()
            try:
                self.assertTrue(board.opening.wait(10.0), "start never reached the board's open()")
                stopped = loop.stop()  # Stop from another thread, mid-connect
            finally:
                board.release.set()
                worker.join(15.0)
            self.assertFalse(worker.is_alive(), "start never returned")
            self.assertNotIn("result", outcome, "the loop started after Stop")
            self.assertIsInstance(outcome.get("error"), RuntimeError)
            self.assertIn("stopped while connecting", str(outcome["error"]))
            live.BoardLock(live.BOARD_LOCK_PATH).release()  # the refused start freed the board
            self.assertTrue(stopped["ok"])
            self.assertFalse(loop.running)
            self.assertEqual(loop.status()["points"], [])
            # No red-light list: at most the abort's empty end-of-list packet.
            self.assertEqual([e for e in board.lists() if e["n_real"]], [])
            self.assertEqual([e for e in board.lists() if e["sha256"] != DUMMY_PACKET_SHA], [])
            self.assertTrue(board.singles("StopExecute"))
            self.assertEqual(board.devices, {})

            # The stop is spent: the next Start runs the loop.
            loop.start(CFG, params(), BOX10)
            try:
                self.assertTrue(loop.running)
                deadline = time.monotonic() + 5.0
                while not [e for e in board.lists() if e["n_real"]] and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue([e for e in board.lists() if e["n_real"]])
            finally:
                loop.stop()
            self.assertFalse(loop.running)


if __name__ == "__main__":
    unittest.main()
