import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from mopa_luiz import live
from mopa_luiz.cli import MarkConfig


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.calibration_path = Path(self.tmp.name) / "calibration.json"
        self.patch = mock.patch.object(live, "CALIBRATION_PATH", self.calibration_path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_missing_file_defaults_to_one(self):
        self.assertEqual(live.load_scale_correction(), 1.0)

    def test_saved_value_round_trips(self):
        self.assertEqual(live.save_scale_correction(1.1834), 1.1834)
        self.assertEqual(live.load_scale_correction(), 1.1834)

    def test_corrupt_json_defaults_to_one(self):
        self.calibration_path.write_text("{", encoding="utf-8")
        self.assertEqual(live.load_scale_correction(), 1.0)

    def test_non_numeric_json_value_defaults_to_one(self):
        self.calibration_path.write_text(
            json.dumps({"scale_correction": "abc"}), encoding="utf-8"
        )
        self.assertEqual(live.load_scale_correction(), 1.0)

    def test_non_positive_json_value_defaults_to_one(self):
        for value in (0, -1):
            self.calibration_path.write_text(
                json.dumps({"scale_correction": value}), encoding="utf-8"
            )
            self.assertEqual(live.load_scale_correction(), 1.0)

    def test_file_value_is_clamped(self):
        self.calibration_path.write_text(
            json.dumps({"scale_correction": 5.0}), encoding="utf-8"
        )
        self.assertEqual(live.load_scale_correction(), 2.0)
        self.calibration_path.write_text(
            json.dumps({"scale_correction": 0.1}), encoding="utf-8"
        )
        self.assertEqual(live.load_scale_correction(), 0.5)

    def test_save_rejects_invalid_values(self):
        with self.assertRaises(ValueError):
            live.save_scale_correction(2.5)
        with self.assertRaises(TypeError):
            live.save_scale_correction(None)

    def test_effective_field_size_uses_scale_correction(self):
        cfg = MarkConfig(path=Path("x"), values={"FIELDSIZE": "200"})
        self.assertEqual(live.effective_field_size(cfg), 200.0)
        live.save_scale_correction(1.1834)
        self.assertAlmostEqual(live.effective_field_size(cfg), 236.68)
        default_cfg = MarkConfig(path=Path("x"), values={})
        self.assertAlmostEqual(live.effective_field_size(default_cfg), 236.68)


if __name__ == "__main__":
    unittest.main()
