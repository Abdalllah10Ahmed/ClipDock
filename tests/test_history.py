"""The history file: what goes into one entry, and what happens when it misbehaves.

Two things are being defended here.

The first is **accuracy**.  A history entry is a claim about files that exist,
so a title, a link and a count have to be provably the right ones - including
the awkward cases, where a playlist repeats a title, a caption job writes more
than one file, or the job produced nothing at all.

The second is **survival**.  This file is written on the way out of a download
that has already happened.  A missing file, a truncated file, a hand-edited
file, a directory where the file should be, an interrupted write - none of them
may raise, because none of them is more important than the result the window is
about to report.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from youtube_downloader.core import history, settings
from youtube_downloader.core.models import (
    DownloadMode,
    DownloadRequest,
    DownloadResult,
    PlaylistDownloadRequest,
    PlaylistDownloadResult,
    PlaylistInfo,
    PlaylistMedia,
    PlaylistQuality,
    QueueDownloadRequest,
    QueueDownloadResult,
    QueueItemResult,
    StreamPreference,
    SubtitleFormat,
    SubtitleSource,
    VideoInfo,
    VideoQuality,
)

# Only ever recorded as text; nothing here creates it on disk.
FOLDER = Path("C:/downloads")

STREAM = VideoQuality("v720", 720, 30, True, 1280, "mp4", 12_000_000)
PLAYLIST_QUALITY = PlaylistQuality(height=720, fps=30)


def video(title: str = "A test video", video_id: str = "id1") -> VideoInfo:
    return VideoInfo(
        video_id,
        title,
        10,
        "https://img.example/max.jpg",
        (STREAM,),
        True,
        False,
        f"https://www.youtube.com/watch?v={video_id}",
    )


def single_request(mode: DownloadMode = DownloadMode.VIDEO) -> DownloadRequest:
    return DownloadRequest(
        url="https://www.youtube.com/watch?v=id1",
        output_dir=FOLDER,
        mode=mode,
        info=video(),
        quality=STREAM,
        audio_bitrate=None,
        preference=StreamPreference.QUALITY,
        subtitle_format=SubtitleFormat.SRT,
        subtitle_source=SubtitleSource.PREFERRED,
        subtitle_language="",
        embed_cover=True,
    )


def playlist_request(*videos: VideoInfo) -> PlaylistDownloadRequest:
    return PlaylistDownloadRequest(
        url="https://www.youtube.com/playlist?list=PL1",
        output_dir=FOLDER,
        info=PlaylistInfo(
            playlist_id="PL1",
            title="A playlist",
            videos=tuple(videos),
            qualities=(PLAYLIST_QUALITY,),
            normalized_url="https://www.youtube.com/playlist?list=PL1",
            skipped_count=0,
        ),
        quality=PLAYLIST_QUALITY,
        media=PlaylistMedia.VIDEO,
        audio_bitrate=None,
        selected_video_ids=tuple(item.video_id for item in videos),
        preference=StreamPreference.QUALITY,
        subtitle_format=SubtitleFormat.SRT,
        subtitle_source=SubtitleSource.PREFERRED,
        embed_cover=False,
    )


def queue_request(*urls: str) -> QueueDownloadRequest:
    return QueueDownloadRequest(
        urls=tuple(urls),
        output_dir=FOLDER,
        media=PlaylistMedia.VIDEO,
        quality=PLAYLIST_QUALITY,
        audio_bitrate=None,
        preference=StreamPreference.QUALITY,
        subtitle_format=SubtitleFormat.SRT,
        subtitle_source=SubtitleSource.PREFERRED,
        embed_cover=False,
    )


class HistoryBuildingTests(unittest.TestCase):
    """One job in, one accurate entry out."""

    def test_a_finished_video_download_records_where_it_went_and_the_link(self) -> None:
        job = history.build_job(
            single_request(),
            DownloadResult(FOLDER / "downloaded.mp4", DownloadMode.VIDEO),
            started="2026-10-07T09:15:00",
        )

        self.assertEqual(job["started"], "2026-10-07T09:15:00")
        self.assertEqual(job["mode"], "video")
        self.assertEqual(job["requested"], "https://www.youtube.com/watch?v=id1")
        self.assertEqual(job["folder"], str(FOLDER))
        self.assertEqual(job["outcome"], history.OUTCOME_COMPLETE)
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["total"], 1)

        (item,) = job["items"]
        self.assertEqual(item["title"], "A test video")
        self.assertEqual(item["destination"], str(FOLDER / "downloaded.mp4"))
        self.assertEqual(item["outcome"], history.ITEM_SAVED)
        self.assertEqual(item["url"], "https://www.youtube.com/watch?v=id1")

    def test_a_caption_job_lists_every_language_it_wrote(self) -> None:
        paths = tuple(FOLDER / f"downloaded.{language}.srt" for language in ("en", "de", "fr"))
        job = history.build_job(
            single_request(DownloadMode.SUBTITLES),
            DownloadResult(paths[-1], DownloadMode.SUBTITLES, paths),
        )

        self.assertEqual(job["succeeded"], 3)
        self.assertEqual(job["total"], 3)
        self.assertEqual([item["destination"] for item in job["items"]], [str(p) for p in paths])
        self.assertEqual({item["outcome"] for item in job["items"]}, {history.ITEM_SAVED})

    def test_a_playlist_names_the_reason_each_file_failed(self) -> None:
        job = history.build_job(
            playlist_request(video("Sprite Fright", "abc")),
            PlaylistDownloadResult(
                paths=(FOLDER / "Sprite Fright.mp4",),
                total=3,
                failures=(("Overgrown", "HTTP Error 429: Too Many Requests"),),
                failed_video_ids=("xyz",),
                fallback_video_count=0,
                media=PlaylistMedia.VIDEO,
                skipped_count=0,
            ),
            started="2026-10-07T10:00:00",
        )

        self.assertEqual(job["mode"], "playlist")
        self.assertEqual(job["outcome"], history.OUTCOME_PARTIAL)
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(job["failed"], 1)
        self.assertEqual(job["total"], 3)

        saved, failed = job["items"]
        # The saved file is looked up in what the job was actually asked for, so
        # the title is the real one and not a guess from the filename.
        self.assertEqual(saved["title"], "Sprite Fright")
        self.assertEqual(saved["url"], "https://www.youtube.com/watch?v=abc")
        self.assertEqual(failed["title"], "Overgrown")
        self.assertEqual(failed["reason"], "HTTP Error 429: Too Many Requests")
        self.assertEqual(failed["destination"], str(FOLDER))

    def test_a_title_two_videos_share_is_never_answered_with_the_wrong_link(self) -> None:
        request = playlist_request(video("Untitled", "first"), video("Untitled", "second"))
        job = history.build_job(
            request,
            PlaylistDownloadResult(
                paths=(FOLDER / "Untitled.mp4",),
                total=2,
                failures=(),
                failed_video_ids=(),
                fallback_video_count=0,
                media=PlaylistMedia.VIDEO,
                skipped_count=0,
            ),
        )

        # Two videos answer to that name, so neither one may be picked: the row
        # keeps the file's own name and gives up the link rather than giving the
        # wrong one.
        (item,) = job["items"]
        self.assertEqual(item["title"], "Untitled")
        self.assertEqual(item["url"], "")
        self.assertEqual(item["outcome"], history.ITEM_SAVED)

    def test_a_batch_job_gives_every_link_its_own_row(self) -> None:
        job = history.build_job(
            queue_request("https://youtu.be/a", "https://youtu.be/b", "https://youtu.be/c"),
            QueueDownloadResult(
                items=(
                    QueueItemResult(
                        url="https://youtu.be/a",
                        index=0,
                        path=FOLDER / "a.mp4",
                        error="",
                        title="A",
                        used_fallback_quality=False,
                    ),
                    QueueItemResult(
                        url="https://youtu.be/b",
                        index=1,
                        path=None,
                        error="HTTP Error 429",
                        title="B",
                        used_fallback_quality=False,
                    ),
                    QueueItemResult(
                        url="https://youtu.be/c",
                        index=2,
                        path=None,
                        error="",
                        title="C",
                        used_fallback_quality=False,
                    ),
                ),
                media=PlaylistMedia.VIDEO,
            ),
            started="2026-10-07T11:00:00",
        )

        self.assertEqual(job["mode"], "queue")
        self.assertEqual(job["requested"], "3 links")
        self.assertEqual(job["outcome"], history.OUTCOME_PARTIAL)
        self.assertEqual(job["succeeded"], 1)
        self.assertEqual(job["failed"], 2)
        self.assertEqual(job["total"], 3)
        self.assertEqual([item["url"] for item in job["items"]], [
            "https://youtu.be/a",
            "https://youtu.be/b",
            "https://youtu.be/c",
        ])
        self.assertEqual(job["items"][1]["reason"], "HTTP Error 429")
        # A link that produced no file still knows where to look for it.
        self.assertEqual(job["items"][1]["destination"], str(FOLDER))

    def test_a_job_that_produced_nothing_says_so_rather_than_claiming_success(self) -> None:
        job = history.build_job(
            single_request(),
            None,
            outcome=history.OUTCOME_FAILED,
            reason="HTTP Error 429: Too Many Requests",
        )

        self.assertEqual(job["outcome"], history.OUTCOME_FAILED)
        self.assertEqual(job["succeeded"], 0)
        # One file was asked for and it did not arrive, so that is one failure
        # out of one attempted - the alternative would be a header claiming
        # nothing went wrong about a job that went entirely wrong.
        self.assertEqual(job["failed"], 1)
        self.assertEqual(job["total"], 1)
        (item,) = job["items"]
        self.assertEqual(item["outcome"], history.ITEM_FAILED)
        self.assertEqual(item["reason"], "HTTP Error 429: Too Many Requests")
        self.assertEqual(item["destination"], str(FOLDER))

    def test_a_stopped_job_is_a_stop_and_not_a_failure(self) -> None:
        job = history.build_job(
            single_request(),
            None,
            outcome=history.OUTCOME_PAUSED,
            reason="the pieces already written were kept",
        )

        self.assertEqual(job["outcome"], history.OUTCOME_PAUSED)
        # Nothing watched which files this job owns, so nothing is counted as
        # lost: the two numbers stay at zero and the outcome carries the truth.
        self.assertEqual(job["succeeded"], 0)
        self.assertEqual(job["failed"], 0)
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["items"][0]["outcome"], history.ITEM_STOPPED)

    def test_the_job_kind_comes_from_what_was_asked_for_not_from_the_media(self) -> None:
        # A playlist has no `mode`; it has `media`, whose value here is "video".
        # Reading that straight through would file a playlist under Video.
        playlist = history.build_job(playlist_request(video()), None)
        batch = history.build_job(queue_request("https://youtu.be/a"), None)
        single = history.build_job(single_request(DownloadMode.AUDIO), None)

        self.assertEqual(playlist["mode"], "playlist")
        self.assertEqual(batch["mode"], "queue")
        self.assertEqual(single["mode"], "audio")

    def test_the_settings_are_written_even_though_the_window_never_shows_them(self) -> None:
        job = history.build_job(single_request(DownloadMode.AUDIO), None)

        request = job["request"]
        self.assertEqual(request["output_dir"], str(FOLDER))
        self.assertEqual(request["mode"], "audio")
        self.assertEqual(request["quality"], {"format_id": "v720", "height": 720, "fps": 30})
        self.assertIs(request["embed_cover"], True)
        # The whole entry has to survive JSON: it is written to a file, not to a
        # picker, and a Path in here would make every record undecodable.
        self.assertEqual(json.loads(json.dumps(job))["request"]["output_dir"], str(FOLDER))

    def test_a_video_is_never_recorded_as_a_setting(self) -> None:
        job = history.build_job(single_request(), None)

        self.assertNotIn("info", job["request"])
        self.assertNotIn("videos", job["request"])
        # Estimated sizes are a reading of one moment and mean nothing later.
        self.assertEqual(
            job["request"]["quality"],
            {"format_id": "v720", "height": 720, "fps": 30},
        )

    def test_a_result_from_an_unknown_job_is_not_invented_into_one(self) -> None:
        self.assertEqual(history.build_job(single_request(), object()), {})


class HistoryStorageTests(unittest.TestCase):
    """The file itself, including every way it can be wrong."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        self.path = self.folder / "history.json"

    def test_a_missing_file_is_the_normal_first_launch(self) -> None:
        self.assertFalse(self.path.exists())
        self.assertEqual(history.read_history(self.path), [])

    def test_jobs_come_back_in_the_order_they_were_written(self) -> None:
        self.assertTrue(history.record_job({"marker": "first"}, self.path))
        self.assertTrue(history.record_job({"marker": "second"}, self.path))

        self.assertEqual(
            [job["marker"] for job in history.read_history(self.path)],
            ["first", "second"],
        )

    def test_the_oldest_jobs_are_dropped_once_the_cap_is_passed(self) -> None:
        for marker in range(history.MAX_HISTORY_JOBS + 5):
            self.assertTrue(history.record_job({"marker": marker}, self.path))

        jobs = history.read_history(self.path)
        self.assertEqual(len(jobs), history.MAX_HISTORY_JOBS)
        self.assertEqual(jobs[0]["marker"], 5)
        self.assertEqual(jobs[-1]["marker"], history.MAX_HISTORY_JOBS + 4)

    def test_a_truncated_file_reads_as_empty_instead_of_raising(self) -> None:
        self.path.write_text('[{"started": "2026-10-0', encoding="utf-8")

        self.assertEqual(history.read_history(self.path), [])

    def test_a_file_holding_the_wrong_document_reads_as_empty(self) -> None:
        self.path.write_text('{"started": "2026-10-07"}', encoding="utf-8")

        self.assertEqual(history.read_history(self.path), [])

    def test_a_file_that_is_a_directory_reads_as_empty(self) -> None:
        self.path.mkdir()

        self.assertEqual(history.read_history(self.path), [])

    def test_entries_of_the_wrong_shape_never_reach_the_window(self) -> None:
        self.path.write_text(
            json.dumps(
                [
                    7,
                    "not a job",
                    {"started": "no items here"},
                    {"items": "not a list"},
                    {"items": [1, "two", {"title": "real", "destination": "somewhere"}]},
                ]
            ),
            encoding="utf-8",
        )

        jobs = history.read_history(self.path)
        # The two that were not dictionaries are dropped; the ones that were a
        # job but listed their files wrongly are kept with an empty list rather
        # than thrown away, because the header is still worth having.
        self.assertEqual(len(jobs), 3)
        self.assertEqual(jobs[0]["items"], [])
        self.assertEqual(jobs[1]["items"], [])
        self.assertEqual(jobs[2]["items"], [{"title": "real", "destination": "somewhere"}])

    def test_a_history_that_cannot_be_written_reports_it_without_raising(self) -> None:
        blocker = self.folder / "blocker"
        blocker.write_text("a file where a directory belongs", encoding="utf-8")

        self.assertFalse(history.record_job({"marker": "nowhere"}, blocker / "history.json"))
        self.assertEqual(history.read_history(blocker / "history.json"), [])

    def test_an_interrupted_write_leaves_the_previous_history_intact(self) -> None:
        self.assertTrue(history.record_job({"marker": "before"}, self.path))
        original = self.path.read_text(encoding="utf-8")

        with patch.object(history.os, "replace", side_effect=OSError("interrupted")):
            self.assertFalse(history.record_job({"marker": "after"}, self.path))

        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        self.assertEqual([job["marker"] for job in history.read_history(self.path)], ["before"])
        # The half-written copy is cleaned up rather than left beside the file
        # to be mistaken for a second history on a later run.
        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_no_temporary_file_is_left_behind_by_a_successful_write(self) -> None:
        self.assertTrue(history.record_job({"marker": "only"}, self.path))

        self.assertEqual(list(self.folder.glob("*.tmp")), [])

    def test_clearing_takes_everything_and_says_so_when_there_was_nothing(self) -> None:
        self.assertTrue(history.record_job({"marker": "first"}, self.path))
        self.assertTrue(history.record_job({"marker": "second"}, self.path))

        self.assertTrue(history.clear_history(self.path))
        self.assertEqual(history.read_history(self.path), [])
        self.assertTrue(history.clear_history(self.path))

    def test_the_history_is_its_own_file_and_not_the_settings_file(self) -> None:
        self.assertNotEqual(history.default_history_path(), settings.default_settings_path())
        self.assertEqual(
            history.default_history_path().name,
            history.HISTORY_FILENAME,
        )
        self.assertEqual(
            history.default_history_path().parent,
            settings.default_settings_directory(),
        )


if __name__ == "__main__":
    unittest.main()
