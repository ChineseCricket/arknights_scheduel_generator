from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from arknights_schedule_generator.data import REQUIRED_FILES
from arknights_schedule_generator.desktop_launcher import (
    APP_DEFAULTS_MARKER,
    is_app_server,
    server_command,
)
from arknights_schedule_generator.web_app import default_payload


class WebAppDefaultPayloadTest(unittest.TestCase):
    def test_reports_missing_data_cache_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            payload = default_payload(Path(temp_dir))

        self.assertEqual(payload["application"], APP_DEFAULTS_MARKER)
        self.assertFalse(payload["dataCache"]["ready"])
        self.assertEqual(payload["dataCache"]["missingFiles"], list(REQUIRED_FILES))

    def test_reports_ready_data_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir) / "data" / "cache"
            data_dir.mkdir(parents=True)
            for file_name in REQUIRED_FILES:
                (data_dir / file_name).write_text("{}", encoding="utf-8")

            payload = default_payload(Path(temp_dir))

        self.assertTrue(payload["dataCache"]["ready"])
        self.assertEqual(payload["dataCache"]["missingFiles"], [])


class DesktopLauncherTest(unittest.TestCase):
    def test_recognizes_marked_app_server(self) -> None:
        with patch(
            "arknights_schedule_generator.desktop_launcher.request_defaults",
            return_value={"application": APP_DEFAULTS_MARKER},
        ):
            self.assertTrue(is_app_server("http://127.0.0.1:8765/"))

    def test_source_server_command_runs_launcher_server_mode(self) -> None:
        command = server_command("127.0.0.1", 8765, Path("C:/tmp/example"))

        self.assertIn("-m", command)
        self.assertIn("arknights_schedule_generator.desktop_launcher", command)
        self.assertIn("--server", command)
        self.assertIn("--root", command)


if __name__ == "__main__":
    unittest.main()
