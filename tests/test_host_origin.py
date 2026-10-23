"""Only OpenLaser's own page, reached over loopback, may drive the server.

A web page using DNS rebinding resolves its own hostname to 127.0.0.1, so the
browser lets its scripts call /api/plan, read the ARM nonce, and replay it to
/api/mark. The Host header still names the page's hostname, and Origin names
the page on every POST: both must be this server over loopback.
"""

from __future__ import annotations

import http.client
import inspect
import json
import re
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mopa_luiz import ui
from mopa_luiz.layers import default_layers
from tests.test_planner import SAMPLE_MARKCFG, TEST_PORT, call_get, call_post

LIVE = {"power": 1, "frequency_khz": 30, "pulse_width_ns": 200, "mark_speed": 1000, "confirm": "ARM"}
REBINDING = {"Host": f"attacker.example:{TEST_PORT}", "Origin": f"http://attacker.example:{TEST_PORT}"}


def routes(method) -> list[str]:
    """Every path the handler serves for this method, read from its source."""
    return re.findall(r'path == "(/[^"]*)"', inspect.getsource(method))


class HandlerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg_path = Path(self.tmp.name) / "markcfg7"
        cfg_path.write_text(SAMPLE_MARKCFG, encoding="latin-1")
        self.handler_cls = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": cfg_path})
        self.payload = {
            "machine_id": "jpt-m7-200",
            "objects": [{"layer_id": "vector-engrave", "polylines": [[(0, 0), (5, 0), (5, 5), (0, 0)]]}],
            "layers": [layer.to_dict() for layer in default_layers()],
            "live": LIVE,
        }
        # If a refusal ever slipped, nothing may reach the board or the
        # owner's saved calibration, layers or workspace.
        self.hardware = self.patch("run_hardware_job", return_value={"ok": True})
        self.frame_loop = self.patch("FRAME_LOOP")
        self.stop_jobs = self.patch("stop_active_hardware_jobs", return_value={"ok": True})
        self.board = self.patch("read_board_status", return_value={})
        self.patch("detect_report", return_value={})
        self.patch("pyusb_probe", return_value={"devices": []})
        self.patch("save_scale_correction", return_value=1.0)
        self.patch("save_layers", return_value=[])
        self.patch("save_workspace", return_value={"ok": True})

    def patch(self, name, **kwargs):
        patcher = mock.patch.object(ui, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def armed(self, nonce):
        return {**self.payload, "arm_nonce": nonce, "live": {**LIVE, "arm": True}}

    def assertRefused(self, result):
        self.assertEqual(result["status"].value, 403, result)
        self.assertTrue(result["payload"]["error"].startswith("Request refused: "), result)
        self.assertNotIn("arm_nonce", result["payload"])


class RebindingAttackTests(HandlerTestCase):
    def test_rebinding_page_cannot_plan_then_mark(self):
        plan = call_post(self.handler_cls, "/api/plan", self.payload, headers=REBINDING)
        self.assertRefused(plan)
        self.assertRefused(call_post(self.handler_cls, "/api/mark", self.armed(None), headers=REBINDING))
        self.hardware.assert_not_called()

    def test_rebinding_page_cannot_spend_a_real_nonce(self):
        nonce = call_post(self.handler_cls, "/api/plan", self.payload)["payload"]["arm_nonce"]
        self.assertRefused(call_post(self.handler_cls, "/api/mark", self.armed(nonce), headers=REBINDING))
        self.hardware.assert_not_called()
        # Refused before the route runs: the operator's own confirmation still works.
        self.assertEqual(call_post(self.handler_cls, "/api/mark", self.armed(nonce))["status"], 200)
        self.assertEqual(self.hardware.call_count, 1)

    def test_rebinding_page_cannot_run_the_first_burn_test(self):
        self.assertRefused(call_post(self.handler_cls, "/api/test/preview", {"kind": "dot"}, headers=REBINDING))
        run = {"kind": "dot", "arm": True, "confirm": "ARM", "arm_nonce": None}
        self.assertRefused(call_post(self.handler_cls, "/api/test/run", run, headers=REBINDING))
        self.hardware.assert_not_called()

    def test_every_post_route_refuses_a_foreign_host(self):
        paths = routes(ui.UiHandler.do_POST)
        for path in ("/api/plan", "/api/mark", "/api/test/run", "/api/frame", "/api/frame/start",
                     "/api/frame/stop", "/api/stop", "/api/calibration/save"):
            self.assertIn(path, paths)
        for path in paths + ["/api/no-such-route"]:
            with self.subTest(path=path):
                self.assertRefused(call_post(self.handler_cls, path, self.armed(None), headers=REBINDING))
        self.hardware.assert_not_called()
        self.stop_jobs.assert_not_called()
        self.assertEqual(self.frame_loop.mock_calls, [])

    def test_every_get_route_refuses_a_foreign_host(self):
        paths = routes(ui.UiHandler.do_GET)
        for path in ("/", "/api/health", "/api/board", "/api/config"):
            self.assertIn(path, paths)
        for path in paths + ["/api/no-such-route"]:
            with self.subTest(path=path):
                self.assertRefused(call_get(self.handler_cls, path, headers={"Host": REBINDING["Host"]}))
        self.board.assert_not_called()


class HostHeaderTests(HandlerTestCase):
    def test_loopback_names_for_the_bound_port_are_answered(self):
        for host in (f"127.0.0.1:{TEST_PORT}", f"localhost:{TEST_PORT}", f"[::1]:{TEST_PORT}",
                     f"LocalHost:{TEST_PORT}"):
            with self.subTest(host=host):
                result = call_get(self.handler_cls, "/api/health", headers={"Host": host})
                self.assertEqual(result["status"], 200, result)
                self.assertTrue(result["payload"]["ok"])

    def test_other_hosts_are_refused(self):
        for host in (None, "", "127.0.0.1", "localhost", f"127.0.0.1:{TEST_PORT + 1}",
                     f"localhost.:{TEST_PORT}", f"app.localhost:{TEST_PORT}",
                     f"127.0.0.1.nip.io:{TEST_PORT}", f"0.0.0.0:{TEST_PORT}",
                     f"192.168.1.5:{TEST_PORT}", f"127.0.0.2:{TEST_PORT}",
                     f"[::ffff:127.0.0.1]:{TEST_PORT}", f"attacker.example:{TEST_PORT}"):
            with self.subTest(host=host):
                self.assertRefused(call_get(self.handler_cls, "/api/health", headers={"Host": host}))
                self.assertRefused(call_post(self.handler_cls, "/api/plan", self.payload, headers={"Host": host}))

    def test_port_80_may_be_left_out(self):
        self.assertIn("localhost", ui.loopback_authorities(80))
        self.assertIn("localhost:80", ui.loopback_authorities(80))
        self.assertNotIn("localhost", ui.loopback_authorities(TEST_PORT))


class OriginHeaderTests(HandlerTestCase):
    def plan(self, origin, host=f"127.0.0.1:{TEST_PORT}"):
        return call_post(self.handler_cls, "/api/plan", self.payload, headers={"Host": host, "Origin": origin})

    def test_own_page_may_post(self):
        self.assertTrue(self.plan(f"http://127.0.0.1:{TEST_PORT}")["payload"]["arm_nonce"])
        self.assertTrue(self.plan(f"http://localhost:{TEST_PORT}", host=f"localhost:{TEST_PORT}")["payload"]["arm_nonce"])
        self.assertTrue(self.plan(f"http://[::1]:{TEST_PORT}", host=f"[::1]:{TEST_PORT}")["payload"]["arm_nonce"])

    def test_scripts_without_origin_may_post(self):
        result = call_post(self.handler_cls, "/api/plan", self.payload, headers={"Origin": None})
        self.assertTrue(result["payload"]["arm_nonce"])

    def test_other_web_pages_are_refused(self):
        for origin in ("https://attacker.example", f"http://attacker.example:{TEST_PORT}", "null", "",
                       f"https://127.0.0.1:{TEST_PORT}", f"http://127.0.0.1:{TEST_PORT + 1}",
                       "http://127.0.0.1", f"http://localhost:{TEST_PORT}",
                       f"http://127.0.0.1:{TEST_PORT}.attacker.example"):
            with self.subTest(origin=origin):
                self.assertRefused(self.plan(origin))

    def test_cross_site_post_cannot_frame_or_stop(self):
        # A plain cross-site form or fetch reaches 127.0.0.1 directly: Host
        # passes, Origin does not.
        evil = {"Origin": "https://attacker.example"}
        for path in ("/api/frame", "/api/frame/start", "/api/stop", "/api/calibration/save"):
            with self.subTest(path=path):
                self.assertRefused(call_post(self.handler_cls, path, self.payload, headers=evil))
        self.hardware.assert_not_called()
        self.stop_jobs.assert_not_called()
        self.assertEqual(self.frame_loop.mock_calls, [])


class LoopbackServerTests(unittest.TestCase):
    """The real server, real header parsing: heartbeat and second-launch probe keep working."""

    @classmethod
    def setUpClass(cls):
        cls.usb = mock.patch.object(ui, "pyusb_probe", return_value={"devices": []})
        cls.usb.start()
        handler = type("ConfiguredUiHandler", (ui.UiHandler,), {"markcfg": Path("missing")})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.usb.stop()

    def request(self, method, path, headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=b"{}" if method == "POST" else None, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()

    def test_openlaser_running_sees_this_server(self):
        self.assertTrue(ui.openlaser_running(ui.browser_url("127.0.0.1", self.port)))
        self.assertTrue(ui.openlaser_running(ui.browser_url("0.0.0.0", self.port)))

    def test_heartbeat_from_own_page(self):
        status, body = self.request("GET", "/api/health", {"Host": f"127.0.0.1:{self.port}"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])

    def test_rebinding_host_is_refused_on_the_wire(self):
        status, body = self.request("GET", "/api/health", {"Host": f"attacker.example:{self.port}"})
        self.assertEqual(status, 403, body)
        status, body = self.request("POST", "/api/frame/stop", {
            "Host": f"attacker.example:{self.port}", "Origin": f"http://attacker.example:{self.port}",
        })
        self.assertEqual(status, 403, body)

    def test_cross_site_origin_is_refused_on_the_wire(self):
        status, body = self.request("POST", "/api/frame/stop", {
            "Host": f"127.0.0.1:{self.port}", "Origin": "https://attacker.example",
        })
        self.assertEqual(status, 403, body)


if __name__ == "__main__":
    unittest.main()
