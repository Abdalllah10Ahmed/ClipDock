from __future__ import annotations

import unittest

from youtube_downloader.core.urls import (
    UrlValidationError,
    normalize_youtube_playlist_url,
    normalize_youtube_url,
)


class UrlTests(unittest.TestCase):
    def test_normalizes_supported_url_forms(self) -> None:
        cases = {
            "youtube.com/watch?v=abc": "https://youtube.com/watch?v=abc",
            " https://www.youtube.com/shorts/abc ": "https://www.youtube.com/shorts/abc",
            "youtu.be/abc": "https://youtu.be/abc",
            "https://music.youtube.com/watch?v=abc": "https://music.youtube.com/watch?v=abc",
            "https://www.youtube-nocookie.com/embed/abc": "https://www.youtube-nocookie.com/embed/abc",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_youtube_url(raw), expected)
        self.assertEqual(
            normalize_youtube_url("https://www.youtube.com/watch?v=abc&list=PL123"),
            "https://www.youtube.com/watch?v=abc&list=PL123",
        )

    def test_playlist_url_is_only_accepted_by_explicit_playlist_mode(self) -> None:
        raw = "https://www.youtube.com/playlist?list=PL123"
        self.assertEqual(normalize_youtube_playlist_url(raw), raw)
        with self.assertRaises(UrlValidationError):
            normalize_youtube_url(raw)
        with self.assertRaises(UrlValidationError):
            normalize_youtube_playlist_url("https://www.youtube.com/watch?v=abc")
        with self.assertRaises(UrlValidationError):
            normalize_youtube_playlist_url("https://example.com/playlist?list=PL123")
        with self.assertRaises(UrlValidationError):
            normalize_youtube_playlist_url("https://example.com/playlist?list=PL123")
        with self.assertRaises(UrlValidationError):
            normalize_youtube_playlist_url("https://www.youtube.com/playlist")

    def test_video_link_inside_a_playlist_resolves_to_the_playlist(self) -> None:
        """The address bar yields watch?v=..&list=.., which is the same playlist."""

        cases = {
            "https://www.youtube.com/watch?v=abc&list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch?v=abc&list=PL123&index=4": "https://www.youtube.com/playlist?list=PL123",
            "https://youtu.be/abc?list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://music.youtube.com/watch?v=abc&list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/shorts/abc?list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/live/abc?list=PL123": "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/embed/abc?list=PL123": "https://www.youtube.com/playlist?list=PL123",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_youtube_playlist_url(raw), expected)

    def test_playlist_mode_still_rejects_links_without_a_list_id(self) -> None:
        for raw in (
            "https://www.youtube.com/watch?v=abc",
            "https://www.youtube.com/shorts/abc",
            "https://youtu.be/abc",
            "https://www.youtube.com/playlist?list=",
            "https://www.youtube.com/playlist",
            # A collection URL is rejected before the list rewrite is considered.
            "https://www.youtube.com/@creator?list=PL123",
            "https://www.youtube.com/channel/UC123?list=PL123",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(UrlValidationError):
                    normalize_youtube_playlist_url(raw)

    def test_rejects_non_youtube_and_collection_links(self) -> None:
        for raw in (
            "https://example.com/watch?v=abc",
            "https://youtube.com/playlist?list=abc",
            "https://youtube.com/channel/UC123",
            "https://youtube.com/@creator",
            "https://youtube.com/watch",
            "https://youtube.com/gaming",
            "https://youtube.com/hashtag/music",
            "https://youtube.com/account",
            "https://youtube.com/clip/abc",
            "https://youtube.com/shorts",
            "ftp://youtube.com/watch?v=abc",
            "https://user:password@youtube.com/watch?v=abc",
            "https://[malformed",
            "",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(UrlValidationError):
                    normalize_youtube_url(raw)


if __name__ == "__main__":
    unittest.main()
