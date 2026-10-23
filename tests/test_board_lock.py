"""Patch 12: an exclusive board lock, taken before USB is opened.

The board is tests.fake_lmc.FakeLmcBoard; the lock file lives in a temp dir.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from mopa_luiz import live
from tests.fake_lmc import BOX10, CFG, FakeLmcBoard, fake_board, params

HOLDER = textwrap.dedent("""\
    import fcntl, os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    print("locked", flush=True)
    sys.stdin.read()
""")


@unittest.skipIf(sys.platform.startswith("win"), "the holder process uses fcntl")
class BoardLockTests(unittest.TestCase):
    def hold_lock_in_another_process(self, path):
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")
        return holder

    def test_second_process_is_refused_and_never_touches_usb(self):
        board = FakeLmcBoard()
        with fake_board(board):
            holder = self.hold_lock_in_another_process(live.BOARD_LOCK_PATH)
            with mock.patch.object(live, "GalvoController") as constructor, \
                    mock.patch.object(live, "ensure_libusb_backend") as libusb:
                with self.assertRaisesRegex(live.BoardBusyError, "another OpenLaser process holds the board"):
                    live.connect_controller(CFG, params())
                with self.assertRaises(live.BoardBusyError):
                    live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
                with self.assertRaises(live.BoardBusyError):
                    live.FrameLoop().start(CFG, params(), BOX10)
            constructor.assert_not_called()
            libusb.assert_not_called()
            self.assertEqual(board.events, [])
            holder.stdin.close()
            holder.wait(10)
            # Once the other process lets go, the board is ours.
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        self.assertTrue(board.lists())

    def test_lock_is_released_after_each_job(self):
        board = FakeLmcBoard()
        with fake_board(board):
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
            live.frame_polylines(CFG, params(), BOX10)
            live.mark_polylines(CFG, params(), BOX10, arm=True, confirm="ARM")
        self.assertEqual(len(board.singles("StopExecute")), 3)

    def test_running_frame_loop_holds_the_board_until_stopped(self):
        board = FakeLmcBoard()
        loop = live.FrameLoop()
        with fake_board(board):
            loop.start(CFG, params(), BOX10)
            try:
                with self.assertRaises(live.BoardBusyError):
                    live.connect_controller(CFG, params())
            finally:
                loop.stop()
            controller = live.connect_controller(CFG, params())
            controller.shutdown()

    def test_failed_connect_releases_the_lock(self):
        board = FakeLmcBoard()
        with fake_board(board):
            with mock.patch.object(board, "open", side_effect=OSError("no device")):
                with self.assertRaises(RuntimeError):
                    live.connect_controller(CFG, params(), connect_timeout_s=5.0)
            controller = live.connect_controller(CFG, params())
            controller.shutdown()

    def test_mock_mode_opens_no_usb_and_takes_no_lock(self):
        board = FakeLmcBoard()
        with fake_board(board):
            self.hold_lock_in_another_process(live.BOARD_LOCK_PATH)
            with mock.patch.object(live, "mock_mode", return_value=True):
                controller = live.connect_controller(CFG, params())
                controller.shutdown()

    def test_default_path_is_the_one_ol_uses_on_macos(self):
        with mock.patch.dict("os.environ", {"OPENLASER_BOARD_LOCK": ""}), mock.patch.object(sys, "platform", "darwin"):
            path = live.default_board_lock_path()
        self.assertEqual(path.parts[-4:], ("Library", "Application Support", "OpenLaser", "board.lock"))


if __name__ == "__main__":
    unittest.main()
