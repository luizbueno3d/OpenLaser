from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from mopa_luiz import ui
from mopa_luiz.ui import UiHandler


def sample_object() -> dict[str, object]:
    return {
        "name": "square",
        "layer_id": "vector-engrave",
        "polylines": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]],
        "pointCount": 3,
        "x": 2.0,
        "y": 3.0,
        "scale": 1.5,
        "rotation": 45.0,
    }


class WorkspacePersistenceTest(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            with mock.patch.object(ui, "WORKSPACE_PATH", path):
                ui.save_workspace([sample_object()])
                loaded = ui.load_workspace()

        self.assertEqual(loaded["objects"], [sample_object()])
        self.assertIsInstance(loaded["saved_at"], str)

    def test_missing_and_corrupt_file_return_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            with mock.patch.object(ui, "WORKSPACE_PATH", path):
                self.assertEqual(ui.load_workspace(), {"objects": [], "saved_at": None})
                path.write_text("{not json", encoding="utf-8")
                self.assertEqual(ui.load_workspace(), {"objects": [], "saved_at": None})

    def test_sanitizer(self):
        payload = [
            {
                "name": "a" * 250,
                "layer_id": "cut",
                "polylines": [
                    [[0, 0], ["1", "1"], ["bad", 2], [2, float("nan")], [2, 2]],
                    [[9, 9]],
                ],
                "pointCount": 99,
                "x": "4",
                "y": float("inf"),
                "scale": "bad",
                "rotation": "-90",
                "extra": "drop",
            },
            {"name": "empty", "polylines": [[[0, 0], ["bad", 1]]]},
            "skip me",
        ]

        cleaned = ui.sanitize_workspace_objects(payload)

        self.assertEqual(len(cleaned), 1)
        self.assertEqual(set(cleaned[0]), {"name", "layer_id", "polylines", "pointCount", "x", "y", "scale", "rotation"})
        self.assertEqual(cleaned[0]["name"], "a" * 200)
        self.assertEqual(cleaned[0]["pointCount"], 3)
        self.assertEqual(cleaned[0]["polylines"], [[[0.0, 0.0], [1.0, 1.0], [2.0, 2.0]]])
        self.assertEqual(cleaned[0]["x"], 4.0)
        self.assertEqual(cleaned[0]["y"], 0.0)
        self.assertEqual(cleaned[0]["scale"], 1.0)
        self.assertEqual(cleaned[0]["rotation"], -90.0)
        with self.assertRaises(ValueError):
            ui.sanitize_workspace_objects("junk")
        with self.assertRaises(ValueError):
            ui.sanitize_workspace_objects([{}] * 3001)

    def test_atomic_write_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.json"
            with mock.patch.object(ui, "WORKSPACE_PATH", path):
                ui.save_workspace([sample_object()])
                self.assertFalse(path.with_suffix(".json.tmp").exists())


class WorkspaceHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        cls.workspace_path = Path(cls.tmpdir.name) / "workspace.json"
        cls.path_patch = mock.patch.object(ui, "WORKSPACE_PATH", cls.workspace_path)
        cls.path_patch.start()
        handler = type("WorkspaceUiHandler", (UiHandler,), {"markcfg": Path("missing")})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.path_patch.stop()
        cls.tmpdir.cleanup()

    def get(self, path: str) -> tuple[int, dict]:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as response:
            return response.status, json.loads(response.read() or b"{}")

    def post(self, path: str, payload: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_http_round_trip(self):
        status, body = self.post("/api/workspace/save", {"objects": [sample_object()]})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])

        status, body = self.get("/api/workspace")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["objects"], [sample_object()])
        self.assertIsInstance(body["saved_at"], str)

        status, body = self.post("/api/workspace/save", {"objects": "junk"})
        self.assertEqual(status, 400, body)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
