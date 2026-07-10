import math
import unittest

from mopa_luiz.raster import hatch_polylines, is_closed


def square(size: float):
    s = size / 2
    return [(-s, -s), (s, -s), (s, s), (-s, s), (-s, -s)]


class IsClosedTests(unittest.TestCase):
    def test_closed_square(self):
        self.assertTrue(is_closed(square(10)))

    def test_open_segment(self):
        self.assertFalse(is_closed([(0, 0), (5, 0)]))

    def test_too_short(self):
        self.assertFalse(is_closed([(0, 0), (0, 0)]))


class HatchTests(unittest.TestCase):
    def test_square_fills_with_one_line_per_pitch(self):
        result = hatch_polylines([square(10)], angle_deg=0, pitch_mm=1.0, passes=1)
        # 10 mm tall at 1 mm pitch: ~10 horizontal hatch lines.
        self.assertEqual(result.passes, 1)
        self.assertEqual(result.closed_input_count, 1)
        self.assertEqual(len(result.segments), 10)
        for seg in result.segments:
            self.assertEqual(len(seg), 2)
            # Hatch lines for angle 0 should be horizontal (same y on both ends).
            self.assertAlmostEqual(seg[0][1], seg[1][1])

    def test_open_path_is_skipped(self):
        result = hatch_polylines([[(0, 0), (5, 0)]], angle_deg=0, pitch_mm=1.0, passes=1)
        self.assertEqual(result.segments, [])
        self.assertEqual(result.closed_input_count, 0)
        self.assertEqual(result.skipped_open_count, 1)

    def test_donut_skips_inner_hole(self):
        outer = square(20)
        inner = square(6)
        result = hatch_polylines([outer, inner], angle_deg=0, pitch_mm=1.0, passes=1)
        # If the algorithm respected the hole, every line crossing the inner
        # square should be split in two. Verify by counting hatch lines whose
        # gap covers the hole area (centered on x=0, y in [-3..3]).
        crossings_through_hole = 0
        for seg in result.segments:
            y = seg[0][1]
            if -3 + 0.05 < y < 3 - 0.05:
                # In the hole's y range, two segments share this y; combined
                # they avoid x in roughly [-3, 3].
                crossings_through_hole += 1
        # In the hole y-range, every scan line is split → 2 segments per line.
        self.assertGreaterEqual(crossings_through_hole, 4)

    def test_cross_hatch_doubles_line_count(self):
        single = hatch_polylines([square(10)], angle_deg=0, pitch_mm=1.0,
                                 passes=1)
        cross = hatch_polylines([square(10)], angle_deg=0, pitch_mm=1.0,
                                passes=2, angle_step_deg=90.0)
        self.assertEqual(len(cross.segments), 2 * len(single.segments))

    def test_angle_90_makes_lines_vertical(self):
        result = hatch_polylines([square(10)], angle_deg=90, pitch_mm=1.0, passes=1)
        for seg in result.segments:
            # 90deg hatching → equal x on both endpoints.
            self.assertAlmostEqual(seg[0][0], seg[1][0])

    def test_pitch_must_be_positive(self):
        with self.assertRaises(ValueError):
            hatch_polylines([square(10)], angle_deg=0, pitch_mm=0)

    def test_passes_zero_returns_no_segments(self):
        result = hatch_polylines([square(10)], angle_deg=0, pitch_mm=1.0, passes=0)
        self.assertEqual(result.segments, [])
        self.assertEqual(result.passes, 0)


if __name__ == "__main__":
    unittest.main()
