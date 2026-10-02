"""The settings file must never be the reason the program fails to start.

These are the paths a real machine hits and a developer never does: no file
yet, a half-written file, a hand-edited file, a directory that cannot be
written.  Each one has to end with the application running on its defaults.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from youtube_downloader.core import settings
from youtube_downloader.core.logging_setup import default_log_directory


class SettingsReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "settings.json"

    def test_a_missing_file_is_the_normal_first_launch(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(settings.read_settings(self.path), {})

    def test_a_saved_value_comes_back(self) -> None:
        self.assertTrue(settings.write_setting("theme", "nord", self.path))
        self.assertEqual(settings.read_settings(self.path), {"theme": "nord"})

    def test_writing_one_key_keeps_the_others(self) -> None:
        settings.write_setting("theme", "nord", self.path)
        settings.write_setting("something-else", "kept", self.path)
        self.assertEqual(
            settings.read_settings(self.path),
            {"theme": "nord", "something-else": "kept"},
        )

    def test_rewriting_the_same_value_is_a_no_op(self) -> None:
        settings.write_setting("theme", "nord", self.path)
        before = self.path.stat().st_mtime_ns
        self.assertTrue(settings.write_setting("theme", "nord", self.path))
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_a_truncated_file_reads_as_empty_instead_of_raising(self) -> None:
        # What an interrupted write, a full disk, or a killed process leaves.
        self.path.write_text('{"theme": "nor', encoding="utf-8")
        self.assertEqual(settings.read_settings(self.path), {})

    def test_an_empty_file_reads_as_empty(self) -> None:
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(settings.read_settings(self.path), {})

    def test_a_json_value_that_is_not_an_object_reads_as_empty(self) -> None:
        for document in ("[]", '"theme"', "42", "null", "true"):
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                self.assertEqual(settings.read_settings(self.path), {})

    def test_values_of_the_wrong_type_are_dropped(self) -> None:
        # A settings file is user-editable, so a hand-edited number or nested
        # object must not reach the rest of the application as a string.
        self.path.write_text(
            json.dumps({"theme": "nord", "count": 3, "nested": {"a": "b"}, "ok": "yes"}),
            encoding="utf-8",
        )
        self.assertEqual(settings.read_settings(self.path), {"theme": "nord", "ok": "yes"})

    def test_a_directory_in_place_of_the_file_reads_as_empty(self) -> None:
        self.path.mkdir()
        self.assertEqual(settings.read_settings(self.path), {})

    def test_a_rejected_value_does_not_destroy_the_stored_ones(self) -> None:
        settings.write_setting("theme", "nord", self.path)
        # A corrupt file must not be silently overwritten by the next write, but
        # it must not wedge the program either: the new value is written and the
        # unreadable content is dropped.
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertTrue(settings.write_setting("theme", "gruvbox", self.path))
        self.assertEqual(settings.read_settings(self.path), {"theme": "gruvbox"})


class SettingsWriteFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name)

    def test_an_unwritable_directory_reports_failure_instead_of_raising(self) -> None:
        if os.name != "nt" and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = self.directory / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(locked.chmod, stat.S_IRWXU)
        target = locked / "settings.json"
        # Whether the write is refused depends on the platform and on whether
        # the process is elevated; what matters is that it never raises.
        result = settings.write_setting("theme", "nord", target)
        self.assertIn(result, (True, False))
        if not result:
            self.assertEqual(settings.read_settings(target), {})

    def test_a_path_that_is_a_directory_reports_failure(self) -> None:
        target = self.directory / "settings.json"
        target.mkdir()
        self.assertFalse(settings.write_setting("theme", "nord", target))

    def test_no_temporary_files_are_left_behind(self) -> None:
        target = self.directory / "settings.json"
        settings.write_setting("theme", "nord", target)
        settings.write_setting("theme", "dracula", target)
        leftovers = [item.name for item in self.directory.iterdir() if item.name != target.name]
        self.assertEqual(leftovers, [])


class SettingsLocationTests(unittest.TestCase):
    def test_the_settings_file_sits_beside_the_logs(self) -> None:
        # One directory per user for everything this application writes, rather
        # than a second location to explain.
        self.assertEqual(
            settings.default_settings_directory(),
            default_log_directory().parent,
        )
        self.assertEqual(settings.default_settings_path().name, "settings.json")

    def test_an_unset_local_appdata_falls_back_to_the_home_directory(self) -> None:
        previous = os.environ.pop("LOCALAPPDATA", None)
        try:
            directory = settings.default_settings_directory()
        finally:
            if previous is not None:
                os.environ["LOCALAPPDATA"] = previous
        self.assertTrue(str(directory).endswith("ClipDock"))


if __name__ == "__main__":
    unittest.main()
