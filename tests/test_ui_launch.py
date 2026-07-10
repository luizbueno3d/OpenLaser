import unittest
from unittest import mock

from mopa_luiz.cli import build_parser
from mopa_luiz.ui import browser_url, open_default_browser


class BrowserUrlTests(unittest.TestCase):
    def test_normal_local_address(self):
        self.assertEqual(browser_url("127.0.0.1", 8765), "http://127.0.0.1:8765")

    def test_wildcard_bind_uses_reachable_loopback_address(self):
        self.assertEqual(browser_url("0.0.0.0", 9000), "http://127.0.0.1:9000")
        self.assertEqual(browser_url("::", 9000), "http://[::1]:9000")

    @mock.patch("mopa_luiz.ui.webbrowser.open", return_value=True)
    def test_default_browser_opens_a_new_tab(self, patched_open):
        self.assertTrue(open_default_browser("http://127.0.0.1:8765"))
        patched_open.assert_called_once_with(
            "http://127.0.0.1:8765", new=2, autoraise=True
        )

    @mock.patch("mopa_luiz.ui.webbrowser.open", side_effect=RuntimeError("no browser"))
    def test_browser_failure_does_not_crash_server_startup(self, _patched_open):
        self.assertFalse(open_default_browser("http://127.0.0.1:8765"))


class BrowserCliTests(unittest.TestCase):
    def test_ui_opens_browser_by_default(self):
        args = build_parser().parse_args(["ui"])
        self.assertFalse(args.no_browser)

    def test_ui_can_disable_browser_opening(self):
        args = build_parser().parse_args(["ui", "--no-browser"])
        self.assertTrue(args.no_browser)


if __name__ == "__main__":
    unittest.main()
