"""B5: ARM is a server-side, single-use nonce bound to the confirmed job."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mopa_luiz import ui
from mopa_luiz.layers import default_layers
from tests.test_planner import SAMPLE_MARKCFG, call_post

LIVE = {"power": 1, "frequency_khz": 30, "pulse_width_ns": 200, "mark_speed": 1000, "confirm": "ARM"}


class ArmNonceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        cfg_path = Path(self.tmp.name) / "markcfg7"
        cfg_path.write_text(SAMPLE_MARKCFG, encoding="latin-1")
        self.handler_cls = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": cfg_path})
        self.payload = {
            "machine_id": "jpt-m7-200",
            "objects": [{"layer_id": "vector-engrave", "polylines": [[(0, 0), (5, 0), (5, 5), (0, 0)]]}],
            "layers": [layer.to_dict() for layer in default_layers()],
            "live": LIVE,
        }
        patcher = mock.patch("mopa_luiz.ui.run_hardware_job", return_value={"ok": True})
        self.hardware = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def plan_nonce(self, payload=None) -> str:
        body = call_post(self.handler_cls, "/api/plan", payload or self.payload)["payload"]
        self.assertTrue(body["arm_nonce"])
        return body["arm_nonce"]

    def mark(self, nonce, payload=None):
        payload = payload or self.payload
        armed = {**payload, "arm_nonce": nonce, "live": {**payload["live"], "arm": True}}
        return call_post(self.handler_cls, "/api/mark", armed)

    def test_confirmed_job_marks_once(self):
        result = self.mark(self.plan_nonce())
        self.assertEqual(result["status"], 200, result)
        self.assertEqual(self.hardware.call_count, 1)

    def test_missing_nonce_is_refused(self):
        result = self.mark(None)
        self.assertEqual(result["status"].value, 403)
        self.hardware.assert_not_called()

    def test_replayed_nonce_is_refused(self):
        nonce = self.plan_nonce()
        self.assertEqual(self.mark(nonce)["status"], 200)
        replay = self.mark(nonce)
        self.assertEqual(replay["status"].value, 403)
        self.assertEqual(self.hardware.call_count, 1)

    def test_expired_nonce_is_refused(self):
        with mock.patch.object(ui, "ARM_NONCE_TTL_S", -1.0):
            nonce = self.plan_nonce()
        result = self.mark(nonce)
        self.assertEqual(result["status"].value, 403)
        self.assertIn("expired", result["payload"]["error"])
        self.hardware.assert_not_called()

    def test_changed_payload_is_refused(self):
        nonce = self.plan_nonce()
        layers = [dict(layer) for layer in self.payload["layers"]]
        layers[0]["power_percent"] = 90.0
        result = self.mark(nonce, {**self.payload, "layers": layers})
        self.assertEqual(result["status"].value, 403)
        self.assertIn("changed", result["payload"]["error"])
        self.hardware.assert_not_called()

    def test_moved_geometry_is_refused(self):
        nonce = self.plan_nonce()
        moved = [{"layer_id": "vector-engrave", "polylines": [[(1, 0), (5, 0), (5, 5), (1, 0)]]}]
        self.assertEqual(self.mark(nonce, {**self.payload, "objects": moved})["status"].value, 403)

    def test_nonce_is_spent_even_when_the_job_is_then_refused(self):
        nonce = self.plan_nonce()
        with mock.patch("mopa_luiz.ui.validate_layers", return_value={"x": ["bad"]}):
            self.assertEqual(self.mark(nonce)["status"].value, 400)
        self.assertEqual(self.mark(nonce)["status"].value, 403)

    def test_arm_flag_and_text_are_not_part_of_the_binding(self):
        nonce = self.plan_nonce({**self.payload, "live": {**LIVE, "confirm": ""}})
        self.assertEqual(self.mark(nonce)["status"], 200)

    def test_first_burn_test_needs_its_own_nonce(self):
        dot = call_post(self.handler_cls, "/api/test/preview", {"kind": "dot"})["payload"]["arm_nonce"]
        line = {"kind": "line", "arm": True, "confirm": "ARM"}
        self.assertEqual(call_post(self.handler_cls, "/api/test/run", {**line, "arm_nonce": dot})["status"].value, 403)
        self.assertEqual(call_post(self.handler_cls, "/api/test/run", {**line})["status"].value, 403)
        fresh = call_post(self.handler_cls, "/api/test/preview", {"kind": "line"})["payload"]["arm_nonce"]
        self.assertEqual(call_post(self.handler_cls, "/api/test/run", {**line, "arm_nonce": fresh})["status"], 200)
        self.assertEqual(self.hardware.call_count, 1)


class ArmClientSourceTests(unittest.TestCase):
    def block(self, start: str, end: str) -> str:
        return ui.HTML[ui.HTML.index(start):ui.HTML.index(end)]

    def test_live_params_never_carry_arm(self):
        self.assertNotIn("arm:", self.block("function liveParams()", "async function postJson"))

    def test_arm_is_sent_only_from_the_confirm_buttons(self):
        mark = self.block("document.getElementById('mark').addEventListener", "document.getElementById('dotTest')")
        self.assertIn("arm_nonce: preview.arm_nonce", mark)
        self.assertIn("arm: true", mark.split("onConfirm")[1])
        self.assertNotIn("arm: true", mark.split("onConfirm")[0])

    def test_closing_the_dialog_clears_the_arm_field(self):
        modal = self.block("function openModal(", "document.getElementById('mark').addEventListener")
        self.assertIn("armField.value = ''", modal)
        self.assertIn("cancel.onclick = close", modal)


if __name__ == "__main__":
    unittest.main()
