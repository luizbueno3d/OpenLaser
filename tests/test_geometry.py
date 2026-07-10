import unittest

from mopa_luiz.geometry import bounding_box, join_lines


class JoinLinesTests(unittest.TestCase):
    def test_two_touching_segments_become_one(self):
        result = join_lines([
            [(0.0, 0.0), (1.0, 0.0)],
            [(1.0, 0.0), (2.0, 0.0)],
        ], tolerance_mm=0.01)
        self.assertEqual(result.input_segments, 2)
        self.assertEqual(result.output_paths, 1)
        self.assertEqual(result.joins_performed, 1)
        self.assertEqual(result.polylines[0][0], (0.0, 0.0))
        self.assertEqual(result.polylines[0][-1], (2.0, 0.0))

    def test_small_gap_within_tolerance_joins(self):
        result = join_lines([
            [(0.0, 0.0), (1.0, 0.0)],
            [(1.05, 0.0), (2.0, 0.0)],
        ], tolerance_mm=0.1)
        self.assertEqual(result.output_paths, 1)
        self.assertEqual(result.joins_performed, 1)

    def test_gap_larger_than_tolerance_does_not_join(self):
        result = join_lines([
            [(0.0, 0.0), (1.0, 0.0)],
            [(1.5, 0.0), (2.0, 0.0)],
        ], tolerance_mm=0.1)
        self.assertEqual(result.output_paths, 2)
        self.assertEqual(result.joins_performed, 0)

    def test_square_from_four_segments_forms_closed_loop(self):
        segments = [
            [(0.0, 0.0), (10.0, 0.0)],
            [(10.0, 0.0), (10.0, 10.0)],
            [(10.0, 10.0), (0.0, 10.0)],
            [(0.0, 10.0), (0.0, 0.0)],
        ]
        result = join_lines(segments, tolerance_mm=0.01)
        self.assertEqual(result.output_paths, 1)
        self.assertEqual(result.closed_loops, 1)
        self.assertEqual(result.polylines[0][0], result.polylines[0][-1])

    def test_t_junction_does_not_collapse(self):
        # Three segments meet at (0, 0); a degree-3 node must not be walked through.
        segments = [
            [(-1.0, 0.0), (0.0, 0.0)],
            [(1.0, 0.0), (0.0, 0.0)],
            [(0.0, 1.0), (0.0, 0.0)],
        ]
        result = join_lines(segments, tolerance_mm=0.01)
        self.assertEqual(result.input_segments, 3)
        self.assertEqual(result.output_paths, 3)
        self.assertEqual(result.joins_performed, 0)

    def test_internal_vertices_preserved(self):
        result = join_lines([
            [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
            [(2.0, 0.0), (3.0, 1.0), (4.0, 0.0)],
        ], tolerance_mm=0.01)
        self.assertEqual(result.output_paths, 1)
        self.assertEqual(len(result.polylines[0]), 5)

    def test_empty_input(self):
        result = join_lines([], tolerance_mm=0.05)
        self.assertEqual(result.output_paths, 0)


class BoundingBoxTests(unittest.TestCase):
    def test_basic(self):
        bbox = bounding_box([[(0.0, 0.0), (5.0, 3.0)], [(-1.0, 2.0)]])
        self.assertEqual(bbox["min_x"], -1.0)
        self.assertEqual(bbox["max_y"], 3.0)
        self.assertEqual(bbox["width"], 6.0)
        self.assertEqual(bbox["height"], 3.0)

    def test_empty(self):
        bbox = bounding_box([])
        self.assertEqual(bbox["width"], 0.0)


if __name__ == "__main__":
    unittest.main()
