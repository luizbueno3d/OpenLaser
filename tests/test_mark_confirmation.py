"""B4: the Mark confirmation shows each layer's own values, never the Frame panel's."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mopa_luiz import live, ui
from mopa_luiz.layers import default_layers
from tests.test_planner import SAMPLE_MARKCFG, call_post

FRAME_LIVE = {
    "power": 100, "frequency_khz": 30, "pulse_width_ns": 200,
    "mark_speed": 1000, "confirm": "ARM", "repeat_count": 3,
}


def js_block(start: str, end: str) -> str:
    source = ui.HTML
    return source[source.index(start):source.index(end)]


class PlanRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "markcfg7"
        self.cfg_path.write_text(SAMPLE_MARKCFG, encoding="latin-1")
        self.handler_cls = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": self.cfg_path})

    def tearDown(self):
        self.tmp.cleanup()

    def test_rows_equal_the_layer_values_not_the_frame_panel(self):
        layers = default_layers()
        engrave = next(layer for layer in layers if layer.layer_id == "vector-engrave")
        engrave.power_percent, engrave.frequency_khz, engrave.pulse_width_ns = 37.0, 45.0, 199.8
        engrave.speed_mm_s, engrave.passes = 300.0, 2
        cut = next(layer for layer in layers if layer.layer_id == "vector-cut")
        cut.power_percent, cut.frequency_khz, cut.pulse_width_ns, cut.speed_mm_s = 80.0, 60.0, 100.0, 50.0
        payload = {
            "machine_id": "jpt-m7-200",
            "objects": [
                {"layer_id": "vector-engrave", "polylines": [[(0, 0), (5, 0)]]},
                {"layer_id": "vector-cut", "polylines": [[(0, 0), (0, 5)], [(1, 0), (1, 5)]]},
                {"layer_id": "frame-only", "polylines": [[(0, 0), (9, 9)]]},
            ],
            "layers": [layer.to_dict() for layer in layers],
            "live": FRAME_LIVE,
        }
        body = call_post(self.handler_cls, "/api/plan", payload)["payload"]
        rows = body["emission_rows"]
        field = live.effective_field_size(ui.load_markcfg(self.cfg_path))
        self.assertEqual([row["layer_id"] for row in rows], ["vector-engrave", "vector-cut"])
        first, second = rows
        self.assertEqual(
            (first["power_percent"], first["frequency_khz"], first["pulse_width_ns"], first["passes"]),
            (37.0, 45.0, 200.0, 2),
        )
        self.assertEqual(first["speed_units"], int(300.0 * (0xFFFF / field) / 1000.0))
        self.assertAlmostEqual(first["actual_speed_mm_s"], first["speed_units"] * 1000.0 * field / 0xFFFF, places=3)
        self.assertLess(first["actual_speed_mm_s"], 300.0)
        self.assertEqual((second["power_percent"], second["frequency_khz"], second["path_count"]), (80.0, 60.0, 2))
        self.assertNotIn(100.0, [row["power_percent"] for row in rows])
        self.assertEqual(body["repeat_count"], 3)


class ConfirmDialogSourceTests(unittest.TestCase):
    def test_dialog_renders_only_the_plan_rows(self):
        handler = js_block("document.getElementById('mark').addEventListener", "document.getElementById('dotTest')")
        self.assertIn("preview.emission_rows", handler)
        self.assertIn("preview.repeat_count", handler)
        for frame_value in ("params.power", "params.frequency_khz", "params.pulse_width_ns", "params.mark_speed",
                            "getElementById('power')", "getElementById('markSpeed')"):
            self.assertNotIn(frame_value, handler)

    def test_job_summary_no_longer_shows_frame_power(self):
        summary = js_block("function updateSummary()", "document.getElementById('mark').disabled")
        self.assertNotIn("getElementById('power')", summary)
        self.assertNotRegex(summary, re.escape("<span>Power %</span>"))

    def test_frame_panel_says_it_never_reaches_a_burn(self):
        self.assertIn("Frame settings (red light only)", ui.HTML)
        self.assertNotIn("Legacy frame parameters", ui.HTML)


if __name__ == "__main__":
    unittest.main()
