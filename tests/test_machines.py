import unittest

from mopa_luiz.gcode import compile_gcode
from mopa_luiz.layers import LayerJob, LayerSpec, VECTOR_CUT
from mopa_luiz.machines import machine_by_id, machine_catalog


def layer(**overrides):
    values = dict(
        layer_id="cut",
        name="Cut",
        operation=VECTOR_CUT,
        power_percent=25,
        frequency_khz=30,
        pulse_width_ns=200,
        speed_mm_s=10,
        passes=1,
    )
    values.update(overrides)
    return LayerSpec(**values)


class MachineCatalogTests(unittest.TestCase):
    def test_catalog_covers_requested_tool_families(self):
        kinds = {item["kind"] for item in machine_catalog()}
        self.assertEqual(kinds, {"fiber_galvo", "co2_gantry", "diode_gantry", "pen_plotter"})

    def test_unknown_machine_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown machine"):
            machine_by_id("missing")


class GCodeTests(unittest.TestCase):
    def test_diode_uses_dynamic_power_and_center_to_bed_conversion(self):
        machine = machine_by_id("diode-grbl-400x400")
        program = compile_gcode(machine, [LayerJob(layer(), [[(-10, 0), (10, 0)]])])
        self.assertIn("M4 S250", program.content)
        self.assertIn("G0 X190 Y200", program.content)
        self.assertIn("G1 X210 Y200 F600", program.content)

    def test_co2_uses_constant_power_and_air_assist(self):
        machine = machine_by_id("co2-grbl-600x400")
        spec = layer(air_assist=True, passes=2)
        program = compile_gcode(machine, [LayerJob(spec, [[(0, 0), (5, 0)]])])
        self.assertIn("M3 S250", program.content)
        self.assertIn("M8", program.content)
        self.assertIn("M9", program.content)
        self.assertEqual(program.path_count, 2)

    def test_plotter_uses_pen_commands_not_laser_power(self):
        machine = machine_by_id("plotter-grbl-a4")
        program = compile_gcode(machine, [LayerJob(layer(power_percent=99), [[(0, 0), (1, 1)]])])
        self.assertIn("M3 S1000", program.content)
        self.assertNotIn("M4 S", program.content)
        self.assertIn("G0 X105 Y148.5", program.content)

    def test_geometry_outside_bed_is_rejected(self):
        machine = machine_by_id("plotter-grbl-a4")
        with self.assertRaisesRegex(ValueError, "outside"):
            compile_gcode(machine, [LayerJob(layer(), [[(0, 0), (106, 0)]])])


if __name__ == "__main__":
    unittest.main()
