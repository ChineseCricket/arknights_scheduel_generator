from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError
import unittest

from arknights_schedule_generator.data import (
    DataDownloadError,
    DataSource,
    REQUIRED_FILES,
    download_data,
)


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def read(self) -> bytes:
        return self.data


class GameDataDownloadTest(unittest.TestCase):
    def test_download_data_falls_back_after_primary_source_429(self) -> None:
        sources = (
            DataSource("primary", "https://primary.example/data"),
            DataSource("backup", "https://backup.example/data"),
        )
        calls: list[str] = []

        def fake_urlopen(request, timeout):
            del timeout
            url = request.full_url
            calls.append(url)
            file_name = url.rsplit("/", 1)[1]
            if "primary.example" in url:
                raise HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)
            return FakeResponse(f"backup:{file_name}".encode("utf-8"))

        with TemporaryDirectory() as temp_dir:
            with patch("arknights_schedule_generator.data.urlopen", fake_urlopen):
                written = download_data(Path(temp_dir), force=True, sources=sources)
            file_contents = {
                path.name: path.read_text(encoding="utf-8")
                for path in written
            }

        self.assertEqual([path.name for path in written], list(REQUIRED_FILES))
        self.assertEqual(calls[0], "https://primary.example/data/building_data.json")
        self.assertEqual(calls[1], "https://backup.example/data/building_data.json")
        self.assertFalse(any("primary.example" in url for url in calls[2:]))
        self.assertEqual(
            file_contents,
            {file_name: f"backup:{file_name}" for file_name in REQUIRED_FILES},
        )

    def test_download_data_reports_source_errors(self) -> None:
        sources = (
            DataSource("primary", "https://primary.example/data"),
            DataSource("backup", "https://backup.example/data"),
        )

        def fake_urlopen(request, timeout):
            del timeout
            url = request.full_url
            raise HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)

        with TemporaryDirectory() as temp_dir:
            with patch("arknights_schedule_generator.data.urlopen", fake_urlopen):
                with self.assertRaisesRegex(
                    DataDownloadError,
                    "building_data.json.*HTTP 429",
                ):
                    download_data(Path(temp_dir), force=True, sources=sources)


if __name__ == "__main__":
    unittest.main()
