"""End-to-end planner tests: uses ui.UiHandler.do_POST via a mocked transport.

We exercise the /api/plan and /api/mark code paths without running the
HTTP server or contacting the controller. Marking is patched at the
`run_hardware_job` boundary, since the test environment has no laser.
"""

import io
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mopa_luiz import ui
from mopa_luiz.layers import default_layers


SAMPLE_MARKCFG = textwrap.dedent("""\
    [LMC_CFG]
    FIELDSIZE=200
    MINPWMFREQ=1000
    MAXPWMFREQ=4000000
    m_nIPGSerialNo=7
    m_bEnableIPGSetPulseWidth=1
""")


class FakeRequest:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)

    def makefile(self, mode, *args, **kwargs):
        return self._buf if "r" in mode else io.BytesIO()


def call_post(handler_cls, path, payload):
    body = json.dumps(payload).encode("utf-8")
    request = FakeRequest(
        f"POST {path} HTTP/1.1\r\nContent-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
    )
    response = io.BytesIO()
    rfile = io.BytesIO(body)
    handler = handler_cls.__new__(handler_cls)
    handler.rfile = rfile
    handler.wfile = response
    handler.headers = {"content-length": str(len(body))}
    handler.path = path
    handler.command = "POST"
    handler.client_address = ("127.0.0.1", 0)
    handler.request_version = "HTTP/1.1"
    handler.server = mock.MagicMock()
    handler.send_response = mock.MagicMock()
    handler.send_header = mock.MagicMock()
    handler.end_headers = mock.MagicMock()
    captured = {}
    def send_json(status, payload_):
        captured["status"] = status
        captured["payload"] = payload_
    handler.send_json = send_json
    handler.do_POST()
    return captured


class PlanAndMarkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "markcfg7"
        self.cfg_path.write_text(SAMPLE_MARKCFG, encoding="latin-1")
        self.handler_cls = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": self.cfg_path})

    def tearDown(self):
        self.tmp.cleanup()

    def _payload(self, *, objects=None, live=None, layers=None):
        return {
            "machine_id": "jpt-m7-200",
            "objects": objects if objects is not None else [
                {"layer_id": "vector-engrave", "polylines": [[(0, 0), (5, 0), (5, 5), (0, 0)]]},
            ],
            "layers": [layer.to_dict() for layer in (layers if layers is not None else default_layers())],
            "live": live if live is not None else {
                "power": 1, "frequency_khz": 30, "pulse_width_ns": 200,
                "mark_speed": 1000, "confirm": "ARM", "arm": True,
            },
        }

    def test_plan_groups_by_layer(self):
        result = call_post(self.handler_cls, "/api/plan", self._payload())
        self.assertEqual(result["status"], 200)
        body = result["payload"]
        self.assertEqual(body["object_count"], 1)
        self.assertEqual(body["emitting_layers"], 1)
        self.assertGreaterEqual(body["bbox"]["width"], 4.99)

    def test_plan_with_no_geometry_does_not_crash(self):
        result = call_post(self.handler_cls, "/api/plan", self._payload(objects=[]))
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["payload"]["object_count"], 0)
        self.assertEqual(result["payload"]["emitting_layers"], 0)

    def test_diode_plan_and_export_use_gcode_without_mopa_safety_fields(self):
        payload = self._payload()
        payload["machine_id"] = "diode-grbl-400x400"
        result = call_post(self.handler_cls, "/api/plan", payload)
        self.assertEqual(result["status"], 200)
        self.assertTrue(result["payload"]["safety"]["ok"])
        self.assertEqual(result["payload"]["mode"], "gcode-preview")

        exported = call_post(self.handler_cls, "/api/export/gcode", payload)
        self.assertEqual(exported["status"], 200)
        self.assertIn("M4 S", exported["payload"]["content"])

    def test_direct_mark_is_refused_for_export_only_machine(self):
        payload = self._payload()
        payload["machine_id"] = "plotter-grbl-a4"
        result = call_post(self.handler_cls, "/api/mark", payload)
        self.assertEqual(result["status"].value, 400)
        self.assertIn("export G-code", result["payload"]["error"])

    def test_mark_blocked_without_arm(self):
        payload = self._payload(live={
            "power": 1, "frequency_khz": 30, "pulse_width_ns": 200,
            "mark_speed": 1000, "confirm": "", "arm": False,
        })
        with mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            patched.side_effect = PermissionError('emission requires arm=true and confirmation text "ARM"')
            result = call_post(self.handler_cls, "/api/mark", payload)
        self.assertEqual(result["status"].value, 403)
        self.assertIn("arm=true", result["payload"]["error"])

    def test_mark_allows_layer_at_100_percent(self):
        layers = default_layers()
        for layer in layers:
            if layer.operation == "vector_cut":
                layer.power_percent = 100
        with mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            patched.return_value = {"ok": True}
            result = call_post(self.handler_cls, "/api/mark", self._payload(layers=layers))
        self.assertEqual(result["status"], 200)

    def test_mark_runs_per_emitting_layer(self):
        with mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            patched.return_value = {"ok": True}
            result = call_post(self.handler_cls, "/api/mark", self._payload())
            self.assertEqual(result["status"], 200)
            self.assertEqual(patched.call_count, 1)

    def test_raster_layer_emits_hatch_lines_for_closed_shape(self):
        layers = default_layers()
        for layer in layers:
            if layer.operation == "raster_engrave":
                layer.output = True
                layer.raster_pitch_mm = 1.0  # 1 mm pitch -> ~10 hatch lines per pass
                layer.passes = 1
        # 10 mm closed square on the raster layer.
        square = [(-5, -5), (5, -5), (5, 5), (-5, 5), (-5, -5)]
        objects = [{"layer_id": "raster", "polylines": [square]}]
        with mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            patched.return_value = {"ok": True}
            result = call_post(self.handler_cls, "/api/mark",
                               self._payload(layers=layers, objects=objects))
        self.assertEqual(result["status"], 200)
        # Raster goes through a single hardware job carrying the hatch
        # segments, not the original closed shape.
        self.assertEqual(patched.call_count, 1)
        emitted_polylines = patched.call_args.args[3]
        self.assertGreater(len(emitted_polylines), 5)
        for seg in emitted_polylines:
            self.assertEqual(len(seg), 2)  # every emitted entry is a 2-pt hatch line

    def test_raster_layer_with_open_path_skips_emission(self):
        layers = default_layers()
        for layer in layers:
            if layer.operation == "raster_engrave":
                layer.output = True
        # Open path: not closed, so no hatch fill possible.
        objects = [{"layer_id": "raster", "polylines": [[(0, 0), (5, 0)]]}]
        with mock.patch("mopa_luiz.ui.run_hardware_job") as patched:
            result = call_post(self.handler_cls, "/api/mark",
                               self._payload(layers=layers, objects=objects))
        self.assertEqual(result["status"], 200)
        patched.assert_not_called()
        passes = result["payload"]["passes"]
        self.assertTrue(any(r.get("operation") == "raster_skip" for r in passes))


if __name__ == "__main__":
    unittest.main()
