"""B10: the work area is the lens's rated field, not the calibrated galvo reach.

At scale_correction 1.1834 the galvo reaches +-118.34 mm, but the lens is
rated for the profile's 200 mm FIELDSIZE, so +-100 mm is the limit for both
Frame and Mark. No USB is opened: the board is tests.fake_lmc.FakeLmcBoard.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mopa_luiz import live, ui
from mopa_luiz.layers import default_layers
from tests.fake_lmc import CFG, FakeLmcBoard, fake_board, params
from tests.test_planner import SAMPLE_MARKCFG, call_mark, call_post

EDGE = [[[0.0, 0.0], [100.0, 0.0]]]
PAST_EDGE = [[[0.0, 0.0], [100.5, 0.0]]]


class WorkAreaDriverTests(unittest.TestCase):
    def test_work_area_is_the_rated_field_at_scale_1_1834(self):
        with fake_board(FakeLmcBoard(), scale=1.1834):
            self.assertAlmostEqual(live.effective_field_size(CFG), 236.68)
            self.assertEqual(live.work_area_size(CFG), 200.0)
        with fake_board(FakeLmcBoard(), scale=0.9):
            self.assertAlmostEqual(live.work_area_size(CFG), 180.0)

    def test_mark_accepts_100_mm_and_refuses_100_5_mm(self):
        board = FakeLmcBoard()
        with fake_board(board, scale=1.1834):
            live.mark_polylines(CFG, params(), EDGE, arm=True, confirm="ARM")
            sent = len(board.events)
            with self.assertRaisesRegex(ValueError, r"outside the 200\.00 mm work area \(lens rated field 200\.00 mm"):
                live.mark_polylines(CFG, params(), PAST_EDGE, arm=True, confirm="ARM")
        self.assertGreater(sent, 0)
        self.assertEqual(len(board.events), sent, "the refused job never reached the board")

    def test_frame_accepts_100_mm_and_refuses_100_5_mm(self):
        board = FakeLmcBoard()
        with fake_board(board, scale=1.1834):
            live.frame_polylines(CFG, params(), EDGE)
            with self.assertRaisesRegex(ValueError, "rated field 200.00 mm"):
                live.frame_polylines(CFG, params(), PAST_EDGE)
            loop = live.FrameLoop()
            with self.assertRaisesRegex(ValueError, "rated field 200.00 mm"):
                loop.start(CFG, params(), PAST_EDGE)
            self.assertFalse(loop.running)

    def test_test_box_past_the_work_area_is_refused_not_clamped(self):
        board = FakeLmcBoard()
        with fake_board(board, scale=1.1834):
            oversized = live.JobParams("", 1.0, 30.0, 200.0, 201.0, 1000.0)
            with self.assertRaisesRegex(ValueError, "work area"):
                live.frame_box(CFG, oversized)
            with self.assertRaisesRegex(ValueError, "work area"):
                live.mark_box(CFG, oversized, arm=True, confirm="ARM")
        self.assertEqual(board.events, [])


class WorkAreaPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        cfg_path = Path(self.tmp.name) / "markcfg7"
        cfg_path.write_text(SAMPLE_MARKCFG, encoding="latin-1")
        self.handler_cls = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": cfg_path})

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, polylines, layer_id="vector-engrave"):
        return {
            "machine_id": "jpt-m7-200",
            "objects": [{"layer_id": layer_id, "polylines": polylines}],
            "layers": [layer.to_dict() for layer in default_layers()],
            "live": {"power": 1, "frequency_khz": 30, "pulse_width_ns": 200, "mark_speed": 1000, "confirm": "ARM"},
        }

    def test_plan_and_mark_use_the_rated_field(self):
        with fake_board(FakeLmcBoard(), scale=1.1834), mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            patched.return_value = {"ok": True}
            self.assertEqual(call_mark(self.handler_cls, self.payload(EDGE))["status"], 200)
            plan = call_post(self.handler_cls, "/api/plan", self.payload(PAST_EDGE))["payload"]
            mark = call_mark(self.handler_cls, self.payload(PAST_EDGE))
        self.assertFalse(plan["safety"]["ok"])
        self.assertTrue(any("rated field 200.00 mm" in e for e in plan["safety"]["errors"]))
        self.assertEqual(mark["status"].value, 400)
        self.assertEqual(patched.call_count, 1)

    def test_frame_only_geometry_past_the_rated_field_fails_the_plan(self):
        with fake_board(FakeLmcBoard(), scale=1.1834):
            plan = call_post(self.handler_cls, "/api/plan", self.payload(PAST_EDGE, "frame-only"))["payload"]
        self.assertTrue(any(e.startswith("Framing:") and "rated field" in e for e in plan["safety"]["errors"]))

    def test_canvas_draws_the_enforced_work_area(self):
        self.assertIn("FIELD_W_MM = Math.min(reach, fieldNominalMm);", ui.HTML)


if __name__ == "__main__":
    unittest.main()
