from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from youtube_downloader.core import updates
from youtube_downloader.core.errors import CancelledError, DownloadFailure
from youtube_downloader.core.models import ProgressEvent
from youtube_downloader.core.updates import (
    CURRENT,
    INSTALLER_ASSET_NAME,
    LATEST_RELEASE_API,
    MAX_INSTALLER_BYTES,
    NEWER,
    RELEASE_DOWNLOAD_PREFIX,
    RELEASES_URL,
    UNKNOWN,
    DownloadedUpdate,
    UpdateCheck,
    check_for_updates,
    download_update,
    is_newer,
    launch_installer,
    parse_version,
)


class FakeResponse:
    """The part of a urllib response this module actually uses."""

    def __init__(self, body: bytes) -> None:
        self._body = body
        self.reads: list[int] = []

    def read(self, size: int) -> bytes:
        self.reads.append(size)
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def replying(body: bytes):
    def opener(request: object, timeout: float | None = None) -> FakeResponse:
        return FakeResponse(body)

    return opener


def raising(error: Exception):
    def opener(request: object, timeout: float | None = None) -> FakeResponse:
        raise error

    return opener


def release(tag: str, url: str = "https://github.com/x/y/releases/tag/v9") -> bytes:
    return json.dumps({"tag_name": tag, "html_url": url}).encode()


def release_with_asset(
    body: bytes,
    *,
    name: str = INSTALLER_ASSET_NAME,
    url: str | None = None,
    size: int | None = None,
    digest: str | None = None,
) -> bytes:
    asset: dict[str, object] = {
        "name": name,
        "browser_download_url": url or f"{RELEASE_DOWNLOAD_PREFIX}v9.9.9/{INSTALLER_ASSET_NAME}",
        "size": len(body) if size is None else size,
    }
    if digest is not None:
        asset["digest"] = digest
    return json.dumps(
        {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/x/y/releases/tag/v9.9.9",
            "assets": [asset],
        }
    ).encode()


class _Headers:
    def __init__(self, content_length: int | None) -> None:
        self._content_length = content_length

    def get(self, name: str, default: object = None) -> object:
        if name.lower() == "content-length" and self._content_length is not None:
            return str(self._content_length)
        return default


class DownloadResponse:
    """The part of a release-asset response the downloader actually uses."""

    def __init__(
        self,
        body: bytes,
        *,
        url: str = "https://objects.githubusercontent.com/asset",
        content_length: int | None = None,
    ) -> None:
        self._body = body
        self.headers = _Headers(len(body) if content_length is None else content_length)
        self._url = url

    def read(self, size: int) -> bytes:
        chunk, self._body = self._body[:size], self._body[size:]
        return chunk

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "DownloadResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def installer_check(body: bytes, *, size: int | None = None, digest: str = "") -> UpdateCheck:
    return UpdateCheck(
        status=NEWER,
        current="0.1.0",
        latest="v9.9.9",
        installer_url=f"{RELEASE_DOWNLOAD_PREFIX}v9.9.9/{INSTALLER_ASSET_NAME}",
        installer_size=len(body) if size is None else size,
        installer_digest=digest,
    )


class VersionTests(unittest.TestCase):
    def test_a_plain_version_parses_to_numbers(self) -> None:
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(parse_version("  v0.1.0  "), (0, 1, 0))

    def test_anything_that_is_not_a_version_is_refused_rather_than_guessed(self) -> None:
        # A pre-release is neither older nor newer than the release it precedes,
        # so comparing them as numbers would pick one of the two wrong answers.
        for text in ("0.2.0-rc1", "v0.2.0-beta.1", "latest", "", "0.1.0+build", "one.two", "0..1"):
            with self.subTest(tag=text):
                self.assertIsNone(parse_version(text))
                self.assertFalse(is_newer(text, "0.1.0"))

    def test_versions_are_compared_as_numbers_not_as_text(self) -> None:
        # As text "0.10.0" sorts below "0.9.0", which would offer a downgrade
        # to somebody on a build ten.
        self.assertTrue(is_newer("v0.10.0", "0.9.0"))
        self.assertTrue(is_newer("v1.0.0", "0.9.9"))
        self.assertFalse(is_newer("v0.0.9", "0.1.0"))

    def test_a_missing_component_is_the_same_version_not_an_update(self) -> None:
        self.assertFalse(is_newer("v0.2", "0.2.0"))
        self.assertFalse(is_newer("v0.2.0", "0.2"))

    def test_the_same_version_is_not_an_update(self) -> None:
        self.assertFalse(is_newer("v0.1.0", "0.1.0"))
        self.assertFalse(is_newer("v0.1.0", "0.1.0.0"))


class ResultTests(unittest.TestCase):
    def test_a_newer_tag_is_reported_as_available(self) -> None:
        result = check_for_updates("0.1.0", opener=replying(release("v9.9.9")))
        self.assertEqual(result.status, NEWER)
        self.assertTrue(result.available)
        self.assertEqual(result.latest, "v9.9.9")
        self.assertEqual(result.current, "0.1.0")

    def test_the_same_tag_is_reported_as_up_to_date(self) -> None:
        result = check_for_updates("0.1.0", opener=replying(release("v0.1.0")))
        self.assertEqual(result.status, CURRENT)
        self.assertTrue(result.up_to_date)
        self.assertFalse(result.available)

    def test_an_older_tag_is_not_offered_as_an_update(self) -> None:
        # Somebody running a build from the future - a developer, or a tag that
        # was moved - must not be told to go backwards.
        result = check_for_updates("9.9.9", opener=replying(release("v0.1.0")))
        self.assertEqual(result.status, CURRENT)

    def test_a_pre_release_is_reported_rather_than_offered(self) -> None:
        result = check_for_updates("0.1.0", opener=replying(release("v0.2.0-rc1")))
        self.assertEqual(result.status, UNKNOWN)
        self.assertEqual(result.latest, "v0.2.0-rc1")
        self.assertIn("v0.2.0-rc1", result.reason)

    def test_a_reply_that_is_not_the_expected_shape_is_unknown(self) -> None:
        for body in (b"", b"not json", b"[]", b'{"html_url": "x"}', b'{"tag_name": 7}'):
            with self.subTest(body=body[:20]):
                result = check_for_updates("0.1.0", opener=replying(body))
                self.assertEqual(result.status, UNKNOWN)

    def test_an_oversized_reply_is_refused_rather_than_read(self) -> None:
        # The body is untrusted.  A cap means a truncated or hostile response
        # cannot make the program allocate without bound.
        response = FakeResponse(b"x" * (updates.MAX_RESPONSE_BYTES + 1024))
        result = check_for_updates("0.1.0", opener=lambda *a, **k: response)
        self.assertEqual(result.status, UNKNOWN)
        self.assertEqual(response.reads, [updates.MAX_RESPONSE_BYTES + 1])

    def test_a_page_url_that_is_not_https_is_replaced(self) -> None:
        # The URL is handed to the desktop to open.  Whatever the reply says is
        # not trusted to be a web address at all.
        result = check_for_updates("0.1.0", opener=replying(release("v9.9.9", "file:///etc/passwd")))
        self.assertTrue(result.release_url.startswith("https://"))
        self.assertNotIn("file:", result.release_url)

    def test_every_failure_is_explained_and_nothing_is_raised(self) -> None:
        # An offline or rate-limited machine has to produce a sentence, not a
        # traceback on the way out of a dialog.
        cases = {
            403: "rate limit",
            404: "no published release",
            500: "could not be asked",
            401: "could not be asked",
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                error = urllib.error.HTTPError("u", code, "x", None, None)
                result = check_for_updates("0.1.0", opener=raising(error))
                self.assertEqual(result.status, UNKNOWN)
                self.assertIn(expected, result.reason)
        for error in (urllib.error.URLError("offline"), TimeoutError(), OSError("socket")):
            with self.subTest(error=type(error).__name__):
                result = check_for_updates("0.1.0", opener=raising(error))
                self.assertEqual(result.status, UNKNOWN)
                self.assertIn("reach GitHub", result.reason)

    def test_the_request_identifies_itself_and_asks_for_json(self) -> None:
        # GitHub rejects a request with no User-Agent outright, so this is
        # required rather than decorative.
        seen: list[object] = []

        def opener(request: object, timeout: float | None = None) -> FakeResponse:
            seen.append(request)
            return FakeResponse(release("v0.1.0"))

        check_for_updates("0.1.0", opener=opener)
        self.assertEqual(len(seen), 1)
        request = seen[0]
        self.assertEqual(request.full_url, LATEST_RELEASE_API)
        self.assertTrue(request.get_header("User-agent").startswith("ClipDock/"))
        self.assertIn("github+json", request.get_header("Accept"))


class DownloadableTests(unittest.TestCase):
    """Pins the boundary around downloading and running an update.

    The module now knows three addresses - the releases page, the endpoint that
    describes it, and the download path an installer must come from - and the
    last one is a prefix, not a page.  An asset on any other host is refused at
    the point it is read, so a tampered reply cannot point the download
    somewhere else.
    """

    def test_the_only_addresses_in_the_module_are_the_releases_page_its_api_and_the_download_path(self) -> None:
        addresses = {
            value
            for name, value in vars(updates).items()
            if not name.startswith("_") and isinstance(value, str) and value.startswith("http")
        }
        self.assertEqual(
            addresses,
            {RELEASES_URL, LATEST_RELEASE_API, RELEASE_DOWNLOAD_PREFIX},
        )

    def test_the_result_describes_the_installer_without_being_able_to_hold_bytes(self) -> None:
        # A version, a status, a reason, a page, and the identity of one asset.
        # There is still no field that could carry an installer's contents, and
        # nothing here starts a download by itself.
        self.assertEqual(
            {field.name for field in UpdateCheck.__dataclass_fields__.values()},
            {
                "status",
                "current",
                "latest",
                "release_url",
                "reason",
                "installer_url",
                "installer_size",
                "installer_digest",
            },
        )
        for field in UpdateCheck.__dataclass_fields__.values():
            with self.subTest(field=field.name):
                self.assertNotIn("bytes", field.type)
                self.assertNotIn("Path", str(field.type))

    def test_a_release_without_the_installer_offers_no_install(self) -> None:
        result = check_for_updates("0.1.0", opener=replying(release("v9.9.9")))
        self.assertTrue(result.available)
        self.assertFalse(result.can_install)
        self.assertEqual(result.installer_url, "")

    def test_the_published_installer_is_described_with_its_size_and_checksum(self) -> None:
        body = b"installer"
        digest = hashlib.sha256(body).hexdigest()
        result = check_for_updates(
            "0.1.0",
            opener=replying(release_with_asset(body, digest=f"sha256:{digest.upper()}")),
        )
        self.assertTrue(result.can_install)
        self.assertEqual(result.installer_size, len(body))
        self.assertEqual(result.installer_digest, f"sha256:{digest}")

    def test_an_installer_on_another_host_is_ignored(self) -> None:
        result = check_for_updates(
            "0.1.0",
            opener=replying(
                release_with_asset(
                    b"installer",
                    url=f"https://example.test/{INSTALLER_ASSET_NAME}",
                )
            ),
        )
        self.assertFalse(result.can_install)

    def test_an_asset_with_another_name_is_ignored(self) -> None:
        result = check_for_updates(
            "0.1.0",
            opener=replying(release_with_asset(b"installer", name="SomethingElse.exe")),
        )
        self.assertFalse(result.can_install)

    def test_an_installer_larger_than_the_cap_is_ignored(self) -> None:
        result = check_for_updates(
            "0.1.0",
            opener=replying(release_with_asset(b"installer", size=MAX_INSTALLER_BYTES + 1)),
        )
        self.assertFalse(result.can_install)

    def test_a_checksum_that_is_not_sha256_is_not_claimed_to_be_one(self) -> None:
        result = check_for_updates(
            "0.1.0",
            opener=replying(release_with_asset(b"installer", digest="sha512:abc")),
        )
        self.assertEqual(result.installer_digest, "")


class DownloadTests(unittest.TestCase):
    def test_the_installer_is_written_and_verified(self) -> None:
        body = b"a real installer"
        digest = hashlib.sha256(body).hexdigest()
        events: list[ProgressEvent] = []
        with tempfile.TemporaryDirectory() as directory:
            result = download_update(
                installer_check(body, digest=f"sha256:{digest}"),
                Path(directory),
                progress=events.append,
                opener=lambda *a, **k: DownloadResponse(body),
            )
            self.assertIsInstance(result, DownloadedUpdate)
            self.assertEqual(result.version, "v9.9.9")
            self.assertEqual(result.path, Path(directory) / INSTALLER_ASSET_NAME)
            self.assertEqual(result.path.read_bytes(), body)
            self.assertFalse((Path(directory) / f"{INSTALLER_ASSET_NAME}.part").exists())
        self.assertTrue(events)
        self.assertEqual(events[-1].message, "Downloading the ClipDock update")

    def test_a_checksum_mismatch_is_refused_and_nothing_is_left_behind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure) as caught:
                download_update(
                    installer_check(b"installer", digest=f"sha256:{'0' * 64}"),
                    Path(directory),
                    opener=lambda *a, **k: DownloadResponse(b"installer"),
                )
            self.assertIn("checksum", caught.exception.message)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_short_or_long_body_is_refused(self) -> None:
        for body in (b"short", b"much longer than declared"):
            with self.subTest(body=body):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaises(DownloadFailure):
                        download_update(
                            installer_check(body, size=len(body) + 1),
                            Path(directory),
                            opener=lambda *a, **k: DownloadResponse(body),
                        )
                    self.assertEqual(list(Path(directory).iterdir()), [])

    def test_cancelling_deletes_the_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CancelledError):
                download_update(
                    installer_check(b"installer"),
                    Path(directory),
                    cancel_check=lambda: True,
                    opener=lambda *a, **k: DownloadResponse(b"installer"),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_redirect_away_from_github_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure) as caught:
                download_update(
                    installer_check(b"installer"),
                    Path(directory),
                    opener=lambda *a, **k: DownloadResponse(
                        b"installer", url="https://example.test/evil.exe"
                    ),
                )
            self.assertIn("redirected away", caught.exception.message)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_check_that_names_no_installer_downloads_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure):
                download_update(
                    UpdateCheck(status=NEWER, current="0.1.0", latest="v9.9.9"),
                    Path(directory),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_a_url_outside_the_projects_download_path_is_refused_before_opening(self) -> None:
        opened: list[object] = []
        check = UpdateCheck(
            status=NEWER,
            current="0.1.0",
            latest="v9.9.9",
            installer_url="https://example.test/ClipDock-Setup.exe",
        )
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(DownloadFailure):
                download_update(
                    check,
                    Path(directory),
                    opener=lambda *a, **k: opened.append(a) or DownloadResponse(b"x"),
                )
        self.assertEqual(opened, [])


class LauncherTests(unittest.TestCase):
    def test_the_installer_is_started_silently_and_never_restarts_the_machine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / INSTALLER_ASSET_NAME
            path.write_bytes(b"installer")
            with patch.object(updates.subprocess, "Popen") as popen:
                self.assertTrue(launch_installer(path))
        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0],
            [str(path), "/SILENT", "/NORESTART"],
        )

    def test_a_missing_installer_is_not_reported_as_started(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(launch_installer(Path(directory) / INSTALLER_ASSET_NAME))

    def test_a_launch_that_raises_is_reported_rather_than_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / INSTALLER_ASSET_NAME
            path.write_bytes(b"installer")
            with patch.object(updates.subprocess, "Popen", side_effect=OSError("nope")):
                self.assertFalse(launch_installer(path))


class ApiShapeTests(unittest.TestCase):
    def test_the_repository_in_the_url_is_the_project_itself(self) -> None:
        # Typed once in one place.  A wrong owner here produces a 404 that looks
        # exactly like "this project has never been released".
        self.assertEqual(RELEASES_URL, "https://github.com/Abdalllah10Ahmed/ClipDock/releases")
        self.assertEqual(
            LATEST_RELEASE_API,
            "https://api.github.com/repos/Abdalllah10Ahmed/ClipDock/releases/latest",
        )
        # Both are derived from one owner-and-name, so they cannot disagree about
        # which project is being asked about.
        self.assertEqual(updates.REPOSITORY, "Abdalllah10Ahmed/ClipDock")


if __name__ == "__main__":
    unittest.main()