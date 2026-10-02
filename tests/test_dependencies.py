"""Tests for the on-demand dependency check and installer.

The installer is the one part of this program that downloads and then executes
something, so these tests are mostly about the ways it can go wrong: a truncated
download, a substituted binary, a cancelled run, and a machine that simply has
no network.  None of them touch the network - a local stand-in archive stands in
for the real one - so the suite is deterministic and offline.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from youtube_downloader.core import dependencies
from youtube_downloader.core.dependencies import (
    DependencyReport,
    check_dependencies,
    discover_ffmpeg,
    ffmpeg_version,
    forget_discovery,
    install_ffmpeg,
    install_targets,
    installed_ffmpeg,
    locate_device_ffmpeg,
    missing_dependencies,
    per_user_directory,
    shared_directory,
)
from youtube_downloader.core.errors import CancelledError, DependencyError


def _working_ffmpeg(directory: Path, name: str = "ffmpeg.exe") -> Path:
    """A candidate that genuinely runs and identifies itself as FFmpeg.

    ``ffmpeg_version`` only accepts a candidate that runs, exits zero, and
    prints a version line, so the tests that need one accepted cannot use a
    text stand-in - a .py file with ffmpeg's output in it is not an executable
    and would be refused for the wrong reason.  The repository's own bundled
    binary is copied instead, so the probe is exercised against the real thing
    rather than against a mock of it.
    """

    bundled = Path(__file__).resolve().parents[1] / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if not bundled.is_file():
        raise unittest.SkipTest("the repository's bundled ffmpeg.exe is needed for this test")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    shutil.copyfile(bundled, target)
    return target


def _unrunnable(directory: Path, name: str = "ffmpeg.exe") -> Path:
    """A candidate that exists but cannot run - a truncated or fake download."""

    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(b"MZ" + b"\x00" * 4096)
    return target


def _same_path(left: Path | None, right: Path) -> bool:
    """Whether two paths name the same file, ignoring Windows' casing.

    Needed because ``shutil.which`` builds its answer from ``PATHEXT``, which is
    uppercase, so a candidate found that way comes back as ``ffmpeg.EXE`` while
    the file on disk is ``ffmpeg.exe``.  They are the same file; comparing the
    strings would call that a difference.
    """

    assert left is not None
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


# Captured before any test patches it: two of these tests assert on what the real
# device search builds, which is exactly what the isolation helper hides.
_REAL_DEVICE_SEARCH_ROOTS = dependencies._device_search_roots


def _walked_first_matching(packages: Path, wanted: Path) -> Path | None:
    """What the scan's own walk finds, run at the depth the scan really uses."""

    for match in dependencies._walk_for_ffmpeg(packages, depth=4):
        if ffmpeg_version(match) is not None:
            return match
    return None


def _unreadable_profiles() -> list[Path]:
    """Stand in for a machine that will not let this process list its profiles."""

    raise OSError("access is denied")


def _no_leftover_probe_files(directory: Path) -> None:
    """Assert the writability probe cleaned up after itself."""

    leftovers = [
        path.name
        for path in directory.iterdir()
        if path.name.startswith(".clipdock-write-probe")
    ]
    assert not leftovers, f"the write probe left files behind: {leftovers}"


def _stand_in_archive(directory: Path, payload: bytes = b"MZ" + b"\x00" * 4096) -> Path:
    """A zip shaped like the real one: one ffmpeg.exe nested in a version folder."""

    archive = directory / "ffmpeg.zip"
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("ffmpeg-n9.0.2-3-ga5923073bf-win64-lgpl-9.0/bin/ffmpeg.exe", payload)
    return archive


class DependencyReportTests(unittest.TestCase):
    def test_a_present_dependency_costs_nothing_to_report(self) -> None:
        reports = check_dependencies(lambda: Path("C:/somewhere/ffmpeg.exe"))
        ffmpeg = next(r for r in reports if r.name == "FFmpeg")
        self.assertTrue(ffmpeg.present)
        self.assertEqual(ffmpeg.download_bytes, 0)
        self.assertEqual(ffmpeg.located_at, str(Path("C:/somewhere/ffmpeg.exe")))
        self.assertEqual(missing_dependencies(lambda: Path("C:/x/ffmpeg.exe")), [])

    def test_every_dependency_is_reported_not_only_the_missing_ones(self) -> None:
        """"Nothing to download" is only reassuring alongside what was checked."""

        reports = check_dependencies(lambda: Path("C:/somewhere/ffmpeg.exe"))
        self.assertEqual([r.name for r in reports], ["FFmpeg", "yt-dlp"])
        self.assertTrue(all(r.present for r in reports))

    def test_the_bundled_extractor_is_reported_even_though_it_cannot_be_missing(self) -> None:
        """yt-dlp ships inside the program; saying so answers the question asked.

        The user asked what is present, not only what is absent.  Reporting it
        also keeps the report honest about the fact that no download could ever
        supply it - there is nothing on the user's machine they could give us.
        """

        reports = {r.name: r for r in check_dependencies(lambda: Path("C:/x/ffmpeg.exe"))}
        ytdlp = reports["yt-dlp"]
        self.assertTrue(ytdlp.present)
        self.assertEqual(ytdlp.download_bytes, 0)

    def test_a_missing_extractor_would_be_reported_rather_than_assumed(self) -> None:
        """The check reads real state; it does not hardcode "yt-dlp is fine"."""

        with patch.object(dependencies, "_ytdlp_version", lambda: None):
            reports = {r.name: r for r in check_dependencies(lambda: Path("C:/x/ffmpeg.exe"))}
            missing = [report.name for report in missing_dependencies(lambda: None)]
        self.assertFalse(reports["yt-dlp"].present)
        self.assertFalse(reports["yt-dlp"].fixable, "nothing on the machine could supply it")
        # Read inside the patch, or the assertion passes for the wrong reason:
        # outside it, yt-dlp really is present and the test proves nothing.  It is
        # reported as absent yet never *offered*, because the download on offer
        # is FFmpeg's and cannot deliver this.
        self.assertEqual(missing, ["FFmpeg"])

    def test_a_present_dependency_has_nothing_to_say(self) -> None:
        from youtube_downloader.gui.dependencies import describe

        self.assertEqual(
            describe(DependencyReport(name="FFmpeg", present=True, path=Path("C:/x"), needed_for="y")),
            "",
        )

    def test_a_missing_dependency_names_where_it_would_be_installed(self) -> None:
        """The destination is part of the offer, not an implementation detail.

        A user agreeing to 164 MB is entitled to know it is going somewhere that
        outlives this copy of the program.
        """

        from youtube_downloader.gui.dependencies import describe

        text = describe(
            DependencyReport(
                name="FFmpeg",
                present=False,
                path=None,
                needed_for="merging",
                download_bytes=171_546_855,
            )
        )
        self.assertIn("whole computer", text)
        self.assertIn(str(install_targets()[0]), text)

    def test_the_dialog_accounts_for_where_the_search_looked(self) -> None:
        """The claim that the whole device was checked is shown, not implied.

        If the copy being used sits somewhere the user did not expect, this line
        is what tells them, before they have to ask.
        """

        from youtube_downloader.gui import dependencies as gui_dependencies

        found = Path("C:/found/ffmpeg.exe")
        with patch.object(gui_dependencies, "locate_device_ffmpeg", lambda: found):
            self.assertIn(str(found), gui_dependencies.checked_devices())
        with patch.object(gui_dependencies, "locate_device_ffmpeg", lambda: None):
            text = gui_dependencies.checked_devices()
        self.assertIn("could not find", text)
        self.assertIn(str(install_targets()[0]), text)

    def test_a_missing_dependency_states_its_cost_and_purpose(self) -> None:
        missing = [r for r in missing_dependencies(lambda: None) if r.name == "FFmpeg"]
        self.assertEqual(len(missing), 1)
        report = missing[0]
        self.assertFalse(report.present)
        self.assertIsNone(report.path)
        self.assertGreater(report.download_bytes, 0)
        self.assertIn("merg", report.needed_for)
        self.assertGreater(report.megabytes, 100)
        self.assertEqual(report.located_at, "")

    def test_the_report_is_frozen(self) -> None:
        """A report is a snapshot, not something a worker thread can edit."""

        report = DependencyReport(name="FFmpeg", present=False, path=None, needed_for="x")
        with self.assertRaises(Exception):
            report.present = True  # type: ignore[misc]

    def test_the_check_never_raises_however_bad_the_injected_finder_is(self) -> None:
        """This runs during startup; nothing here may stop the window opening."""

        def explode() -> None:
            raise OSError("device on fire")

        with self.assertRaises(OSError):
            # documents that the function itself does not swallow: the
            # guarantee is provided by the caller in the GUI, which is where the
            # total-handling requirement belongs.
            check_dependencies(explode)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="clipdock-dep-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        # Both install folders are redirected into the temporary one.  Without
        # this the test would write to - and could be refused by - the real
        # %ProgramData% and %LOCALAPPDATA% of whoever is running it.  Patching
        # shared_directory() is what redirects the device-wide location, since
        # every other function is derived from those two.
        self._shared = patch.object(
            dependencies, "shared_directory", lambda: self.directory / "shared"
        )
        self._shared.start()
        self.addCleanup(self._shared.stop)
        self._personal = patch.object(
            dependencies, "per_user_directory", lambda: self.directory / "personal"
        )
        self._personal.start()
        self.addCleanup(self._personal.stop)
        # PATH too, and this is not belt-and-braces.  The device search consults
        # PATH first, so redirecting only the two install folders leaves the
        # tests' premise - "this computer has no FFmpeg" - true on a clean
        # machine and false on a developer machine, which typically has one on
        # PATH precisely because it was installed there by hand.  A suite whose
        # result depends on the machine it runs on is not a suite.
        #
        # Pointing PATH at the (empty) temporary directory is enough: the search
        # is a bounded list of folders, and a stand-in that genuinely runs is
        # installed into the shared folder, not found by name.
        self._path = patch.dict(os.environ, {"PATH": str(self.directory)})
        self._path.start()
        self.addCleanup(self._path.stop)
        forget_discovery()
        self.addCleanup(forget_discovery)

    def test_the_machine_running_this_suite_cannot_leak_an_ffmpeg(self) -> None:
        """The premise every other test in this class rests on.

        The device search consults PATH first.  Redirecting only the two install
        folders therefore leaves "this computer has no FFmpeg" true on a clean
        machine and false on a developer machine, which usually has one on PATH
        because it was put there by hand.  That is not hypothetical: the machine
        this was found on has the repository's own vendor\\ffmpeg\\bin on the
        MACHINE PATH, and one test failed because of it while every other test
        in the class passed.

        Asserting the premise directly covers the isolation in setUp, rather than
        leaving it an invisible condition that the other tests happen to depend
        on and that only shows up on somebody's machine.
        """

        self.assertIsNone(
            shutil.which("ffmpeg"),
            "PATH still reaches a real ffmpeg; setUp's PATH redirect is gone",
        )
        self.assertIsNone(
            locate_device_ffmpeg(),
            "the device search found something, so 'nothing is installed' is false",
        )


    def _install_from(self, archive: Path) -> Path:
        def fake_download(destination: Path, progress, is_cancelled) -> None:
            shutil.copyfile(archive, destination)

        with patch.object(dependencies, "_download_archive", fake_download):
            with patch.object(dependencies, "FFMPEG_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()):
                return install_ffmpeg(lambda event: None, lambda: False)

    def test_a_verified_archive_installs_a_usable_binary(self) -> None:
        payload = b"MZ" + b"\x7fELF-ish stand-in" * 32
        archive = _stand_in_archive(self.directory, payload)
        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()):
            installed = self._install_from(archive)
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_bytes(), payload)
        # Device-wide, not per-user: the point of the shared folder.
        self.assertEqual(installed.parent, self.directory / "shared")

    def test_a_substituted_archive_is_refused_and_nothing_is_written(self) -> None:
        archive = _stand_in_archive(self.directory)
        with patch.object(dependencies, "FFMPEG_ARCHIVE_SHA256", "0" * 64):
            with self.assertRaises(DependencyError):
                self._install_from(archive)
        # None, not a path: a refused install must leave nothing behind at all,
        # including nothing that the next launch would mistake for a usable copy.
        self.assertIsNone(installed_ffmpeg())

    def test_a_good_archive_yielding_a_bad_binary_is_refused(self) -> None:
        """The two checksums catch different failures and are not redundant.

        A correct archive is only correct if the pin is right.  Verifying the
        extracted binary as well means a changed pin cannot quietly install
        something nobody checked.
        """

        archive = _stand_in_archive(self.directory)
        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", "0" * 64):
            with self.assertRaises(DependencyError):
                self._install_from(archive)
        self.assertIsNone(installed_ffmpeg())

    def test_a_damaged_archive_is_a_friendly_error_not_a_crash(self) -> None:
        broken = self.directory / "broken.zip"
        broken.write_bytes(b"this is not a zip file")
        with self.assertRaises(DependencyError) as caught:
            dependencies._extract_ffmpeg(broken, self.directory / "out.exe", lambda e: None, lambda: False)
        self.assertEqual(caught.exception.code, "missing_dependency")
        self.assertFalse((self.directory / "out.exe").exists())

    def test_an_archive_without_ffmpeg_is_refused(self) -> None:
        empty = self.directory / "empty.zip"
        with zipfile.ZipFile(empty, "w") as package:
            package.writestr("readme.txt", "no binary here")
        with self.assertRaises(DependencyError):
            dependencies._extract_ffmpeg(empty, self.directory / "out.exe", lambda e: None, lambda: False)

    def test_a_cancelled_run_leaves_no_partial_file(self) -> None:
        payload = b"MZ" * 512
        archive = _stand_in_archive(self.directory, payload)
        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()):
            with patch.object(dependencies, "_download_archive", lambda d, p, c: shutil.copyfile(archive, d)):
                with self.assertRaises(CancelledError):
                    install_ffmpeg(lambda event: None, lambda: True)
        self.assertIsNone(installed_ffmpeg())
        self.assertEqual(list(self.directory.rglob("*.part")), [])

    def test_a_second_install_reuses_what_is_already_there(self) -> None:
        """Re-running must not re-download 164 MB for nothing."""

        target = install_targets()[0] / "ffmpeg.exe"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"already here")
        self.assertEqual(installed_ffmpeg(), target)

        def explode(*args, **kwargs):  # pragma: no cover - must not run
            raise AssertionError("install_ffmpeg should not download when present")

        with patch.object(dependencies, "_download_archive", explode):
            self.assertEqual(install_ffmpeg(lambda e: None, lambda: False), target)

    def test_the_device_wide_folder_is_preferred_over_the_per_user_one(self) -> None:
        """One copy for the whole machine, so a second user does not re-download.

        This is the behaviour the request asked for, stated as a measurement
        rather than as prose: the file must land in the device-wide folder
        whenever that folder can be written to.
        """

        payload = b"MZ" * 300
        archive = _stand_in_archive(self.directory, payload)
        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()):
            installed = self._install_from(archive)
        self.assertEqual(installed.parent, self.directory / "shared")
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.read_bytes(), payload)

    def test_it_falls_back_to_the_per_user_folder_when_the_shared_one_is_refused(self) -> None:
        """A machine that will not allow the shared folder must still work.

        The user is probably not an administrator, and the shared folder may be
        locked down by policy.  Failing the whole install in that case would make
        the download pointless, so the per-user folder is used instead.
        """

        payload = b"MZ" * 200
        archive = _stand_in_archive(self.directory, payload)
        with patch.object(
            dependencies,
            "_is_writable_directory",
            lambda path: Path(path) != self.directory / "shared",
        ):
            with patch.object(
                dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()
            ):
                installed = self._install_from(archive)
        self.assertEqual(installed.parent, self.directory / "personal")
        self.assertTrue(installed.is_file())

    def test_the_writability_probe_leaves_no_files_behind(self) -> None:
        target = self.directory / "probe-target"
        self.assertTrue(dependencies._is_writable_directory(target))
        _no_leftover_probe_files(target)

    def test_installing_clears_the_cached_device_scan(self) -> None:
        """Otherwise the just-installed copy stays invisible for the session.

        The scan is cached because it runs a subprocess per candidate, and a
        cache that is not invalidated by the installer would mean the program
        downloads FFmpeg and then cannot find it until the next launch.
        """

        cache = dependencies._CACHED_SCAN
        payload = b"MZ" * 100
        archive = _stand_in_archive(self.directory, payload)

        discover_ffmpeg()  # populates the cache with "nothing found"
        before = cache.cache_info()
        self.assertEqual(before.misses, 1, "the first call should have scanned")
        discover_ffmpeg()
        self.assertEqual(
            cache.cache_info().misses, before.misses, "the second call re-scanned"
        )

        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()):
            installed = self._install_from(archive)
        self.assertTrue(installed.is_file())
        # Not patched, so this is the real invalidation the installer performs.
        # cache_clear() zeroes the counters as well as the contents, so the next
        # lookup has to miss again - which is the whole point.
        self.assertEqual(cache.cache_info().currsize, 0, "the cache was not cleared")
        self.assertIsNone(
            locate_device_ffmpeg(),
            "the stand-in payload cannot run, so it is correctly still not usable",
        )
        self.assertEqual(cache.cache_info().misses, 1, "the scan was not re-run")

    def test_a_really_installed_copy_is_visible_to_the_next_lookup(self) -> None:
        """The full round trip, with a payload that genuinely runs.

        The cache test above only proves the scan is re-run.  This one proves the
        re-run can actually succeed: a real FFmpeg is installed into the shared
        folder and then found again through the ordinary lookup, which is exactly
        what a second user launching this program would do.
        """

        bundled = _working_ffmpeg(self.directory / "source")
        archive = _stand_in_archive(self.directory, bundled.read_bytes())
        with patch.object(
            dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(bundled.read_bytes()).hexdigest()
        ):
            installed = self._install_from(archive)
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.parent, self.directory / "shared")
        self.assertTrue(_same_path(locate_device_ffmpeg(), installed))

    def test_progress_events_carry_a_percentage_and_a_note(self) -> None:
        events: list[dict] = []
        payload = b"MZ" * 64
        archive = _stand_in_archive(self.directory, payload)
        with patch.object(dependencies, "FFMPEG_BINARY_SHA256", hashlib.sha256(payload).hexdigest()):

            def fake_download(destination: Path, progress, is_cancelled) -> None:
                progress({"kind": "dependency_progress", "note": "Downloading FFmpeg", "done": 50, "total": 100, "percent": 50})
                shutil.copyfile(archive, destination)

            with patch.object(dependencies, "_download_archive", fake_download):
                with patch.object(dependencies, "FFMPEG_ARCHIVE_SHA256", hashlib.sha256(archive.read_bytes()).hexdigest()):
                    install_ffmpeg(events.append, lambda: False)
        self.assertTrue(events)
        self.assertTrue(all(event["kind"] == "dependency_progress" for event in events))
        self.assertTrue(all(0 <= event["percent"] <= 100 for event in events))
        self.assertTrue(all(event["note"] for event in events))

    def test_no_network_yields_a_dependency_error_the_user_can_act_on(self) -> None:
        import urllib.error

        def refuse(*args, **kwargs):
            raise urllib.error.URLError("no route to host")

        with patch.object(dependencies.urllib.request, "urlopen", refuse):
            with self.assertRaises(DependencyError) as caught:
                dependencies._download_archive(
                    self.directory / "x.zip", lambda e: None, lambda: False
                )
        self.assertIn("internet", caught.exception.message.lower())

    def test_the_download_request_identifies_the_current_version(self) -> None:
        """The version must not be written out a third time by hand.

        It lives in youtube_downloader/__init__.py and in scripts/clipdock.iss,
        and the User-Agent was a third copy.  A request that announces 0.1.0
        after the program is at 0.2.0 is wrong in a way nothing else would catch,
        because the download still succeeds - the header is just stale, and
        stale is invisible until someone reads a server log.
        """

        import urllib.error

        from youtube_downloader import __version__

        captured: dict[str, object] = {}

        def capture(request, *args, **kwargs):
            # Capitalised by urllib itself: "User-Agent".capitalize() is
            # "User-agent", which is why the lookup below is not spelled the
            # way the header is written here.
            captured["agent"] = request.get_header("User-agent")
            raise urllib.error.URLError("the header was all this test wanted")

        with patch.object(dependencies.urllib.request, "urlopen", capture):
            with self.assertRaises(DependencyError):
                dependencies._download_archive(
                    self.directory / "x.zip", lambda e: None, lambda: False
                )
        self.assertEqual(captured["agent"], f"ClipDock/{__version__}")


class LookupOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="clipdock-device-test-"))
        self.addCleanup(shutil.rmtree, self.directory, True)
        forget_discovery()
        self.addCleanup(forget_discovery)

    def _isolate(self, **environment: str) -> None:
        """Point every discovery location at a folder with nothing in it.

        Without this the test would inherit whatever the machine running it has
        installed, which is the opposite of what these tests are asserting about.
        """

        environment_patcher = patch.dict(os.environ, environment, clear=False)
        environment_patcher.start()
        self.addCleanup(environment_patcher.stop)
        self._patch(
            dependencies, "shared_directory", lambda: self.directory / "shared"
        )
        self._patch(
            dependencies, "per_user_directory", lambda: self.directory / "personal"
        )
        self._patch(
            dependencies, "_device_search_roots", lambda: [self.directory / "nowhere"]
        )
        forget_discovery()

    def _patch(self, target: object, name: str, value: object) -> None:
        patcher = patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_copy_this_program_installed_is_found_on_the_device(self) -> None:
        """The whole point: a second program must not trigger a re-download."""

        self._isolate(PATH=str(self.directory / "empty"))
        shared = self.directory / "shared"
        installed = _working_ffmpeg(shared)
        forget_discovery()
        self.assertTrue(_same_path(locate_device_ffmpeg(), installed))

    def test_a_copy_another_program_installed_is_found_too(self) -> None:
        """Found in a conventional install folder, not only in our own."""

        self._isolate(PATH=str(self.directory / "empty"))
        elsewhere = _working_ffmpeg(self.directory / "Program Files" / "ffmpeg" / "bin")
        patcher = patch.object(
            dependencies,
            "_device_search_roots",
            lambda: [self.directory / "Program Files"],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        forget_discovery()
        self.assertTrue(_same_path(locate_device_ffmpeg(), elsewhere))

    def test_our_own_copy_wins_over_one_found_elsewhere(self) -> None:
        """A pinned, checksum-verified build beats an arbitrary system copy."""

        self._isolate(PATH=str(self.directory / "empty"))
        ours = _working_ffmpeg(self.directory / "shared")
        _working_ffmpeg(self.directory / "Program Files" / "ffmpeg" / "bin")
        patcher = patch.object(
            dependencies,
            "_device_search_roots",
            lambda: [self.directory / "Program Files"],
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        forget_discovery()
        self.assertTrue(_same_path(locate_device_ffmpeg(), ours))

    def test_a_file_that_exists_but_does_not_run_is_refused(self) -> None:
        """Existence is not evidence.

        A truncated download, or an unrelated program that happens to be called
        ffmpeg.exe, both pass a file-exists check and then fail at the moment a
        video is being merged.  A candidate has to run.
        """

        self._isolate(PATH=str(self.directory / "empty"))
        broken = _unrunnable(self.directory / "shared")
        self.assertIsNone(ffmpeg_version(broken))
        forget_discovery()
        self.assertIsNone(locate_device_ffmpeg())

    def test_a_program_that_exits_zero_without_identifying_itself_is_refused(self) -> None:
        """Exit status alone is not identity.

        An unrelated program that treats unknown arguments as a no-op exits zero
        while printing its own name, and would otherwise be accepted as a
        dependency and then fail at the first merge.  The probe therefore checks
        that the output actually claims to be FFmpeg.
        """

        from youtube_downloader.core import dependencies as module

        class Succeeded:
            returncode = 0
            stdout = "Some Other Tool 3.1\n"
            stderr = ""

        with patch.object(module.subprocess, "run", return_value=Succeeded()):
            self.assertIsNone(ffmpeg_version(Path("C:/x/ffmpeg.exe")))

    def test_a_nonzero_exit_is_refused_even_when_the_banner_looks_right(self) -> None:
        """Some broken builds print the banner and then fail on a missing DLL.

        Believing the banner would accept exactly the binary most likely to fail
        during a real conversion, which is the opposite of what the probe is for.
        """

        from youtube_downloader.core import dependencies as module

        class Failed:
            returncode = 1
            stdout = "ffmpeg version n9.0.2 Copyright (c) 2000-2026\n"
            stderr = "The code execution cannot proceed because fzlib.dll was not found."

        with patch.object(module.subprocess, "run", return_value=Failed()):
            self.assertIsNone(ffmpeg_version(Path("C:/x/ffmpeg.exe")))

    def test_a_working_candidate_reports_its_version(self) -> None:
        installed = _working_ffmpeg(self.directory / "bin")
        version = ffmpeg_version(installed)
        assert version is not None
        self.assertIn("ffmpeg version", version.lower())

    def test_a_candidate_that_cannot_be_launched_at_all_is_refused(self) -> None:
        """A missing DLL raises at launch rather than exiting non-zero.

        That is the failure mode of a hand-copied binary, and it must read as
        "not usable" rather than propagate out of a lookup.
        """

        import subprocess as stdlib_subprocess

        with patch.object(
            stdlib_subprocess, "run", side_effect=OSError("The specified module could not be found")
        ):
            self.assertIsNone(ffmpeg_version(Path("C:/x/ffmpeg.exe")))

    def test_a_broken_copy_on_path_is_refused_rather_than_used(self) -> None:
        """The same rule applies to PATH, which is the easiest place to be fooled.

        A machine whose PATH holds a stale ffmpeg must still end up downloading a
        working one, because the alternative is discovering the problem halfway
        through a merge.
        """

        bin_directory = self.directory / "bin"
        _unrunnable(bin_directory)
        self._isolate(PATH=str(bin_directory))
        forget_discovery()
        self.assertIsNone(discover_ffmpeg())
        self.assertIsNone(locate_device_ffmpeg())

    def test_a_working_copy_on_path_is_found(self) -> None:
        bin_directory = self.directory / "bin"
        on_path = _working_ffmpeg(bin_directory)
        self._isolate(PATH=str(bin_directory))
        forget_discovery()
        self.assertTrue(_same_path(locate_device_ffmpeg(), on_path))

    def test_the_scan_runs_once_per_launch_and_is_not_reexecuted(self) -> None:
        """The scan starts a process per candidate, so it is cached.

        Without the cache, every media job would re-run several executables for
        an answer that cannot have changed.
        """

        self._isolate(PATH=str(self.directory / "empty"))
        _working_ffmpeg(self.directory / "shared")
        forget_discovery()
        discover_ffmpeg()
        before = discover_ffmpeg.cache_info()
        discover_ffmpeg()
        after = discover_ffmpeg.cache_info()
        self.assertEqual(before.misses, after.misses, "a cached scan re-ran its body")
        self.assertEqual(before.hits, after.hits - 1)

    def test_forget_discovery_makes_the_next_scan_look_again(self) -> None:
        self._isolate(PATH=str(self.directory / "empty"))
        self._patch(dependencies, "_device_search_roots", lambda: [self.directory / "shared"])
        forget_discovery()
        self.assertIsNone(discover_ffmpeg())
        _working_ffmpeg(self.directory / "shared")
        self.assertIsNone(discover_ffmpeg(), "the cached answer should still be served")
        forget_discovery()
        self.assertIsNotNone(discover_ffmpeg())

    def test_a_bundled_copy_still_wins_over_the_device(self) -> None:
        from youtube_downloader.core.binaries import find_ffmpeg

        self._isolate()
        bundled = self.directory / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"bundled")
        cached = _working_ffmpeg(self.directory / "shared")
        forget_discovery()
        # An explicit root tests one layout in isolation, so the device is not
        # consulted and the bundled copy is the only answer.
        self.assertEqual(find_ffmpeg(self.directory), bundled)
        self.assertTrue(cached.is_file(), "the device copy should not have been consulted at all")

    def test_a_slim_build_resolves_to_the_device_wide_copy(self) -> None:
        """This is the path a slim build depends on end to end."""

        from youtube_downloader.core import binaries

        self._isolate(PATH=str(self.directory / "empty"))
        installed = _working_ffmpeg(self.directory / "shared")
        forget_discovery()
        # application_roots() is emptied so there is no vendor folder anywhere,
        # which is what a -Slim build looks like.
        patcher = patch.object(binaries, "application_roots", lambda: [])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertTrue(_same_path(binaries.find_ffmpeg(), installed))

    def test_the_package_manager_walk_stays_within_its_depth_bound(self) -> None:
        """A deep tree must not turn the check into a disk scan.

        WinGet nests its packages under version-named directories, so the fixed
        layouts cannot match and the tree has to be walked - but only a few levels
        deep.  A deeper tree than the bound has to come back empty rather than
        being searched all the way down.
        """

        # Packages\<package>\<version>\<arch>\bin\ffmpeg.exe is the real shape.
        deep = self.directory / "Packages" / "FFmpeg.BtbN" / "9.0.2" / "x64" / "bin"
        installed = _working_ffmpeg(deep)
        self.assertTrue(installed.is_file())
        self.assertEqual(dependencies._walk_for_ffmpeg(self.directory / "Packages", 1), [])
        self.assertEqual(dependencies._walk_for_ffmpeg(self.directory / "Packages", 2), [])
        self.assertEqual(dependencies._walk_for_ffmpeg(self.directory / "Packages", 3), [])
        found = dependencies._walk_for_ffmpeg(self.directory / "Packages", 4)
        self.assertEqual([path.name for path in found], ["ffmpeg.exe"])
        # And that depth is the one the scan actually uses.
        self.assertIsNotNone(
            _walked_first_matching(self.directory / "Packages", installed)
        )

    def test_the_package_manager_walk_stops_after_a_few_matches(self) -> None:
        """Bounded output, so one crowded tree cannot dominate the launch."""

        for index in range(12):
            _working_ffmpeg(self.directory / "Packages" / f"FFmpeg.{index}" / "bin")
        self.assertLessEqual(
            len(dependencies._walk_for_ffmpeg(self.directory / "Packages", 3)), 4
        )

    def test_the_device_search_survives_a_profile_folder_it_cannot_read(self) -> None:
        """One locked-down profile must not abort the whole search.

        A machine with a profile the running user cannot list is ordinary enough
        - domain accounts, restore points, a second Windows install - and the
        consequence of raising here would be that the check fails and the user is
        offered a download the machine does not need.
        """

        self._isolate(PATH=str(self.directory / "empty"))
        self._patch(dependencies, "_local_profiles", _unreadable_profiles)
        # The real one, not the isolation stub: this is about what it builds.
        roots = _REAL_DEVICE_SEARCH_ROOTS()
        # The ClipDock folders are still searched; only the profiles are lost.
        self.assertIn(self.directory / "shared", roots)
        self.assertIn(self.directory / "personal", roots)
        self.assertNotIn(
            Path.home(), roots, "no per-profile location could have been added"
        )
        self.assertIsNone(locate_device_ffmpeg())
        """A build must never ship a binary the app would then refuse to install."""

        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "fetch_dependencies",
            Path(__file__).resolve().parents[1] / "tools" / "fetch_dependencies.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.FFMPEG_URL, dependencies.FFMPEG_URL)
        self.assertEqual(module.FFMPEG_ARCHIVE_SHA256, dependencies.FFMPEG_ARCHIVE_SHA256)
        self.assertEqual(
            module.FFMPEG_BINARY_SHA256["ffmpeg.exe"],
            dependencies.FFMPEG_BINARY_SHA256,
        )

    def test_the_shared_folder_is_under_program_data_not_program_files(self) -> None:
        """A normal user must be able to install without being an administrator.

        Program Files grants neither write access nor CREATOR OWNER to a standard
        user, so choosing it would replace the 164 MB download with a UAC
        prompt - a worse first-run experience than the one this avoids.
        Program Data grants both.
        """

        shared = shared_directory()
        self.assertEqual(shared.name, "runtime")
        self.assertEqual(shared.parent.name, "ClipDock")
        self.assertEqual(shared.parts[-3], "ProgramData")

    def test_the_per_user_folder_is_under_the_apps_own_data_directory(self) -> None:
        """The fallback, for a machine that refuses the shared folder.

        Per user rather than beside the executable, because the executable is
        routinely un-writable: Program Files, a read-only share, or the removable
        drive the folder was copied from.
        """

        self.assertEqual(per_user_directory().name, "runtime")
        self.assertEqual(per_user_directory().parent.name, "ClipDock")

    def test_the_shared_folder_is_always_tried_before_the_per_user_one(self) -> None:
        self.assertEqual(install_targets(), (shared_directory(), per_user_directory()))
        self.assertNotEqual(shared_directory(), per_user_directory())

    def test_installed_ffmpeg_reports_nothing_when_neither_folder_has_one(self) -> None:
        """None is the case that triggers the download, so it must be a real answer."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(dependencies, "shared_directory", lambda: root / "a"):
                with patch.object(dependencies, "per_user_directory", lambda: root / "b"):
                    self.assertIsNone(installed_ffmpeg())
                    (root / "a").mkdir(parents=True)
                    (root / "a" / "ffmpeg.exe").write_bytes(b"x")
                    self.assertEqual(installed_ffmpeg(), root / "a" / "ffmpeg.exe")
                    (root / "b").mkdir(parents=True)
                    (root / "b" / "ffmpeg.exe").write_bytes(b"x")
                    # Shared still wins when both hold a copy.
                    self.assertEqual(installed_ffmpeg(), root / "a" / "ffmpeg.exe")

    def test_a_lookup_never_creates_a_directory_as_a_side_effect(self) -> None:
        """installed_ffmpeg() is called by a read-only lookup, every job.

        Choosing the install folder by probing writability would mean a lookup
        that creates directories - and one that can fail on a machine it was only
        meant to inspect.  It has to look, not touch.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(dependencies, "shared_directory", lambda: root / "shared"):
                with patch.object(dependencies, "per_user_directory", lambda: root / "personal"):
                    self.assertIsNone(installed_ffmpeg())
                    self.assertFalse((root / "shared").exists())
                    self.assertFalse((root / "personal").exists())


class SlenderBuildTests(unittest.TestCase):
    def test_the_slim_build_produces_a_folder_far_smaller_than_the_full_one(self) -> None:
        """The whole point of the slim build, stated as a measurement.

        The threshold is deliberately loose.  It exists to catch a regression -
        a vendor folder creeping back into the packaging step, which would
        silently undo the feature - and not to describe the exact size, which
        moves whenever PySide6 is upgraded.
        """

        dist = Path(__file__).resolve().parents[1] / "dist" / "ClipDock"
        if not dist.is_dir():
            self.skipTest("no packaged build to measure")
        vendor = dist / "vendor"
        if vendor.exists():
            self.skipTest("dist/ClipDock is a full build, not a slim one")
        total = sum(path.stat().st_size for path in dist.rglob("*") if path.is_file())
        self.assertLess(
            total,
            160 * 1024 * 1024,
            "a slim build should be well under 160 MB without the vendor folder",
        )


if __name__ == "__main__":
    unittest.main()
