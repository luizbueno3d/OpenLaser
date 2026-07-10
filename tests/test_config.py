import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mopa_luiz.cli import (
    JPT_M7_PULSE_WIDTHS_NS,
    build_test_box_plan,
    load_markcfg,
    nearest_pulse_width,
)


SAMPLE_MARKCFG = textwrap.dedent("""\
    [LMC_CFG]
    LASERTYPE=4
    FIELDSIZE=200
    MINPWMFREQ=1000
    MAXPWMFREQ=4000000
    m_nIPGSerialNo=7
    m_bEnableIPGSetPulseWidth=1
    m_nEnableFiberLaserStateCheckInMarking=1
    REDLIGHTSPEED=3000
""")


class _CfgFixture:
    def __init__(self, text: str):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "markcfg7"
        self.path.write_text(text, encoding="latin-1")

    def cleanup(self):
        self._tmp.cleanup()


class ConfigParsingTests(unittest.TestCase):
    def test_parses_known_keys(self):
        fix = _CfgFixture(SAMPLE_MARKCFG)
        try:
            cfg = load_markcfg(fix.path)
            self.assertEqual(cfg.get_int("FIELDSIZE"), 200)
            self.assertEqual(cfg.get_int("MAXPWMFREQ"), 4_000_000)
            self.assertTrue(cfg.get_bool("m_bEnableIPGSetPulseWidth"))
            self.assertEqual(cfg.get_int("m_nIPGSerialNo"), 7)
        finally:
            fix.cleanup()

    def test_missing_section_raises(self):
        fix = _CfgFixture("[OTHER]\nFOO=1\n")
        try:
            with self.assertRaises(ValueError):
                load_markcfg(fix.path)
        finally:
            fix.cleanup()


class PulseWidthGuardTests(unittest.TestCase):
    def test_nearest_snaps_to_table(self):
        # Avoid exact-tie inputs; ties resolve to the first equal candidate.
        for raw, expected in [(0, 2), (5, 4), (7, 6), (199, 200), (430, 450), (999, 500)]:
            self.assertEqual(nearest_pulse_width(raw), expected)
            self.assertIn(nearest_pulse_width(raw), JPT_M7_PULSE_WIDTHS_NS)


class TestBoxPlanTests(unittest.TestCase):
    def setUp(self):
        self._fix = _CfgFixture(SAMPLE_MARKCFG)
        self.cfg = load_markcfg(self._fix.path)

    def tearDown(self):
        self._fix.cleanup()

    def test_blocks_power_above_100_percent(self):
        with self.assertRaises(ValueError):
            build_test_box_plan(
                self.cfg, power=101.0, frequency_khz=30.0,
                pulse_width_ns=200.0, size_mm=10.0,
            )

    def test_frequency_must_be_within_markcfg_bounds(self):
        with self.assertRaises(ValueError):
            build_test_box_plan(
                self.cfg, power=1.0, frequency_khz=0.1,
                pulse_width_ns=200.0, size_mm=10.0,
            )

    def test_pulse_width_is_snapped(self):
        plan = build_test_box_plan(
            self.cfg, power=1.0, frequency_khz=30.0,
            pulse_width_ns=199.0, size_mm=10.0,
        )
        self.assertEqual(plan["effective_pulse_width_ns"], 200)
        self.assertTrue(plan["pulse_width_was_snapped"])


if __name__ == "__main__":
    unittest.main()
