import textwrap
import unittest

from mopa_luiz.importers import import_geometry, parse_dxf


class DxfImportTests(unittest.TestCase):
    def test_line_and_lwpolyline(self):
        dxf = textwrap.dedent("""\
            0
            SECTION
            2
            ENTITIES
            0
            LINE
            10
            0
            20
            0
            11
            10
            21
            0
            0
            LWPOLYLINE
            70
            1
            10
            0
            20
            0
            10
            4
            20
            0
            10
            4
            20
            3
            0
            ENDSEC
        """)
        polylines = parse_dxf(dxf)
        self.assertEqual(len(polylines), 2)
        self.assertEqual(polylines[0], [(0.0, 0.0), (10.0, 0.0)])
        self.assertEqual(polylines[1][0], polylines[1][-1])

    def test_old_polyline_vertex_seqend(self):
        dxf = textwrap.dedent("""\
            0
            POLYLINE
            70
            1
            0
            VERTEX
            10
            0
            20
            0
            0
            VERTEX
            10
            5
            20
            0
            0
            VERTEX
            10
            5
            20
            5
            0
            SEQEND
        """)
        polylines = parse_dxf(dxf)
        self.assertEqual(len(polylines), 1)
        self.assertEqual(polylines[0][0], polylines[0][-1])
        self.assertEqual(len(polylines[0]), 4)

    def test_import_geometry_preserves_dxf_millimeters(self):
        dxf = "0\nLINE\n10\n0\n20\n0\n11\n10\n21\n0\n"
        result = import_geometry("part.dxf", dxf)
        self.assertEqual(result["polyline_count"], 1)
        self.assertEqual(result["point_count"], 2)
        self.assertFalse(result["source_bounds"]["flip_y"])
        self.assertEqual(result["source_bounds"]["unit_name"], "unitless_assumed_mm")
        self.assertAlmostEqual(result["polylines"][0][0][0], -5.0)
        self.assertAlmostEqual(result["polylines"][0][1][0], 5.0)

    def test_import_geometry_converts_dxf_inches_to_mm(self):
        dxf = textwrap.dedent("""\
            0
            SECTION
            2
            HEADER
            9
            $INSUNITS
            70
            1
            0
            ENDSEC
            0
            LINE
            10
            0
            20
            0
            11
            1
            21
            0
        """)
        result = import_geometry("inch-part.dxf", dxf)
        self.assertEqual(result["source_bounds"]["unit_name"], "inches")
        self.assertAlmostEqual(result["source_bounds"]["unit_scale_to_mm"], 25.4)
        self.assertAlmostEqual(result["polylines"][0][0][0], -12.7)
        self.assertAlmostEqual(result["polylines"][0][1][0], 12.7)

    def test_lwpolyline_bulge_is_flattened(self):
        dxf = textwrap.dedent("""\
            0
            LWPOLYLINE
            10
            1
            20
            0
            42
            0.41421356237
            10
            0
            20
            1
        """)
        polylines = parse_dxf(dxf)
        self.assertEqual(len(polylines), 1)
        self.assertGreater(len(polylines[0]), 2)
        self.assertAlmostEqual(polylines[0][0][0], 1.0)
        self.assertAlmostEqual(polylines[0][-1][1], 1.0)

    def test_ellipse_entity(self):
        dxf = textwrap.dedent("""\
            0
            ELLIPSE
            10
            0
            20
            0
            11
            5
            21
            0
            40
            0.5
            41
            0
            42
            6.283185307179586
        """)
        polylines = parse_dxf(dxf)
        self.assertEqual(len(polylines), 1)
        self.assertGreater(len(polylines[0]), 20)


if __name__ == "__main__":
    unittest.main()
