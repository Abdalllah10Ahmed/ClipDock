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

import ast
import re
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "scripts" / "clipdock.iss"
BUILD = ROOT / "scripts" / "build_installer.ps1"
INFO = ROOT / "scripts" / "installer" / "info-before.txt"
EXE_BUILD = ROOT / "scripts" / "build_exe.ps1"
VERSION_FILE_TOOL = ROOT / "tools" / "make_version_file.py"


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

    def test_the_installers_first_page_names_the_current_version(self) -> None:
        """The installer's opening page states the version in prose.

        This is a **fourth** copy of the version, and until this test existed it
        was the only unguarded one: `__init__.py` and `clipdock.iss` have been
        compared against each other, and the FFmpeg request's User-Agent is
        interpolated and checked, but nothing looked at the sentence a person
        reads *before deciding to install*.  A release built from a bumped
        `__init__.py` would have shown `ClipDock 0.1.0` as the first thing on
        screen while installing 0.2.0's code - and unlike a stale User-Agent,
        which is invisible until someone reads a server log, this one is the most
        visible surface the release has.

        The check is that the version appears at all, rather than that it equals
        `__version__`: the page opens with the product name, so the sentence has
        to name the version in that position to be read as a version at all, and
        requiring the full "ClipDock <version>" prefix keeps it from passing on
        some unrelated number that happens to appear further down.
        """

        from youtube_downloader import __version__

        body = INFO.read_text(encoding="utf-8")
        opening = body.strip().splitlines()[0]
        self.assertTrue(
            opening.startswith(f"ClipDock {__version__}"),
            f"the installer's first line is {opening!r}, which does not open with "
            f"'ClipDock {__version__}'",
        )

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

    # -- start with Windows ----------------------------------------------------

    def _autostart_tasks(self) -> list[str]:
        tasks = [line for line in _section_body(self.text, "Tasks") if "autostart" in line]
        self.assertEqual(len(tasks), 1, f"expected one autostart task, got {tasks}")
        return tasks

    def _autostart_writes(self) -> list[str]:
        # `uninsdeletevalue` contains `deletevalue`, so the two entries cannot be
        # told apart by a bare substring - they are told apart by which flag they
        # actually carry.
        writes = [
            line
            for line in _section_body(self.text, "Registry")
            if "CurrentVersion\\Run" in line and "uninsdeletevalue" in line
        ]
        self.assertEqual(len(writes), 1, f"expected one Run-value write, got {writes}")
        return writes

    def test_the_autostart_option_is_offered_and_ticked_by_default(self) -> None:
        """Checked by default is decided by an *absent* flag, so what matters is
        that neither of the two flags which would change it appears.

        `unchecked` would ship it off, against the decision. `checkedonce` is
        the trap: it un-ticks the box whenever Inno finds a previous version, so
        using it here would mean every upgrade silently switched the option off
        for the people who had left it on - the opposite of the default that was
        asked for, and invisible until someone noticed ClipDock had stopped
        starting. Inno's UsePreviousTasks is what remembers a choice the user
        actually made, and it is on by default, so nothing is needed for that.
        """

        task = self._autostart_tasks()[0]
        lowered = task.lower()
        self.assertNotIn("unchecked", lowered, "the autostart option ships unticked")
        self.assertNotIn("checkedonce", lowered, "checkedonce un-ticks on every upgrade")
        # It has to be reachable in the same page as the desktop icon, or a user
        # who does not want it is left hunting through Windows startup settings.
        self.assertIn("GroupDescription", task)

    def test_the_autostart_label_says_a_window_will_appear_on_its_own(self) -> None:
        """The label is where the consent actually happens.

        "Start with Windows" is true and uninformative: a ticked box here is
        consent to launch a GUI application at every sign-in, and what the
        person needs to know is that a frameless ClipDock window will then
        appear unprompted. Asserting the phrases keeps this from being
        shortened back to something accurate and useless.
        """

        description = re.search(r'Description:\s*"([^"]*)"', self._autostart_tasks()[0])
        self.assertIsNotNone(description, "the autostart task has no description")
        text = description.group(1)
        self.assertIn("Windows starts", text)
        self.assertIn("opens by itself", text)
        self.assertIn("no prompt", text)

    def test_the_autostart_value_is_quoted_per_user_and_points_at_the_executable(self) -> None:
        """Three properties that each fail differently if dropped.

        HKCU because PrivilegesRequired=lowest never elevates, so an HKLM write
        would either fail or demand the administrator rights this installer
        promises not to ask for. Quoted because {localappdata} contains a space
        and Windows parses an unquoted Run value as the first token plus the
        rest - the classic hijack, and a reason rather than a style. The
        executable through the defines, because a literal name would let the
        product be renamed everywhere except this line.
        """

        line = self._autostart_writes()[0]
        self.assertIn("Root: HKCU", line)
        self.assertNotIn("Root: HKLM", line)
        self.assertIn("Subkey: \"Software\\Microsoft\\Windows\\CurrentVersion\\Run\"", line)
        self.assertIn('ValueData: """{app}\\{#ProductExeName}"""', line)
        for literal in ("ClipDock.exe", "\\ClipDock\\"):
            self.assertNotIn(literal, line, f"the Run value hardcodes {literal!r}")
        self.assertIn("Tasks: autostart", line, "the value is written whether or not it was asked for")

    def test_uninstall_removes_the_autostart_entry(self) -> None:
        """The half that is easy to forget, and the reason it is the common case.

        The task is checked by default, so most machines will hold this value;
        without `uninsdeletevalue` an uninstall leaves a sign-in entry pointing
        at a file that has been deleted, once every sign-in, with nothing left
        installed that could remove it. [UninstallDelete] deliberately stays
        empty (pinned by its own test) - this is a registry value, not a file,
        so it does not belong there.
        """

        write = self._autostart_writes()[0]
        self.assertIn("uninsdeletevalue", write)
        self.assertIn("[UninstallDelete]", self.text)
        self.assertEqual(_section_body(self.text, "UninstallDelete"), [])

        # The promise is also stated to the person deciding whether to install,
        # not only to the uninstaller.
        info = INFO.read_text(encoding="utf-8").lower()
        self.assertIn("start clipdock when windows starts", info)

    def test_unticking_autostart_on_an_upgrade_retracts_the_entry(self) -> None:
        """Inno creates what is selected and never retracts what an earlier run
        created, so without this the box would be a lie on the second install.

        A user who unticks the option during an upgrade would still have
        ClipDock starting at sign-in, because the value written by the previous
        version is untouched by an upgrade that did not select the task. The
        `not` operator on a Tasks parameter is Inno's own, so this says exactly
        "when it was not asked for, make sure it is not there".
        """

        retractions = [
            line
            for line in _section_body(self.text, "Registry")
            if "CurrentVersion\\Run" in line
            and "deletevalue" in line
            and "uninsdeletevalue" not in line
        ]
        self.assertEqual(len(retractions), 1, f"expected one retraction, got {retractions}")
        line = retractions[0]
        self.assertIn("ValueType: none", line)
        self.assertIn("Tasks: not autostart", line)
        self.assertNotIn("uninsdeletevalue", line, "there is nothing left to delete at uninstall")
        # It must name the same value the write does, or it retracts nothing.
        self.assertIn("ValueName: \"{#ProductName}\"", line)
        self.assertIn("ValueName: \"{#ProductName}\"", self._autostart_writes()[0])


class VersionResourceTests(unittest.TestCase):
    """The frozen executable's Windows version resource.

    `dist\\ClipDock\\ClipDock.exe` reported an empty FileVersion, ProductName and
    FileDescription, so Task Manager, file Properties and Windows' installed-
    programs list all showed nothing for it.  `ClipDock-Setup.exe` identifies
    itself correctly, because Inno Setup writes a resource of its own - so the
    installer named a version while the program it installed named none.  That
    was PROJECT_MAP.md:266.

    The resource is generated rather than committed, which is the point of these
    tests.  A committed version file would be a fifth copy of the version, and
    this project has already shipped two stale ones - a stale FFmpeg User-Agent
    and an installer first page that kept naming the previous release.  So the
    checks below are about the value *flowing* from `youtube_downloader.__version__`
    rather than about the contents of a checked-in file.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not EXE_BUILD.is_file():
            raise unittest.SkipTest("scripts/build_exe.ps1 is not present")
        if not VERSION_FILE_TOOL.is_file():
            raise unittest.SkipTest("tools/make_version_file.py is not present")
        cls.script = EXE_BUILD.read_text(encoding="utf-8")

    # --- the build is wired to it at all ------------------------------------

    def test_the_executable_build_passes_a_version_resource(self) -> None:
        """Without `--version-file` PyInstaller writes no resource at all."""

        self.assertIn("--version-file", self.script)
        self.assertIn("make_version_file.py", self.script)

    def test_the_resource_is_generated_before_pyinstaller_runs(self) -> None:
        """PyInstaller reads the file when it starts.  A resource generated
        afterwards would be a correct file and an empty executable.

        The anchor is the `$arguments = @(` block rather than the word
        "PyInstaller", which occurs at the top of the file in a comment saying
        PyInstaller is build-time only - a first attempt used the bare word and
        compared against that comment.
        """

        generated = self.script.find("make_version_file.py")
        assembled = self.script.find("$arguments = @(")
        invoked = self.script.find("& $venvPython @arguments")
        self.assertNotEqual(generated, -1, "the version file is never generated")
        self.assertNotEqual(assembled, -1, "the PyInstaller arguments are not assembled")
        self.assertNotEqual(invoked, -1, "PyInstaller is never invoked")
        self.assertLess(
            generated,
            assembled,
            "$versionFile is assigned by the generation call, so the generation "
            "must come before the argument list that uses it",
        )
        self.assertLess(assembled, invoked, "the arguments are built after the build")

    def test_the_product_name_is_passed_in_rather_than_repeated(self) -> None:
        """`$appName` in build_exe.ps1 is the one place the product name lives.

        The tool defaults its own `--product` to "ClipDock", which is a second
        copy, so the build has to override it - otherwise renaming the product
        renames the executable and leaves the resource claiming otherwise.
        """

        self.assertIn("$appName = ", self.script)
        self.assertIn("--product $appName", self.script)

    def test_the_resource_is_generated_into_the_ignored_build_folder(self) -> None:
        """It must not land somewhere git tracks.

        `build/` is ignored, which is what keeps the generated copy from becoming
        a committed version - the drift this whole approach exists to prevent.
        The path is asserted rather than the file's existence, because the file
        only exists during a build and a test that waited for it would be a test
        that could not fail in a clean checkout.
        """

        self.assertIn('Join-Path $specDir "version_info.txt"', self.script)
        self.assertIn('$specDir = Join-Path $root "build"', self.script)

        ignore = ROOT / ".gitignore"
        self.assertTrue(ignore.is_file(), ".gitignore is missing")
        patterns = ignore.read_text(encoding="utf-8").splitlines()
        self.assertTrue(
            any(line.strip().rstrip("/") == "build" for line in patterns),
            "build/ is not ignored, so a generated version file could be committed",
        )

    # --- the version itself --------------------------------------------------

    def test_the_resource_names_the_application_version(self) -> None:
        """The two strings Windows displays are the application's own version."""

        from youtube_downloader import __version__
        from tools.make_version_file import render

        rendered = render("ClipDock", __version__, "desc")
        self.assertIn(f"StringStruct(u'FileVersion', u'{__version__}')", rendered)
        self.assertIn(f"StringStruct(u'ProductVersion', u'{__version__}')", rendered)

    def test_the_binary_version_is_the_application_version_padded(self) -> None:
        """Windows' numeric version is four integers, not three.

        `filevers` is what Task Manager's Details tab and most log parsers read,
        so a three-part `__version__` has to be padded rather than rejected.
        """

        from youtube_downloader import __version__
        from tools.make_version_file import _quadruple, render

        numbers = [int(p) for p in __version__.split(".")]
        numbers += [0] * (4 - len(numbers))
        expected = tuple(numbers[:4])
        self.assertEqual(_quadruple(__version__), expected)
        shown = ", ".join(str(n) for n in expected)
        self.assertIn(f"filevers=({shown})", render("ClipDock", __version__, "desc"))
        self.assertIn(f"prodvers=({shown})", render("ClipDock", __version__, "desc"))

    def test_a_different_version_produces_a_different_resource(self) -> None:
        """The anti-hardcoding guard.

        If the tool contained a version literal, `render` would ignore its
        argument and this would fail.  Asserting on the *output* rather than on
        the source is what makes it meaningful: a source scan for "0.2.0" would
        also have to forbid the word appearing in a comment, which is not the
        property being tested.
        """

        from youtube_downloader import __version__
        from tools.make_version_file import render

        real = render("ClipDock", __version__, "desc")
        invented = render("ClipDock", "9.9.9", "desc")
        self.assertNotEqual(real, invented)

        changed = [
            (before, after)
            for before, after in zip(real.splitlines(), invented.splitlines())
            if before != after
        ]
        self.assertTrue(changed, "the two resources are identical line for line")
        for before, after in changed:
            for line in (before, after):
                self.assertTrue(
                    "filevers=" in line
                    or "prodvers=" in line
                    or "FileVersion" in line
                    or "ProductVersion" in line,
                    f"changing the version altered a line that is not a version: "
                    f"{line!r}",
                )

    def test_a_two_part_version_is_padded_rather_than_rejected(self) -> None:
        """Worth guarding because it is the shape a hurried bump takes."""

        from tools.make_version_file import _quadruple, render

        self.assertEqual(_quadruple("1.2"), (1, 2, 0, 0))
        self.assertIn("filevers=(1, 2, 0, 0)", render("P", "1.2", "d"))

    def test_the_written_file_carries_no_byte_order_mark(self) -> None:
        """PyInstaller reads the file as UTF-8 and evaluates it, so a BOM is a
        syntax error rather than something it tolerates.

        `Out-File -Encoding utf8` on Windows PowerShell 5.1 emits one, which makes
        this a realistic failure rather than a theoretical one.
        """

        from tools.make_version_file import main

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version_info.txt"
            self.assertEqual(main(["--output", str(target), "--product", "P"]), 0)
            raw = target.read_bytes()
            self.assertFalse(
                raw.startswith(b"\xef\xbb\xbf"),
                "the generated file starts with a UTF-8 BOM",
            )
            ast.parse(raw.decode("utf-8"))

    def test_the_check_mode_notices_a_stale_file(self) -> None:
        """`--check` is what lets the suite verify a committed copy.

        It has to fail on a *differing* file and not merely on a missing one, or
        it would pass on anything it can read.
        """

        from tools.make_version_file import main

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version_info.txt"
            self.assertEqual(main(["--output", str(target), "--product", "P"]), 0)
            self.assertEqual(
                main(["--check", "--output", str(target), "--product", "P"]), 0
            )

            target.write_text("VSVersionInfo(ffi=FixedFileInfo())\n", encoding="utf-8")
            self.assertEqual(
                main(["--check", "--output", str(target), "--product", "P"]), 1,
                "--check accepted a file that does not match the application version",
            )
            self.assertEqual(
                main(["--check", "--output", str(Path(directory) / "absent.txt")]), 1,
                "--check accepted a file that is not there",
            )

    # --- the check that would have caught the first draft --------------------

    def test_pyinstaller_itself_can_read_the_generated_resource(self) -> None:
        """Load it with PyInstaller's own parser, not a substitute.

        The first draft of the generated file was written from memory and used
        the long-form FixedFileInfo keys found in hand-written examples -
        `VOS_NT_WINDOWS32`, `VFT_APP` and so on.  PyInstaller's FixedFileInfo
        takes `OS`, `fileType`, `subtype` and `date`, so the build would have
        failed with "unexpected keyword argument", at build time, on a machine
        whose only mistake was trusting a file that looked right.

        Every check above is satisfied by a file that PyInstaller rejects, because
        the file's syntax is Python but its top-level expression is a *call* -
        which ast.literal_eval refuses by design.  So this test goes through the
        loader the build itself uses.  It is skipped, not failed, when PyInstaller
        is absent, because it is a build-time-only dependency and the rest of the
        suite must not require it.
        """

        try:
            from PyInstaller.utils.win32.versioninfo import (
                load_version_info_from_text_file,
            )
        except ImportError:
            self.skipTest("PyInstaller is a build-time-only dependency and is absent")

        from youtube_downloader import __version__
        from tools.make_version_file import _quadruple, main, render

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "version_info.txt"
            self.assertEqual(main(["--output", str(target), "--product", "ClipDock"]), 0)
            try:
                info = load_version_info_from_text_file(str(target))
            except Exception as error:  # noqa: BLE001 - the message is the assertion
                self.fail(
                    f"PyInstaller rejected the generated resource: {error}\n"
                    f"{render('ClipDock', __version__, 'desc')}"
                )

        ffi = info.ffi
        self.assertEqual(ffi.sig, 0xFEEF04BD)
        self.assertEqual(ffi.fileType, 0x1, "an application is VFT_APP")
        # Read the binary version the way Windows does - MS holds the major pair
        # and LS the minor pair - rather than comparing text, because the whole
        # point is that these are numbers now.
        shown = (
            ffi.fileVersionMS >> 16,
            ffi.fileVersionMS & 0xFFFF,
            ffi.fileVersionLS >> 16,
            ffi.fileVersionLS & 0xFFFF,
        )
        self.assertEqual(
            shown,
            _quadruple(__version__),
            "the binary version does not match the application version",
        )

        # The strings are what a person reads; the numbers above are what a
        # machine reads.  Both are the same version and both are checked.
        strings = {
            struct.name: struct.val
            for kid in info.kids
            if type(kid).__name__ == "StringFileInfo"
            for table in kid.kids
            for struct in table.kids
        }
        self.assertEqual(strings["FileVersion"], __version__)
        self.assertEqual(strings["ProductVersion"], __version__)
        self.assertEqual(strings["ProductName"], "ClipDock")
        self.assertEqual(strings["OriginalFilename"], "ClipDock.exe")

    def test_a_missing_resource_is_an_exception_and_not_an_empty_result(self) -> None:
        """Why the release pass must catch rather than test for emptiness.

        PyInstaller's reader of a *built* executable raises when the image
        carries no RT_VERSION resource, rather than returning something falsy:

            pywintypes.error (1813, 'EnumResourceNamesW',
            'The specified resource type cannot be found in the image file')

        Measured against the shipped 0.2.0 files, where the application
        executable raised this and the installer returned 0.2.0.0.  The natural
        guard is therefore `if not info:`, and on this reader that branch is
        unreachable - the exception fires first.  A check written that way would
        report success on a broken executable by never entering its own failure
        branch, which is the lesson this project has now learned six times.

        What is pinned is the reader's *shape*, not whether this particular build
        carries a resource: `dist\\ClipDock\\ClipDock.exe` predates the wiring, so
        asserting it has one would fail until the next rebuild, and a test whose
        result depends on when it was last run is not a test.  The falsifiable
        claim is that the call never yields a falsy value - it raises, or it
        returns a populated resource, and there is no third outcome for a caller
        to guard against incorrectly.
        """

        try:
            from PyInstaller.utils.win32.versioninfo import (
                read_version_info_from_executable,
            )
        except ImportError:
            self.skipTest("PyInstaller is a build-time-only dependency and is absent")

        frozen = ROOT / "dist" / "ClipDock" / "ClipDock.exe"
        if not frozen.is_file():
            self.skipTest("no frozen build in dist\\; run scripts\\build_exe.ps1 first")

        info = None
        raised: Exception | None = None
        try:
            info = read_version_info_from_executable(str(frozen))
        except Exception as error:  # noqa: BLE001 - the exception IS the assertion
            raised = error

        if raised is not None:
            self.assertIn(
                "1813",
                str(raised),
                "expected ERROR_RESOURCE_TYPE_NOT_FOUND for an image with no "
                f"RT_VERSION, got {type(raised).__name__}: {raised}",
            )
        else:
            # The other permitted outcome.  Asserting it is populated is what
            # stops "returned something" from being mistaken for "returned a
            # usable version".
            self.assertTrue(
                info,
                "read_version_info_from_executable returned a falsy value; the "
                "release pass's `if not info` guard would then be unreachable "
                "for the opposite reason",
            )
            self.assertNotEqual(
                info.ffi.fileVersionMS,
                0,
                "a resource was returned but its binary version is zero",
            )


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
