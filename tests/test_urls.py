from __future__ import annotations

import unittest

from youtube_downloader.core.urls import (
    UrlValidationError,
    normalize_media_url,
    normalize_playlist_url,
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
            "tiktok.com/@user/video/123": "https://tiktok.com/@user/video/123",
            "https://vimeo.com/12345": "https://vimeo.com/12345",
            "https://www.dailymotion.com/video/x8abc": "https://www.dailymotion.com/video/x8abc",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_media_url(raw), expected)
                self.assertEqual(normalize_youtube_url(raw), expected)

    def test_playlist_normalization_is_best_effort(self) -> None:
        cases = (
            "https://www.youtube.com/playlist?list=PL123",
            "https://www.youtube.com/watch?v=abc&list=PL123",
            "https://soundcloud.com/artist/sets/album",
            "https://vimeo.com/showcase/12345",
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_playlist_url(raw), raw)
                self.assertEqual(normalize_youtube_playlist_url(raw), raw)

    def test_rejects_invalid_links(self) -> None:
        for raw in (
            "",
            "   ",
            "ftp://example.com/video",
            "https://user:password@example.com/watch?v=abc",
            "https://[malformed",
            "https:///missing-host",
            "not a url",
        ):
            with self.subTest(raw=raw):
                with self.assertRaises(UrlValidationError):
                    normalize_media_url(raw)
                with self.assertRaises(UrlValidationError):
                    normalize_playlist_url(raw)


if __name__ == "__main__":
    unittest.main()
