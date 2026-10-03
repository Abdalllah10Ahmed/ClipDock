from __future__ import annotations

import json
import unittest
import urllib.error

from youtube_downloader.core import updates
from youtube_downloader.core.updates import (
    CURRENT,
    LATEST_RELEASE_API,
    NEWER,
    RELEASES_URL,
    UNKNOWN,
    UpdateCheck,
    check_for_updates,
    is_newer,
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


class ReportOnlyTests(unittest.TestCase):
    """Pins the decision that this module reports and never installs.

    There is no code path here for an update to be fetched, so this is about the
    module's whole surface rather than any one function: the only addresses it
    knows are the releases page and the endpoint that describes it.  A change
    that adds a second address has to come here and argue with this.
    """

    def test_the_only_addresses_in_the_module_are_the_releases_page_and_its_api(self) -> None:
        addresses = {
            value
            for name, value in vars(updates).items()
            if not name.startswith("_") and isinstance(value, str) and value.startswith("http")
        }
        self.assertEqual(addresses, {RELEASES_URL, LATEST_RELEASE_API})

    def test_the_result_carries_no_payload_a_downloader_could_use(self) -> None:
        # A version, a status, a reason, and a page.  There is no field that
        # could hold an installer or a download URL handed over as trusted.
        self.assertEqual(
            {field.name for field in UpdateCheck.__dataclass_fields__.values()},
            {"status", "current", "latest", "release_url", "reason"},
        )
        for field in UpdateCheck.__dataclass_fields__.values():
            with self.subTest(field=field.name):
                self.assertNotIn("bytes", field.type)
                self.assertNotIn("Path", str(field.type))


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