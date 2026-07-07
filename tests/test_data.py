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
            return FakeResponse(download_payload(file_name, "backup"))

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
            {file_name: expected_download_text(file_name, "backup") for file_name in REQUIRED_FILES},
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

    def test_download_data_keeps_existing_cache_when_refresh_fails(self) -> None:
        sources = (DataSource("primary", "https://primary.example/data"),)
        calls: list[str] = []

        def fake_urlopen(request, timeout):
            del timeout
            url = request.full_url
            calls.append(url)
            file_name = url.rsplit("/", 1)[1]
            if file_name == "character_table.json":
                raise HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)
            return FakeResponse(download_payload(file_name, "new"))

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_cache(data_dir, "old")
            with patch("arknights_schedule_generator.data.urlopen", fake_urlopen):
                with self.assertRaisesRegex(DataDownloadError, "character_table.json.*HTTP 429"):
                    download_data(data_dir, force=True, sources=sources)
            file_contents = {
                file_name: (data_dir / file_name).read_text(encoding="utf-8")
                for file_name in REQUIRED_FILES
            }

        self.assertEqual(calls[:2], [
            "https://primary.example/data/building_data.json",
            "https://primary.example/data/character_table.json",
        ])
        self.assertEqual(
            file_contents,
            {file_name: expected_download_text(file_name, "old") for file_name in REQUIRED_FILES},
        )

    def test_download_data_refreshes_complete_set_when_cache_is_partial(self) -> None:
        sources = (DataSource("primary", "https://primary.example/data"),)
        calls: list[str] = []

        def fake_urlopen(request, timeout):
            del timeout
            url = request.full_url
            calls.append(url)
            file_name = url.rsplit("/", 1)[1]
            return FakeResponse(download_payload(file_name, "new"))

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "building_data.json").write_text(
                expected_download_text("building_data.json", "old"),
                encoding="utf-8",
            )
            with patch("arknights_schedule_generator.data.urlopen", fake_urlopen):
                written = download_data(data_dir, force=False, sources=sources)
            file_contents = {
                file_name: (data_dir / file_name).read_text(encoding="utf-8")
                for file_name in REQUIRED_FILES
            }

        self.assertEqual([path.name for path in written], list(REQUIRED_FILES))
        self.assertEqual(len(calls), len(REQUIRED_FILES))
        self.assertEqual(
            file_contents,
            {file_name: expected_download_text(file_name, "new") for file_name in REQUIRED_FILES},
        )

    def test_download_data_rejects_invalid_json_without_overwriting_cache(self) -> None:
        sources = (DataSource("primary", "https://primary.example/data"),)

        def fake_urlopen(request, timeout):
            del timeout
            file_name = request.full_url.rsplit("/", 1)[1]
            if file_name == "building_data.json":
                return FakeResponse(b"not json")
            return FakeResponse(download_payload(file_name, "new"))

        with TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            write_cache(data_dir, "old")
            with patch("arknights_schedule_generator.data.urlopen", fake_urlopen):
                with self.assertRaisesRegex(DataDownloadError, "building_data.json.*valid JSON"):
                    download_data(data_dir, force=True, sources=sources)

            self.assertEqual(
                (data_dir / "building_data.json").read_text(encoding="utf-8"),
                expected_download_text("building_data.json", "old"),
            )


def download_payload(file_name: str, marker: str) -> bytes:
    return expected_download_text(file_name, marker).encode("utf-8")


def expected_download_text(file_name: str, marker: str) -> str:
    if file_name.endswith(".json"):
        return f'{{"{file_name}": "{marker}"}}'
    return f"{marker}:data-version\n"


def write_cache(data_dir: Path, marker: str) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    for file_name in REQUIRED_FILES:
        (data_dir / file_name).write_text(expected_download_text(file_name, marker), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
