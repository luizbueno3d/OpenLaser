"""B2: the abort never writes into a board that is not READY, and always closes MO.

Every test runs against tests.fake_lmc.FakeLmcBoard; no USB is opened.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, DUMMY_PACKET_SHA, FakeLmcBoard, fake_board, params

# galvoplotter's abort() on a READY board, as captured in report 05 section 1.9.
GOLDEN_ABORT = [
    ("StopExecute", [0x001F, 0, 0, 0, 0, 0]),
    ("Fiber_SetMo", [0x0033, 0, 0, 0, 0, 0]),
    ("ResetList", [0x0012, 0, 0, 0, 0, 0]),
    ("GetVersion", [0x0007, 0, 0, 0, 0, 0]),
    ("list", DUMMY_PACKET_SHA),
    ("SetEndOfList", [0x0019, 0, 0, 0, 0, 0]),
    ("ExecuteList", [0x0005, 0, 0, 0, 0, 0]),
    ("Fiber_SetMo", [0x0033, 0, 0, 0, 0, 0]),
    ("WritePort", [0x0021, 0, 0, 0, 0, 0]),
]


def wire_after_last(board: FakeLmcBoard, op: str) -> list[tuple]:
    wire = board.wire()
    start = max(i for i, e in enumerate(wire) if e.get("op") == op) + 1
    return [("list", e["sha256"]) if e["kind"] == "list" else (e["op"], e["words"]) for e in wire[start:]]


class SafeAbortTests(unittest.TestCase):
    def test_ready_board_gets_galvoplotters_exact_abort_bytes(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        # The job ends with GetMarkTime and the main thread's idle poll, then the abort.
        tail = wire_after_last(board, "GetMarkTime")
        self.assertEqual(tail[0], ("GetVersion", [0x0007, 0, 0, 0, 0, 0]))
        self.assertEqual(tail[1:], GOLDEN_ABORT)

    def test_never_ready_board_gets_no_packet_but_mo_and_ports_close(self):
        board = FakeLmcBoard(ready=False)
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            started = time.monotonic()
            controller.shutdown()
            elapsed = time.monotonic() - started
        self.assertEqual(board.lists(), [])
        ops = [e["op"] for e in board.singles()]
        abort = ops[ops.index("StopExecute"):]
        self.assertEqual(abort[:3], ["StopExecute", "Fiber_SetMo", "ResetList"])
        self.assertEqual(set(abort[3:-2]), {"GetVersion"})
        self.assertEqual(abort[-2:], ["Fiber_SetMo", "WritePort"])
        self.assertNotIn("ExecuteList", abort)
        self.assertEqual(board.singles("WritePort")[-1]["words"][1], 0)
        self.assertLess(elapsed, 0.5)

    def test_user_stop_on_a_never_ready_board_writes_nothing(self):
        board = FakeLmcBoard(ready=False)
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            controller.submit(live._guard_job(controller, lambda c: c.wait_ready()))
            time.sleep(0.05)
            live.stop_controller(controller)
        self.assertEqual(board.lists(), [])
        self.assertEqual(board.singles("WritePort")[-1]["words"][1], 0)

    def test_failing_commands_never_skip_the_final_mo_close_and_port_off(self):
        board = FakeLmcBoard()
        with fake_board(board):
            controller = live.connect_controller(CFG, params())
            with mock.patch.object(controller, "stop_execute", side_effect=OSError("usb gone")), \
                    mock.patch.object(controller, "reset_list", side_effect=OSError("usb gone")):
                controller.abort()  # must not raise
            controller.shutdown()
        ops = [e["op"] for e in board.singles()]
        self.assertEqual(ops[-2:], ["Fiber_SetMo", "WritePort"])

    def test_abort_after_red_light_turns_the_pointer_off_too(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.frame_polylines(CFG, params(), BOX10)
        self.assertEqual(board.singles("WritePort")[-1]["words"][1], 0)


if __name__ == "__main__":
    unittest.main()
