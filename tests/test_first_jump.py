"""B3: every list starts with an explicit jump to its first point.

Every test runs against tests.fake_lmc.FakeLmcBoard; no USB is opened.
"""

from __future__ import annotations

import time
import unittest

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, DOT, MARK_BOX10_LIST_SHA, FakeLmcBoard, fake_board, params

JUMP_TO, MARK_TO, WRITE_PORT = 0x8001, 0x8005, 0x8011
CENTRE = 0x8000
# frame_polylines(BOX10) and frame_box() list packets before B3 (box starts off-centre).
FRAME_BOX10_SHAS = [
    "1e3d0cf48a8821d91b4dda44b480e70e44abc316b2ff181fb1e63b2152cecfa6",
    "d2edd540ebfb691db580ac8060c1ccf7912457958fe894be3be62c8196180dd0",
    "c007f2003140ae52444d41e39052fff4d2da92f6214f1c41a0da4e3def9f7916",
]
FRAME_BOX_SHA = "ece9c7398eaae6e12a1d60ad59d69e909b27a8d77c7f3463a237e2a798c69ff2"
FROM_ORIGIN = [[[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 0.0]]]


def first_list(board: FakeLmcBoard) -> list[list[int]]:
    return board.lists()[0]["cmds"]


class FirstJumpTests(unittest.TestCase):
    def test_dot_test_starts_with_an_explicit_jump(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.mark_polylines(CFG, params(power=1.0), DOT, arm=True, confirm="ARM")
        cmds = first_list(board)
        opcodes = [c[0] for c in cmds]
        self.assertIn(JUMP_TO, opcodes)
        jump = opcodes.index(JUMP_TO)
        self.assertLess(jump, opcodes.index(MARK_TO))
        self.assertEqual(cmds[jump][1:3], [CENTRE, CENTRE])

    def test_mark_box10_bytes_are_unchanged(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        self.assertEqual(board.lists()[0]["sha256"], MARK_BOX10_LIST_SHA)

    def test_off_centre_frames_are_unchanged(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.frame_polylines(CFG, params(), BOX10)
            live.frame_box(CFG, params())
        shas = [e["sha256"] for e in board.lists() if e["n_real"]]
        self.assertEqual(shas, FRAME_BOX10_SHAS + [FRAME_BOX_SHA])

    def test_red_light_from_the_origin_jumps_dark_first(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.frame_polylines(CFG, params(), FROM_ORIGIN)
        cmds = first_list(board)
        jump = [c[0] for c in cmds].index(JUMP_TO)
        self.assertEqual(cmds[jump][1:3], [CENTRE, CENTRE])
        ports = [c[1] for c in cmds[:jump] if c[0] == WRITE_PORT]
        self.assertEqual(ports[-1], 0x0000, "the red light must be off for the first jump")

    def test_frame_loop_list_starts_with_the_jump(self):
        board = FakeLmcBoard()
        loop = live.FrameLoop()
        with fake_board(board):
            loop.start(CFG, params(), FROM_ORIGIN)
            deadline = time.monotonic() + 5.0
            while not board.lists() and time.monotonic() < deadline:
                time.sleep(0.01)
            loop.stop()
        cmds = first_list(board)
        jump = [c[0] for c in cmds].index(JUMP_TO)
        self.assertEqual(cmds[jump][1:3], [CENTRE, CENTRE])
        self.assertEqual([c[1] for c in cmds[:jump] if c[0] == WRITE_PORT][-1], 0x0000)

    def test_mark_from_the_origin_jumps_before_the_first_mark(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.mark_polylines(CFG, params(), FROM_ORIGIN, arm=True, confirm="ARM")
        opcodes = [c[0] for c in first_list(board)]
        self.assertLess(opcodes.index(JUMP_TO), opcodes.index(MARK_TO))


if __name__ == "__main__":
    unittest.main()
