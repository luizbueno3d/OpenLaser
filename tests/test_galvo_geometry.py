import unittest

from mopa_luiz.live import (
    MAX_JOB_PATHS,
    MAX_JOB_POINTS,
    geometry_errors,
    mm_to_galvo,
    sanitize_polylines,
)


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
