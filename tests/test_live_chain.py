"""End-to-end smoke tests for the UI -> HTTP -> spawn -> galvo command chain.

These run against galvoplotter's MockConnection (OPENMOPA_MOCK=1): no hardware
is touched, but route dispatch, safety gating, hardware-job process spawning,
and controller command generation are exercised for real.
"""

from __future__ import annotations

import json
import os
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mopa_luiz import live
from mopa_luiz.cli import MarkConfig
from mopa_luiz.live import JobParams, connect_controller
from mopa_luiz.ui import UiHandler


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE = REPO_ROOT / "profiles" / "openmopa-machine.ini"

LIVE = {
    "text": "",
    "power": 1,
    "frequency_khz": 30,
    "pulse_width_ns": 200,
    "mark_speed": 1000,
    "size_mm": 10,
    "repeat_count": 1,
    "confirm": "",
    "arm": False,
}


class MockModeTest(unittest.TestCase):
    def test_mock_mode_env_flag(self):
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}):
            self.assertTrue(live.mock_mode())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(live.mock_mode())

    def test_connect_controller_mock_connects(self):
        cfg = MarkConfig(path=Path("mem"), values={"FIELDSIZE": "200"})
        params = JobParams(
            text="",
            power=1.0,
            frequency_khz=30.0,
            pulse_width_ns=200.0,
            size_mm=10.0,
            mark_speed=1000.0,
        )
        with mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"}):
            controller = connect_controller(cfg, params)
        try:
            self.assertTrue(controller.mock)
        finally:
            controller._sending = False
            controller.shutdown()


class StubHangingController:
    """Mimics a wedged board: claims the interface, then never answers."""

    def __init__(self, **kwargs):
        self._usb_log = kwargs.get("usb_log") or (lambda message: None)
        self.mock = kwargs.get("mock", False)
        self._sending = True
        self.abort_requested = False

    def connect_if_needed(self):
        self._usb_log("Attempting to claim interface.")
        self._usb_log("Interface claim: Success")
        time.sleep(2.0)

    def abort_connect(self):
        self.abort_requested = True

    def shutdown(self):
        pass


class WedgeProtectionTest(unittest.TestCase):
    def test_connect_timeout_reports_wedged_board(self):
        cfg = MarkConfig(path=Path("mem"), values={"FIELDSIZE": "200"})
        params = JobParams(
            text="",
            power=1.0,
            frequency_khz=30.0,
            pulse_width_ns=200.0,
            size_mm=10.0,
            mark_speed=1000.0,
        )
        with mock.patch.object(live, "GalvoController", StubHangingController):
            with self.assertRaises(RuntimeError) as ctx:
                connect_controller(cfg, params, connect_timeout_s=0.2)
        self.assertIn("Power-cycle", str(ctx.exception))
        self.assertIn("not responding", str(ctx.exception))

    def test_graceful_stop_shuts_down_active_controller(self):
        controller = mock.Mock()
        with mock.patch.object(live, "_ACTIVE_CONTROLLER", controller):
            live._graceful_hardware_stop()
        controller.shutdown.assert_called_once()

    def test_graceful_stop_without_controller_is_a_no_op(self):
        with mock.patch.object(live, "_ACTIVE_CONTROLLER", None):
            live._graceful_hardware_stop()  # must not raise


class UiChainSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._env = mock.patch.dict(os.environ, {"OPENMOPA_MOCK": "1"})
        cls._env.start()
        handler = type("ConfiguredUiHandler", (UiHandler,), {"markcfg": PROFILE})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls._env.stop()

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_health_endpoint_is_fast_and_typed(self):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/health")
        started = time.monotonic()
        with urllib.request.urlopen(request, timeout=10) as response:
            body = json.loads(response.read())
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertTrue(body["ok"])
        self.assertIn("laser_connected", body)
        self.assertIn("frame_loop_running", body)
        self.assertTrue(body["mock"])

    def test_frame_survives_stop_click(self):
        status, body = self.post("/api/frame", {"objects": [], "layers": [], "live": LIVE})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        self.assertTrue(body.get("mock"), body)

        status, body = self.post("/api/stop", {})
        self.assertEqual(status, 200, body)

        # Regression: a Stop click used to latch STOP_REQUESTED forever,
        # making every later frame/test job fail with "hardware stop requested".
        status, body = self.post("/api/frame", {"objects": [], "layers": [], "live": LIVE})
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)

    def test_mark_reaches_galvo_layer(self):
        payload = {
            "objects": [
                {
                    "name": "square",
                    "layer_id": "vector-engrave",
                    "polylines": [
                        [[-5.0, -5.0], [5.0, -5.0], [5.0, 5.0], [-5.0, 5.0], [-5.0, -5.0]]
                    ],
                }
            ],
            "layers": [],
            "live": {**LIVE, "arm": True, "confirm": "ARM"},
        }
        status, body = self.post("/api/mark", payload)
        self.assertEqual(status, 200, body)
        self.assertTrue(body.get("ok"), body)
        passes = body.get("passes") or []
        self.assertGreaterEqual(len(passes), 1, body)
        for item in passes:
            self.assertTrue(item.get("emission_enabled"), item)
            self.assertTrue(item.get("mock"), item)


if __name__ == "__main__":
    unittest.main()
