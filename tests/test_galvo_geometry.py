import threading
import time
import types
import unittest

from mopa_luiz.live import (
    FRAME_ONCE_SECONDS,
    FRAME_LIGHT_SPEED_MM_S,
    MAX_JOB_PATHS,
    MAX_JOB_POINTS,
    apply_wait_timeouts,
    frame_once_repeats,
    geometry_errors,
    mm_to_galvo,
    sanitize_polylines,
)


def busy_board():
    board = types.SimpleNamespace(
        _sending=True,
        _spooler_thread=None,
        is_busy=lambda: True,
        is_ready=lambda: False,
        is_ready_and_not_busy=lambda: False,
    )
    apply_wait_timeouts(board, timeout_s=5.0)
    return board


class StopLatencyTests(unittest.TestCase):
    def test_stop_aborts_the_spooler_instead_of_sending_its_packet(self):
        board = busy_board()
        outcome = {}

        def spooler():
            started = time.monotonic()
            try:
                board.wait_ready()
                outcome["result"] = "returned"
            except ConnectionAbortedError:
                outcome["result"] = "aborted"
            outcome["seconds"] = time.monotonic() - started

        thread = threading.Thread(target=spooler)
        board._spooler_thread = thread
        thread.start()
        time.sleep(0.05)
        board._openlaser_stop.set()
        thread.join(2.0)
        self.assertEqual(outcome.get("result"), "aborted")
        self.assertLess(outcome["seconds"], 1.0)

    def test_stop_releases_other_waits_without_raising(self):
        board = busy_board()
        board._openlaser_stop.set()
        started = time.monotonic()
        board.wait_finished()
        self.assertLess(time.monotonic() - started, 1.0)


class FrameOnceTests(unittest.TestCase):
    def test_small_shape_is_retraced_for_a_visible_time(self):
        square = [[(-25, -25), (25, -25), (25, 25), (-25, 25), (-25, -25)]]
        repeats = frame_once_repeats(square, 5)
        self.assertGreaterEqual(repeats * 200.0 / FRAME_LIGHT_SPEED_MM_S, FRAME_ONCE_SECONDS * 0.9)

    def test_many_tiny_paths_are_not_repeated_past_the_point_limit(self):
        paths = [[(0, 0), (0.001, 0)] for _ in range(MAX_JOB_PATHS)]
        repeats = frame_once_repeats(paths, 2 * MAX_JOB_PATHS)
        self.assertEqual(repeats, MAX_JOB_POINTS // (2 * MAX_JOB_PATHS))

    def test_long_job_is_traced_once(self):
        paths = [[(-100, y), (100, y)] for y in range(-50, 50)]
        self.assertEqual(frame_once_repeats(paths, 200), 1)


class GalvoGeometryTests(unittest.TestCase):
    def test_in_field_geometry_converts(self):
        paths = sanitize_polylines([[(-100, -100), (100, 100)]], 200.0)
        self.assertEqual(paths, [[mm_to_galvo(-100, -100, 200.0), mm_to_galvo(100, 100, 200.0)]])

    def test_out_of_field_point_is_refused_not_clamped(self):
        with self.assertRaisesRegex(ValueError, "outside the 200.00 mm galvo field"):
            sanitize_polylines([[(0, 0), (150, 0)]], 200.0)

    def test_non_finite_point_is_refused(self):
        self.assertTrue(geometry_errors([[(0, 0), (float("nan"), 0)]], 200.0))
        self.assertTrue(geometry_errors([[(0, 0), (float("inf"), 0)]], 200.0))

    def test_oversized_job_is_refused_not_truncated(self):
        too_many_paths = [[(-1, 0), (1, 0)] for _ in range(MAX_JOB_PATHS + 1)]
        with self.assertRaisesRegex(ValueError, "paths"):
            sanitize_polylines(too_many_paths, 200.0)
        too_many_points = [[(0.0, 0.0)] * 1500 for _ in range(MAX_JOB_POINTS // 1500 + 1)]
        with self.assertRaisesRegex(ValueError, "points"):
            sanitize_polylines(too_many_points, 200.0)

    def test_limit_sized_job_is_kept_whole(self):
        paths = [[(-1, 0), (1, 0)] for _ in range(MAX_JOB_PATHS)]
        self.assertEqual(len(sanitize_polylines(paths, 200.0)), MAX_JOB_PATHS)


if __name__ == "__main__":
    unittest.main()
