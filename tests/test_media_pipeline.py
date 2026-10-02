from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP
from yt_dlp.postprocessor.ffmpeg import FFmpegExtractAudioPP, FFmpegMergerPP, FFmpegVideoConvertorPP

from youtube_downloader.core.binaries import find_ffmpeg

# Enough to catch a cover image left behind in any of the formats the pipeline
# might produce one in.
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})


def media_description(ffmpeg: Path, path: Path) -> str:
    """Return ffmpeg's own description of a media file.

    ffprobe is intentionally not bundled because the application never calls
    it.  The tests read the same stream information out of ffmpeg so the
    assertions keep their real coverage without needing the 128 MB binary.
    ``ffmpeg -i`` with no output file always exits non-zero, so the return
    code is deliberately ignored.
    """

    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
    )
    return result.stderr


def reported_bitrate_kbps(ffmpeg: Path, path: Path) -> int:
    """Return the overall bitrate in kb/s that ffmpeg reports for a file."""

    match = re.search(r"bitrate:\s*(\d+)\s*kb/s", media_description(ffmpeg, path))
    if match is None:
        raise AssertionError(f"ffmpeg did not report a bitrate for {path}")
    return int(match.group(1))


def id3_frame_ids(path: Path) -> set[str]:
    """Return the frame identifiers in an MP3's leading ID3v2 tag.

    Read straight out of the bytes rather than through a library, because the
    question being asked is whether the artwork physically made it into the
    file.  ffmpeg's own description of a file mentions the attached picture, but
    that is ffmpeg reporting on itself; this is the tag actually being there.
    """

    data = path.read_bytes()
    if data[:3] != b"ID3":
        return set()
    # A synchsafe size: seven bits per byte, high bit always clear.
    size = 0
    for byte in data[6:10]:
        size = (size << 7) | (byte & 0x7F)
    tag = data[10 : 10 + size]
    identifiers: set[str] = set()
    offset = 0
    while offset + 10 <= len(tag):
        identifier = tag[offset : offset + 4]
        if not identifier.strip(b"\x00"):
            break  # padding, which is where the tag ends
        identifiers.add(identifier.decode("latin-1"))
        offset += 10 + int.from_bytes(tag[offset + 4 : offset + 8], "big")
    return identifiers


class MediaPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).resolve().parents[1]
        cls.ffmpeg = find_ffmpeg(root)
        if cls.ffmpeg is None:
            raise unittest.SkipTest("Bundled FFmpeg is not available.")
        cls._temporary = tempfile.TemporaryDirectory(prefix="ydl-media-test-")
        cls.directory = Path(cls._temporary.name)
        cls.source = cls.directory / "source.webm"
        subprocess.run(
            [
                str(cls.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=320x180:r=30",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "1",
                "-c:v",
                "libvpx",
                "-c:a",
                "libopus",
                "-shortest",
                str(cls.source),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _downloader(self, *, video: bool = True) -> YoutubeDL:
        options = {"ffmpeg_location": str(self.ffmpeg), "quiet": True}
        if video:
            options["postprocessor_args"] = {
                "videoconvertor+ffmpeg": ["-c:v", "libopenh264", "-c:a", "aac"]
            }
        return YoutubeDL(options)

    def test_video_conversion_creates_mp4_with_aac(self) -> None:
        work = self.directory / "video"
        work.mkdir()
        source = work / "source.webm"
        shutil.copy2(self.source, source)
        info = {"filepath": str(source), "ext": "webm", "vcodec": "vp9", "acodec": "opus"}
        _, converted = FFmpegVideoConvertorPP(self._downloader(), preferedformat="mp4").run(info)
        output = Path(converted["filepath"])
        self.assertEqual(output.suffix, ".mp4")
        self.assertGreater(output.stat().st_size, 0)
        description = media_description(self.ffmpeg, output)
        self.assertIn("Video: h264", description)
        self.assertIn("Audio: aac", description)

    def test_split_merge_keeps_streams_copy_with_scoped_conversion_args(self) -> None:
        work = self.directory / "split-merge"
        work.mkdir()
        video = work / "video.mp4"
        audio = work / "audio.m4a"
        output = work / "merged.mp4"
        subprocess.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc=size=160x90:rate=30",
                "-t",
                "1",
                "-c:v",
                "mpeg4",
                str(video),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "1",
                "-c:a",
                "aac",
                str(audio),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        info = {
            "filepath": str(output),
            "requested_formats": [
                {"filepath": str(video), "acodec": "none", "vcodec": "mpeg4", "protocol": "https"},
                {"filepath": str(audio), "acodec": "aac", "vcodec": "none", "protocol": "https"},
            ],
            "__files_to_merge": [str(video), str(audio)],
        }
        FFmpegMergerPP(self._downloader()).run(info)
        self.assertGreater(output.stat().st_size, 0)
        description = media_description(self.ffmpeg, output)
        self.assertIn("Video: mpeg4", description)
        self.assertIn("Audio: aac", description)

    def test_mp3_conversion_supports_all_requested_bitrates(self) -> None:
        reported: dict[int, int] = {}
        for bitrate in (128, 192, 256, 320):
            with self.subTest(bitrate=bitrate):
                work = self.directory / f"audio-{bitrate}"
                work.mkdir()
                source = work / "source.webm"
                shutil.copy2(self.source, source)
                info = {"filepath": str(source), "ext": "webm", "vcodec": "vp9", "acodec": "opus"}
                _, converted = FFmpegExtractAudioPP(
                    self._downloader(video=False), preferredcodec="mp3", preferredquality=str(bitrate)
                ).run(info)
                output = Path(converted["filepath"])
                self.assertEqual(output.suffix, ".mp3")
                self.assertGreater(output.stat().st_size, 0)
                description = media_description(self.ffmpeg, output)
                self.assertIn("Audio: mp3", description)
                reported[bitrate] = reported_bitrate_kbps(self.ffmpeg, output)

        # ``ffmpeg -i`` reports the average bitrate of the whole file, so the
        # ID3/Xing tag, the LAME header, and the encoder delay/padding show up
        # as a fixed overhead.  On a one-second clip that is a consistent ~6%,
        # so compare proportionally rather than exactly.
        for bitrate, actual in reported.items():
            with self.subTest(bitrate=bitrate, reported=actual):
                self.assertAlmostEqual(actual, bitrate, delta=bitrate * 0.10)

        # A proportional tolerance is far looser than the 50% gap between the
        # supported rates, so this still proves each requested rate was applied
        # and that the four settings produce genuinely different files.
        self.assertEqual(list(reported), sorted(reported))
        self.assertEqual(
            [reported[bitrate] for bitrate in sorted(reported)],
            sorted(reported.values()),
        )

    def _make_mp3(self, work: Path) -> Path:
        """Produce the MP3 the audio path ends up with, cover not yet attached."""

        work.mkdir(exist_ok=True)
        source = work / "source.webm"
        shutil.copy2(self.source, source)
        info = {"filepath": str(source), "ext": "webm", "vcodec": "vp9", "acodec": "opus"}
        _, converted = FFmpegExtractAudioPP(
            self._downloader(video=False), preferredcodec="mp3", preferredquality="192"
        ).run(info)
        return Path(converted["filepath"])

    def _make_cover(self, work: Path, name: str, extension: str) -> Path:
        cover = work / name
        subprocess.run(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x180",
                "-frames:v",
                "1",
                str(cover),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.assertEqual(cover.suffix, extension)
        return cover

    def test_cover_art_is_embedded_in_the_mp3_and_the_loose_image_removed(self) -> None:
        # The complaint this answers: a downloaded track showed no artwork, so
        # every player drew a blank icon.  The cover has to be inside the file,
        # and the intermediate image yt-dlp fetches must not be left behind.
        work = self.directory / "cover"
        mp3 = self._make_mp3(work)
        before = mp3.stat().st_size
        self.assertNotIn("APIC", id3_frame_ids(mp3))

        cover = self._make_cover(work, "cover.jpg", ".jpg")
        info = {
            "filepath": str(mp3),
            "ext": "mp3",
            "thumbnails": [{"id": "0", "url": "unused", "filepath": str(cover)}],
        }
        _, embedded = EmbedThumbnailPP(self._downloader(video=False)).run(info)

        # The same path, still named .mp3: the postprocessor writes a temporary
        # file and puts it back, so a player never sees a ".temp" extension.
        self.assertEqual(Path(embedded["filepath"]), mp3)
        self.assertTrue(mp3.is_file())
        self.assertGreater(mp3.stat().st_size, before)
        self.assertIn("APIC", id3_frame_ids(mp3))
        self.assertIn("attached pic", media_description(self.ffmpeg, mp3))
        # The audio is still there; only the artwork was added.
        self.assertIn("Audio: mp3", media_description(self.ffmpeg, mp3))
        # A cover left lying next to the track is not what was asked for.
        self.assertFalse(cover.exists())
        self.assertEqual(
            [item.name for item in work.iterdir() if item.suffix.lower() in _IMAGE_SUFFIXES],
            [],
        )

    def test_a_cover_in_an_unsupported_format_is_converted_before_embedding(self) -> None:
        # YouTube serves webp for many thumbnails, and an MP3 tag cannot hold
        # one.  yt-dlp converts it through ffmpeg first; this checks the
        # conversion happens and the file still ends up with artwork.
        work = self.directory / "cover-webp"
        mp3 = self._make_mp3(work)
        cover = self._make_cover(work, "cover.webp", ".webp")
        info = {
            "filepath": str(mp3),
            "ext": "mp3",
            "thumbnails": [{"id": "0", "url": "unused", "filepath": str(cover)}],
        }
        EmbedThumbnailPP(self._downloader(video=False)).run(info)
        self.assertIn("APIC", id3_frame_ids(mp3))
        # Both the original and the converted intermediate are cleaned up.
        self.assertEqual(
            [item.name for item in work.iterdir() if item.suffix.lower() in _IMAGE_SUFFIXES],
            [],
        )

    def test_a_track_with_no_thumbnail_still_succeeds(self) -> None:
        # Not every video has artwork.  The postprocessor has to let that pass
        # silently rather than failing a download that is otherwise complete.
        work = self.directory / "cover-missing"
        mp3 = self._make_mp3(work)
        info = {"filepath": str(mp3), "ext": "mp3", "thumbnails": []}
        _, embedded = EmbedThumbnailPP(self._downloader(video=False)).run(info)
        self.assertEqual(Path(embedded["filepath"]), mp3)
        self.assertTrue(mp3.is_file())
        self.assertNotIn("APIC", id3_frame_ids(mp3))
        self.assertIn("Audio: mp3", media_description(self.ffmpeg, mp3))


if __name__ == "__main__":
    unittest.main()
