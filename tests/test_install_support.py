import unittest
from pathlib import Path
from unittest import mock

from mopa_luiz import cli, ui, usb_backend


class LibusbBackendTests(unittest.TestCase):
    def setUp(self):
        usb_backend._STATUS = None

    def tearDown(self):
        usb_backend._STATUS = None

    def test_system_libusb_is_preferred(self):
        with mock.patch("usb.backend.libusb1.get_backend", return_value=object()) as get_backend:
            status = usb_backend.ensure_libusb_backend()
        self.assertEqual(status, {"available": True, "source": "system"})
        get_backend.assert_called_once_with()

    def test_bundled_libusb_is_used_when_the_system_has_none(self):
        bundled = object()

        def fake_get_backend(find_library=None):
            return bundled if find_library is not None else None

        with mock.patch("usb.backend.libusb1.get_backend", side_effect=fake_get_backend):
            status = usb_backend.ensure_libusb_backend()
        self.assertEqual(status["source"], "bundled (libusb-package)")
        self.assertTrue(status["available"])

    def test_missing_libusb_is_reported_not_raised(self):
        with mock.patch("usb.backend.libusb1.get_backend", return_value=None):
            status = usb_backend.ensure_libusb_backend()
        self.assertFalse(status["available"])
        self.assertIn("installer", status["note"])


class DetectHintTests(unittest.TestCase):
    def test_usable_laser_needs_no_hint(self):
        self.assertIsNone(cli.detect_hint(seen_by_os=True, usable=True, libusb_ok=True))

    def test_windows_driver_hint(self):
        with mock.patch.object(cli.sys, "platform", "win32"):
            hint = cli.detect_hint(seen_by_os=True, usable=False, libusb_ok=True)
        self.assertIn("Zadig", hint)

    def test_linux_permission_hint(self):
        with mock.patch.object(cli.sys, "platform", "linux"):
            hint = cli.detect_hint(seen_by_os=True, usable=False, libusb_ok=True)
        self.assertIn("99-openlaser.rules", hint)

    def test_unplugged_laser_hint(self):
        hint = cli.detect_hint(seen_by_os=False, usable=False, libusb_ok=True)
        self.assertIn("USB cable", hint)


class SecondLaunchTests(unittest.TestCase):
    def test_second_launch_reuses_the_running_server(self):
        with mock.patch.object(ui, "openlaser_running", return_value=True), \
             mock.patch.object(ui, "open_default_browser", return_value=True) as browser, \
             mock.patch.object(ui, "ThreadingHTTPServer") as server:
            ui.serve("127.0.0.1", 8765, Path("profile.ini"), open_browser=True)
        server.assert_not_called()
        browser.assert_called_once_with("http://127.0.0.1:8765")

    def test_port_taken_by_another_program_gives_a_clear_error(self):
        with mock.patch.object(ui, "openlaser_running", return_value=False), \
             mock.patch.object(ui, "ThreadingHTTPServer", side_effect=OSError("in use")):
            with self.assertRaisesRegex(OSError, "--port"):
                ui.serve("127.0.0.1", 8765, Path("profile.ini"), open_browser=False)


if __name__ == "__main__":
    unittest.main()
