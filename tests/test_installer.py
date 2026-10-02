"""Tests for the Inno Setup installer script and the script that drives it.

The installer is a build artefact nobody imports, so nothing else in the suite
would notice it rotting.  Each test here pins one decision that was made on
purpose, so that a later edit which quietly reverses it fails here instead of
being discovered by the person the installer was sent to.

What is deliberately NOT tested: that the script compiles, that the wizard
looks right, or that the installed program runs.  Those need Inno Setup
installed and a real Windows session, and they are covered by actually building
and installing it rather than by asserting on text.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "scripts" / "clipdock.iss"
BUILD = ROOT / "scripts" / "build_installer.ps1"
INFO = ROOT / "scripts" / "installer" / "info-before.txt"


def _meaningful_lines(text: str) -> list[str]:
    """Drop blank lines and whole-line comments.

    Only a leading ';' is treated as a comment.  Inno also allows ';' mid-line,
    but stripping those would corrupt any value that legitimately contains one,
    and none of the directives asserted on below does.
    """

    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]


def _directives(text: str) -> dict[str, list[str]]:
    """Map every directive to its values, keyed lowercase, preserving order."""

    found: dict[str, list[str]] = {}
    for line in _meaningful_lines(text):
        if line.startswith("[") and line.endswith("]"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        found.setdefault(key.strip().lower(), []).append(value.strip())
    return found


def _code_lines(text: str) -> list[str]:
    """Non-comment lines of a PowerShell script.

    Separate from _meaningful_lines because the .ps1 files here quote the
    anti-patterns they warn about inside their own comments, so a negative
    assertion about the code has to be made against the code and not against
    the prose describing what not to write.
    """

    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _defines(text: str) -> dict[str, str]:
    return {
        match.group(1): match.group(2)
        for match in re.finditer(r'^\s*#define\s+(\w+)\s+"([^"]*)"', text, re.MULTILINE)
    }


def _section_body(text: str, name: str) -> list[str]:
    """The non-comment lines of one section, excluding the section headers.

    The comparison is case-insensitive on both sides.  Getting that wrong makes
    this return an empty list for every section, which turns "this section is
    deliberately empty" into a test that passes for the wrong reason - so the
    section lookup is itself covered by test_a_section_with_content_is_found.
    """

    body: list[str] = []
    inside = False
    wanted = f"[{name.lower()}]"
    for line in _meaningful_lines(text):
        if line.startswith("[") and line.endswith("]"):
            inside = line.lower() == wanted
            continue
        if inside:
            body.append(line)
    return body


class InstallerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not ISS.is_file():
            raise unittest.SkipTest("scripts/clipdock.iss is not present")
        cls.text = ISS.read_text(encoding="utf-8")
        cls.directives = _directives(cls.text)
        cls.defines = _defines(cls.text)

    def _value(self, key: str) -> str:
        values = self.directives.get(key.lower(), [])
        self.assertEqual(len(values), 1, f"{key} should be set exactly once, got {values}")
        return values[0]

    # -- identity -------------------------------------------------------------

    def test_a_section_with_content_is_found(self) -> None:
        """The section parser itself, before anything relies on it.

        A case-sensitive comparison here returns an empty list for every
        section, which makes every "this section is deliberately empty" test
        below pass for the wrong reason.  Asserting that a section known to
        have content is found keeps that from being invisible.
        """

        self.assertTrue(_section_body(self.text, "Files"))
        self.assertTrue(_section_body(self.text, "Icons"))
        self.assertTrue(_section_body(self.text, "Run"))
        self.assertTrue(_section_body(self.text, "Tasks"))
        # Case must not matter: these are looked up by their name in the tests.
        self.assertEqual(
            _section_body(self.text, "files"), _section_body(self.text, "Files")
        )

    def test_the_script_and_its_inputs_all_exist(self) -> None:
        for path in (BUILD, INFO, ROOT / "assets" / "app.ico"):
            self.assertTrue(path.is_file(), f"{path} is referenced but missing")

    def test_the_installer_version_matches_the_application_version(self) -> None:
        """A version bump in code that misses the installer is invisible until
        someone installs the new program and the Apps and Features list calls it
        the old one."""

        from youtube_downloader import __version__

        self.assertEqual(self.defines["ProductVersion"], __version__)

    def test_the_product_name_is_defined_once_and_used_everywhere(self) -> None:
        """One name, referenced through the preprocessor.

        A literal 'ClipDock' pasted into a second place is how a product ends up
        called two things in its own installer; every use below must go through
        the define instead.
        """

        self.assertEqual(self.defines["ProductName"], "ClipDock")
        for key in ("AppName", "AppVerName", "OutputBaseFilename", "DefaultGroupName"):
            self.assertIn("{#ProductName}", self._value(key), f"{key} hardcodes the name")
        # The publisher is a separate define on purpose: it is the one value most
        # likely to be changed to a real name, and it appears in Apps and
        # Features and on the installer's file properties.
        self.assertIn("ProductPublisher", self.defines)
        self.assertEqual(self._value("AppPublisher"), "{#ProductPublisher}")
        for section in ("Files", "Icons", "Run"):
            for line in _section_body(self.text, section):
                if "{#ProductName}" in line:
                    continue
                self.assertNotIn(
                    "ClipDock", line, f"[{section}] hardcodes the product name: {line}"
                )

    def test_the_app_id_is_a_fixed_guid_not_a_generated_one(self) -> None:
        """Inno keys 'already installed, and where' off this value.

        A fresh GUID per build would install each release beside the last one
        instead of upgrading it, and the old copy would stay behind forever.
        """

        app_id = self.defines["ProductAppId"]
        self.assertRegex(app_id, r"^\{\{[0-9A-F-]{36}\}$")
        # [Setup] carries the reference; the define carries the value.  Asserting
        # the reference is what stops a literal GUID being pasted into [Setup]
        # and then diverging from the define on the next release.
        self.assertEqual(self._value("AppId"), "{#ProductAppId}")

    # -- the payload ----------------------------------------------------------

    def test_it_packages_the_current_build_folder(self) -> None:
        """The whole reason build_installer.ps1 exists.

        dist\\ held 279 MB under the previous product's name alongside the
        current one.  A Source: line still pointing at the old folder, or at
        vendor\\, would ship the wrong program or fail to compile, so the path
        is asserted rather than trusted.
        """

        sources = [
            line for line in _section_body(self.text, "Files") if line.lower().startswith("source")
        ]
        self.assertEqual(len(sources), 1, f"expected one Source line, got {sources}")
        self.assertIn(r"..\dist\{#ProductName}\*", sources[0])
        self.assertNotIn("vendor", sources[0].lower())
        for retired in ("YouTubeDownloader", "YouTubeDownloader-source"):
            self.assertNotIn(retired, self.text, f"{retired} is a superseded folder name")

    def test_the_file_copy_recurses_and_ignores_versions(self) -> None:
        """Without recursesubdirs the build is an executable that cannot start,
        and without ignoreversion Qt's and Python's own file versions make every
        upgrade prompt before it does anything."""

        source = _section_body(self.text, "Files")[0].lower()
        self.assertIn("recursesubdirs", source)
        self.assertIn("createallsubdirs", source)
        self.assertIn("ignoreversion", source)

    # -- no elevation ---------------------------------------------------------

    def test_installing_never_asks_for_administrator_rights(self) -> None:
        """The promise the whole per-user design exists to keep.

        'lowest' is what makes a right-click Run as administrator resolve to the
        same per-user folder rather than a machine-wide copy, so it is asserted
        rather than assumed; 'admin' here would put a UAC prompt in front of
        every person the program is sent to.
        """

        self.assertEqual(self._value("PrivilegesRequired"), "lowest")

    def test_the_default_destination_is_per_user_and_editable(self) -> None:
        target = self._value("DefaultDirName")
        self.assertIn("{localappdata}", target)
        self.assertNotIn("{autopf}", target)
        self.assertNotIn("{commonpf}", target)
        # A wizard that only offers the default would not be a choice.
        self.assertNotIn("DisableDirPage", self.directives)

    def test_the_architecture_is_pinned_to_what_was_frozen(self) -> None:
        """_internal\\ holds x64 Python and Qt, so a 32-bit machine gets a clear
        refusal here instead of a program that dies at startup with no
        explanation."""

        self.assertEqual(self._value("ArchitecturesAllowed"), "x64compatible")

    # -- the first page --------------------------------------------------------

    def test_the_first_page_exists_and_explains_the_unsigned_warning(self) -> None:
        """SmartScreen can fire before the wizard opens, so nothing inside it can
        prevent the warning - only explain it in advance.  This asserts the text
        is wired up and says the two things that matter: that the warning is
        expected, and how to get past it."""

        referenced = self._value("InfoBeforeFile")
        path = (ISS.parent / referenced).resolve()
        self.assertTrue(path.is_file(), f"InfoBeforeFile points at a missing file: {path}")
        body = path.read_text(encoding="utf-8")
        lowered = body.lower()
        for phrase in ("more info", "run anyway", "unknown publisher"):
            self.assertIn(phrase, lowered, f"the first page never mentions {phrase!r}")
        # InfoBeforeFile belongs to [Setup], which is parsed before anything that
        # installs files, so a missing path there fails the compile rather than
        # the install.  Asserted only so the directive is not renamed away.
        self.assertIn("[Setup]", self.text)

    def test_the_first_page_tells_the_truth_about_the_first_run_check(self) -> None:
        """The check runs before the window appears and can download ~164 MB.
        The person deciding whether to install is the one who has to be told,
        and that it asks first is the whole reason the program is not read as
        something that spends bandwidth unasked."""

        body = (ISS.parent / self._value("InfoBeforeFile")).read_text(encoding="utf-8").lower()
        # The page has to say the program asks, and that saying no is a normal
        # answer.  A version that only says "ClipDock will download what it
        # needs" describes a behaviour the program does not have.
        self.assertIn("ask whether to download", body)
        self.assertIn("declining is a normal answer", body)
        self.assertIn("before its window appears", body)
        # The worst outcome to describe wrongly is a silent download.
        self.assertNotIn("automatically download", body)
        self.assertNotIn("without asking", body)

    # -- uninstall ------------------------------------------------------------

    def test_uninstall_touches_nothing_outside_the_install_directory(self) -> None:
        """[UninstallDelete] is present and empty, and that is the point.

        A slim build may have installed FFmpeg into %ProgramData%, where other
        programs can be using it, and the user's settings and download folders
        are in %LOCALAPPDATA%.  Both live outside {app}, so neither is removed -
        and the section exists, empty and commented, so that this is a recorded
        decision rather than an accident of nobody writing the section.
        """

        self.assertIn("[UninstallDelete]", self.text)
        self.assertEqual(
            _section_body(self.text, "UninstallDelete"),
            [],
            "uninstall must not delete anything outside {app}",
        )

    def test_the_uninstaller_is_reachable_from_the_start_menu(self) -> None:
        """An uninstaller that only exists in the install directory is an
        uninstaller most people never find."""

        targets = [line for line in _section_body(self.text, "Icons") if "{uninstallexe}" in line]
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].lower().startswith('name: "{group}\\'))

    # -- sensible defaults -----------------------------------------------------

    def test_the_desktop_shortcut_is_offered_rather_than_assumed(self) -> None:
        tasks = [line for line in _section_body(self.text, "Tasks") if "desktopicon" in line]
        self.assertEqual(len(tasks), 1)
        self.assertIn("unchecked", tasks[0].lower())
        # A bare checkbox with no label above it is worse than no checkbox.
        self.assertIn("GroupDescription", tasks[0])

    def test_launching_after_install_is_opt_out(self) -> None:
        run = [
            line
            for line in _section_body(self.text, "Run")
            if "{#ProductExeName}" in line
        ]
        self.assertEqual(len(run), 1, f"expected one Run entry, got {run}")
        self.assertIn("{app}", run[0])
        self.assertIn("postinstall", run[0])  # draws a checked box to untick
        self.assertIn("skipifsilent", run[0])  # a scripted install must not pop a window
        self.assertIn("nowait", run[0])

    def test_no_duplicate_directives_anywhere(self) -> None:
        """Inno Setup takes the last value and says nothing, so a duplicated key
        is a silent override.  AppName appearing twice is a typo, not a choice."""

        for key, values in self.directives.items():
            if key in {"source", "name", "filename", "description", "parameters"}:
                continue  # legitimately repeatable in [Icons] and [Run]
            self.assertEqual(len(values), 1, f"{key} is set {len(values)} times: {values}")


class BuildScriptTests(unittest.TestCase):
    """build_installer.ps1 is what stops a stale folder from being packaged."""

    @classmethod
    def setUpClass(cls) -> None:
        if not BUILD.is_file():
            raise unittest.SkipTest("scripts/build_installer.ps1 is not present")
        cls.text = BUILD.read_text(encoding="utf-8")

    def test_it_builds_the_executable_before_it_packages_it(self) -> None:
        """The ordering is the design.

        The .iss packages whatever is in dist\\ClipDock and compiles nothing, so
        running it alone ships whatever was left there by an earlier build.
        """

        build_at = self.text.find("build_exe.ps1")
        package_at = self.text.find("clipdock.iss")
        self.assertNotEqual(build_at, -1, "the executable build is never invoked")
        self.assertNotEqual(package_at, -1, "the installer is never compiled")
        self.assertLess(
            build_at,
            package_at,
            "the executable must be frozen before the installer packages it",
        )

    def test_the_default_is_the_slim_build(self) -> None:
        """The slim build is the artefact meant for someone else's machine: it is
        134 MB instead of 279 MB and the program fetches FFmpeg itself.
        """

        self.assertIn("-Slim", self.text)
        # A splat BY NAME, not by position.  @array binds positionally to a
        # script's param() block, so the first version of this script passed
        # @("-Python", "python", "-Slim"), which bound "-Python" to $Python and
        # dropped -Slim entirely.  The build then produced the full 279 MB
        # variant, the installer came out at 103 MB, and the closing message
        # still described it as slim - no error anywhere.  The literal below is
        # the bug, not the fix, and its return is the regression guard.
        self.assertIn("$exeArgs = @{ Python = $Python }", self.text)
        self.assertIn('$exeArgs["Slim"] = $true', self.text)
        # Against the CODE, not the prose: the comment explaining this bug quotes
        # the broken form verbatim, so searching the whole file would always fail.
        code = "\n".join(_code_lines(self.text))
        self.assertNotIn(
            '$exeArgs += "-Slim"',
            code,
            "an array splat silently drops the -Slim switch",
        )
        self.assertNotIn(
            '@("-Python"',
            code,
            "an array splat binds positionally and misroutes -Python",
        )
        self.assertIn("@exeArgs", code, "the arguments are never passed on")

    def test_it_checks_the_artefact_rather_than_trusting_the_flag(self) -> None:
        """A variant claim is only trustworthy if it is read off the folder.

        The splatting bug above produced a self-installer that announced itself as
        slim and was not.  Asserting the flag was passed could not have caught
        that, because the flag was believed to have been passed.  Reading the
        package itself is what closes it, and both directions matter: a -Full run
        that quietly produced a slim installer would ship a program asking to
        download 164 MB the sender believed was already inside it.
        """

        self.assertIn("$vendorDir = Join-Path $appDir \"vendor\"", self.text)
        self.assertIn("$hasVendor = Test-Path $vendorDir", self.text)
        self.assertIn("if ($Full -and -not $hasVendor)", self.text)
        self.assertIn("if (-not $Full -and $hasVendor)", self.text)
        # Both branches have to actually stop the build.
        self.assertGreaterEqual(self.text.count("throw"), 3)

    def test_it_refuses_to_package_a_folder_that_is_not_there(self) -> None:
        """-SkipExe against a deleted dist\\ should say so in a sentence a person
        can act on, rather than surfacing as an ISCC error about no source files.
        """

        self.assertIn("Nothing to package", self.text)

    def test_it_reports_the_sha256_for_separate_transmission(self) -> None:
        """The hash is only useful sent apart from the installer, so it is printed
        to the console and never written next to the file it describes."""

        self.assertIn("Get-FileHash", self.text)
        self.assertIn("SHA256", self.text)
        self.assertNotIn("Out-File", self.text)
        self.assertNotIn("Set-Content", self.text)

    def test_it_looks_for_the_compiler_where_winget_actually_installs_it(self) -> None:
        """winget installs Inno Setup per-user, so asserting on the Program Files
        path alone would fail on a machine where the compiler is present and
        working."""

        self.assertIn("Inno Setup 6", self.text)
        self.assertIn("LOCALAPPDATA", self.text)
        self.assertIn("JRSoftware.InnoSetup", self.text)

    def test_it_does_not_become_a_runtime_dependency(self) -> None:
        """Inno Setup is build-time only, exactly as PyInstaller is.  Adding it to
        requirements.txt would change the approved runtime set for a program
        whose whole selling point is a small dependency footprint."""

        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("inno", requirements)
        self.assertNotIn("pyinstaller", requirements)


if __name__ == "__main__":
    unittest.main()
