from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from arknights_schedule_generator.data import REQUIRED_FILES
from arknights_schedule_generator.desktop_launcher import (
    APP_DEFAULTS_MARKER,
    is_app_server,
    prepare_runtime_root,
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

    def test_prepare_runtime_root_copies_bundled_data_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "runtime"
            bundle_dir = Path(temp_dir) / "bundle"
            source_dir = bundle_dir / "data" / "cache"
            source_dir.mkdir(parents=True)
            for file_name in REQUIRED_FILES:
                (source_dir / file_name).write_text(f"bundled:{file_name}", encoding="utf-8")

            with patch("sys._MEIPASS", str(bundle_dir), create=True):
                prepare_runtime_root(root_dir)

            for file_name in REQUIRED_FILES:
                self.assertEqual(
                    (root_dir / "data" / "cache" / file_name).read_text(encoding="utf-8"),
                    f"bundled:{file_name}",
                )

    def test_prepare_runtime_root_does_not_overwrite_existing_data_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "runtime"
            target_dir = root_dir / "data" / "cache"
            target_dir.mkdir(parents=True)
            existing_file = REQUIRED_FILES[0]
            (target_dir / existing_file).write_text("user-cache", encoding="utf-8")
            bundle_dir = Path(temp_dir) / "bundle"
            source_dir = bundle_dir / "data" / "cache"
            source_dir.mkdir(parents=True)
            for file_name in REQUIRED_FILES:
                (source_dir / file_name).write_text(f"bundled:{file_name}", encoding="utf-8")

            with patch("sys._MEIPASS", str(bundle_dir), create=True):
                prepare_runtime_root(root_dir)

            self.assertEqual((target_dir / existing_file).read_text(encoding="utf-8"), "user-cache")
            for file_name in REQUIRED_FILES[1:]:
                self.assertEqual(
                    (target_dir / file_name).read_text(encoding="utf-8"),
                    f"bundled:{file_name}",
                )


if __name__ == "__main__":
    unittest.main()
