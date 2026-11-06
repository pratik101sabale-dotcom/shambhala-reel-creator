"""Offline tests for the local Reel creator.

The mocked tests fake FFmpeg and ffprobe, so they don't need either one installed.
RealFfmpegTests renders synthetic clips, and it skips itself until FFmpeg is on your
PATH. Nothing here uses the network, reads a real token or publishes. Run with:

    .\\.venv\\Scripts\\python.exe -m unittest test_reel_creator -v
"""

import ast
import json
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import reel_creator as reel

TODAY = date(2026, 11, 4)
FAKE_TOKEN = "FAKE-TOKEN-abc123"


def box(name, body=b""):
    return struct.pack(">I4s", 8 + len(body), name.encode()) + body


GOOD_MP4 = box("ftyp", b"isom") + box("moov", box("trak", box("tkhd"))) + box("mdat", b"x" * 16)
MOOV_LAST = box("ftyp", b"isom") + box("mdat", b"x" * 16) + box("moov", box("trak"))
WITH_EDIT_LIST = (
    box("ftyp", b"isom")
    + box("moov", box("trak", box("edts", box("elst", b"\0" * 8))))
    + box("mdat", b"x" * 16)
)


def video_stream(**overrides):
    stream = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "pix_fmt": "yuv420p",
        "avg_frame_rate": "30/1",
        "field_order": "progressive",
    }
    stream.update(overrides)
    return stream


def audio_stream(**overrides):
    stream = {"index": 1, "codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}
    stream.update(overrides)
    return stream


def probe_body(streams, duration=20.0, format_name="mov,mp4,m4a,3gp,3g2,mj2", bit_rate=5_000_000):
    return {
        "streams": streams,
        "format": {"duration": str(duration), "format_name": format_name, "bit_rate": str(bit_rate)},
    }


class FakeTools:
    """Stands in for subprocess.run. Answers ffprobe and writes a fake ffmpeg output."""

    def __init__(self, source=None, music=None, output=None, render_code=0, output_bytes=GOOD_MP4):
        self.source = source or probe_body([video_stream(), audio_stream()])
        self.music = music or probe_body([audio_stream(index=0)], duration=60.0, format_name="mp3")
        self.output = output or probe_body([video_stream(), audio_stream()])
        self.render_code = render_code
        self.output_bytes = output_bytes
        self.calls = []
        self.environments = []
        self.keyword_arguments = []

    @property
    def render_calls(self):
        return [call for call in self.calls if Path(call[0]).stem == "ffmpeg"]

    def __call__(self, arguments, **keywords):
        self.calls.append(arguments)
        self.environments.append(keywords.get("env"))
        self.keyword_arguments.append(keywords)
        if Path(arguments[0]).stem == "ffmpeg":
            if self.render_code == 0:
                Path(arguments[-1]).write_bytes(self.output_bytes)
            else:
                Path(arguments[-1]).write_bytes(b"partial")
            return subprocess.CompletedProcess(arguments, self.render_code, "", "ffmpeg said no")
        path = Path(arguments[-1])
        if path.suffix.lower() in reel.MUSIC_EXTENSIONS:
            body = self.music
        elif path.parent.name == "previews":
            body = self.output
        else:
            body = self.source
        return subprocess.CompletedProcess(arguments, 0, json.dumps(body), "")


class ReelTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.music_dir = self.root / "music"
        self.previews = self.root / "previews"
        self.music_dir.mkdir()
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"original video bytes")
        which = mock.patch.object(reel.shutil, "which", side_effect=lambda name: f"C:/tools/{name}.exe")
        which.start()
        self.addCleanup(which.stop)
        environment = mock.patch.dict(os.environ, {"IG_ACCESS_TOKEN": FAKE_TOKEN, "IG_PUBLISHING_MODE": "live"})
        environment.start()
        self.addCleanup(environment.stop)

    def add_track(self, file="piano.mp3", create=True, **overrides):
        entry = {
            "file": file,
            "title": file,
            "moods": ["calm"],
            "license_type": "purchased_license",
            "business_use_verified": True,
            "proof": "C:/Licences/receipt.pdf",
        }
        entry.update(overrides)
        if create:
            (self.music_dir / Path(file).name).write_bytes(b"music bytes")
        path = self.music_dir / reel.LICENSES_FILE_NAME
        body = json.loads(path.read_text()) if path.exists() else {"tracks": []}
        body["tracks"].append(entry)
        path.write_text(json.dumps(body))

    def use_tools(self, **options):
        tools = FakeTools(**options)
        patcher = mock.patch.object(reel.subprocess, "run", tools)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tools

    def render(self, track="piano.mp3", **options):
        return reel.render_reel(
            self.video, track, music_dir=self.music_dir, output_dir=self.previews, today=TODAY, **options
        )

    def graph(self, tools):
        command = tools.render_calls[0]
        return command[command.index("-filter_complex") + 1]


class FfmpegCheckTests(unittest.TestCase):
    def test_missing_ffmpeg_explains_installation_and_runs_nothing(self):
        for found in ({"ffprobe": "x"}, {"ffmpeg": "x"}, {}):
            with self.subTest(found=found), mock.patch.object(reel.shutil, "which", side_effect=found.get):
                with mock.patch.object(reel.subprocess, "run") as run:
                    with self.assertRaisesRegex(reel.ReelError, "winget install --id Gyan.FFmpeg"):
                        reel.find_tools()
                    run.assert_not_called()

    def test_check_command_reports_the_missing_tool(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.multiple(
            reel, MUSIC_DIR=Path(folder) / "m", PREVIEW_DIR=Path(folder) / "p"
        ), mock.patch.object(reel.shutil, "which", return_value=None), mock.patch("sys.stderr"):
            self.assertEqual(reel.main(["check"]), 1)


class LicenceTests(ReelTestCase):
    def reason(self, **overrides):
        self.add_track(**overrides)
        track = reel.load_tracks(self.music_dir, TODAY)[0]
        self.assertFalse(track["eligible"])
        return track["reason"]

    def test_complete_record_is_eligible(self):
        self.add_track()
        self.assertTrue(reel.load_tracks(self.music_dir, TODAY)[0]["eligible"])

    def test_file_with_no_record_is_not_eligible(self):
        (self.music_dir / "ripped-hit-song.mp3").write_bytes(b"x")
        track = reel.load_tracks(self.music_dir, TODAY)[0]
        self.assertFalse(track["eligible"])
        self.assertIn("no entry", track["reason"])

    def test_no_licence_file_means_nothing_is_eligible(self):
        (self.music_dir / "a.mp3").write_bytes(b"x")
        self.assertFalse(any(item["eligible"] for item in reel.load_tracks(self.music_dir, TODAY)))

    def test_verification_must_be_literally_true(self):
        for value in (False, "true", 1, None):
            with self.subTest(value=value):
                (self.music_dir / reel.LICENSES_FILE_NAME).unlink(missing_ok=True)
                self.assertIn("business_use_verified", self.reason(business_use_verified=value))

    def test_proof_is_required(self):
        for value in ("", "   ", None):
            with self.subTest(value=value):
                (self.music_dir / reel.LICENSES_FILE_NAME).unlink(missing_ok=True)
                self.assertIn("proof", self.reason(proof=value))

    def test_non_commercial_and_unknown_licences_are_refused(self):
        for value in ("cc_by_nc", "personal", "youtube", None):
            with self.subTest(value=value):
                (self.music_dir / reel.LICENSES_FILE_NAME).unlink(missing_ok=True)
                self.assertIn("license_type", self.reason(license_type=value))

    def test_cc_by_needs_attribution(self):
        self.assertIn("attribution", self.reason(license_type="cc_by"))

    def test_expired_licence_is_refused(self):
        self.assertIn("expired", self.reason(expires="2026-11-03"))

    def test_licence_ending_today_still_works(self):
        self.add_track(expires="2026-11-04")
        self.assertTrue(reel.load_tracks(self.music_dir, TODAY)[0]["eligible"])

    def test_bad_expiry_date_is_refused(self):
        self.assertIn("expires", self.reason(expires="soon"))

    def test_entry_can_not_point_outside_the_music_folder(self):
        outside = self.root / "outside.mp3"
        outside.write_bytes(b"x")
        self.assertIn("plain file name", self.reason(file="../outside.mp3", create=False))

    def test_entry_for_a_missing_file_is_refused(self):
        self.assertIn("isn't in the music folder", self.reason(create=False))

    def test_broken_licence_file_stops_with_a_clear_error(self):
        for text in ("not json", "[]", '{"tracks": "x"}', '{"tracks": ["x"]}'):
            with self.subTest(text=text):
                (self.music_dir / reel.LICENSES_FILE_NAME).write_text(text)
                with self.assertRaisesRegex(reel.ReelError, "couldn't be read"):
                    reel.load_tracks(self.music_dir, TODAY)

    def test_example_file_is_not_usable_until_you_verify_it(self):
        body = json.loads((reel.BASE_DIR / "music_licenses.example.json").read_text(encoding="utf-8"))
        self.assertIs(body["tracks"][0]["business_use_verified"], False)


class RecommendationTests(ReelTestCase):
    def recommend(self, moods=(), duration=20.0):
        return reel.recommend_tracks(duration, list(moods), "ffprobe", self.music_dir, TODAY)

    def test_mood_match_ranks_first(self):
        self.add_track("a-energetic.mp3", moods=["energetic"])
        self.add_track("b-calm.mp3", moods=["calm"])
        self.use_tools()
        recommended, _ = self.recommend(["calm"])
        self.assertEqual([item["file"] for item in recommended], ["b-calm.mp3", "a-energetic.mp3"])
        self.assertIn("calm", recommended[0]["reasons"][0])

    def test_long_enough_track_ranks_above_a_short_one(self):
        self.add_track("a.mp3")
        self.use_tools(music=probe_body([audio_stream()], duration=60.0))
        recommended, _ = self.recommend()
        self.assertEqual(recommended[0]["score"], 1)
        self.use_tools(music=probe_body([audio_stream()], duration=5.0))
        recommended, _ = self.recommend()
        self.assertEqual(recommended[0]["score"], 0)
        self.assertIn("loop", recommended[0]["reasons"][0])

    def test_tracks_without_permission_are_never_recommended(self):
        self.add_track("ok.mp3")
        self.add_track("unverified.mp3", business_use_verified=False)
        (self.music_dir / "unlisted.mp3").write_bytes(b"x")
        self.use_tools()
        recommended, not_usable = self.recommend()
        self.assertEqual([item["file"] for item in recommended], ["ok.mp3"])
        self.assertEqual({item["file"] for item in not_usable}, {"unverified.mp3", "unlisted.mp3"})

    def test_ineligible_tracks_are_not_even_probed(self):
        self.add_track("unverified.mp3", business_use_verified=False)
        tools = self.use_tools()
        self.recommend()
        self.assertEqual(tools.calls, [])

    def test_unreadable_track_is_not_recommended(self):
        self.add_track()
        self.use_tools(music=probe_body([], duration=60.0))
        recommended, not_usable = self.recommend()
        self.assertEqual(recommended, [])
        self.assertIn("no audio", not_usable[0]["reason"])


class RenderRefusalTests(ReelTestCase):
    def assert_refused_without_rendering(self, pattern, track="piano.mp3", **options):
        tools = self.use_tools()
        with self.assertRaisesRegex(reel.ReelError, pattern):
            self.render(track, **options)
        self.assertEqual(tools.render_calls, [])
        self.assertFalse(self.previews.exists() and any(self.previews.iterdir()))

    def test_unverified_track(self):
        self.add_track(business_use_verified=False)
        self.assert_refused_without_rendering("can't be used")

    def test_unlisted_track(self):
        (self.music_dir / "piano.mp3").write_bytes(b"x")
        self.assert_refused_without_rendering("no entry")

    def test_unknown_track(self):
        self.assert_refused_without_rendering("isn't in the music folder")

    def test_track_path_outside_the_music_folder(self):
        self.add_track()
        (self.root / "outside.mp3").write_bytes(b"x")
        self.assert_refused_without_rendering("isn't in the music folder", track="../outside.mp3")

    def test_volumes_out_of_range(self):
        self.add_track()
        for options in (
            {"music_volume": 0},
            {"music_volume": 101},
            {"music_volume": True},
            {"music_volume": 1.5},
            {"original_volume": -1},
            {"original_volume": 101},
        ):
            with self.subTest(options=options):
                self.assert_refused_without_rendering("whole number", **options)

    def test_bad_fit(self):
        self.add_track()
        self.assert_refused_without_rendering("fit must be", fit="stretch")

    def test_video_too_short_or_too_long(self):
        self.add_track()
        for duration in (2.9, 900.1):
            with self.subTest(duration=duration):
                tools = self.use_tools(source=probe_body([video_stream()], duration=duration))
                with self.assertRaisesRegex(reel.ReelError, "3 seconds to 15 minutes"):
                    self.render()
                self.assertEqual(tools.render_calls, [])

    def test_missing_or_wrong_video_file(self):
        self.add_track()
        self.use_tools()
        self.video = self.root / "missing.mp4"
        with self.assertRaisesRegex(reel.ReelError, "No video file"):
            self.render()
        self.video = self.root / "notes.txt"
        self.video.write_text("x")
        with self.assertRaisesRegex(reel.ReelError, "isn't a supported video"):
            self.render()

    def test_video_with_only_cover_art_has_no_video_stream(self):
        self.add_track()
        cover = video_stream(disposition={"attached_pic": 1})
        tools = self.use_tools(source=probe_body([cover, audio_stream()]))
        with self.assertRaisesRegex(reel.ReelError, "no video stream"):
            self.render()
        self.assertEqual(tools.render_calls, [])


class RenderCommandTests(ReelTestCase):
    def setUp(self):
        super().setUp()
        self.add_track()

    def test_output_settings_meet_the_reel_requirements(self):
        tools = self.use_tools()
        self.render()
        command = " ".join(tools.render_calls[0])
        for expected in (
            "-c:v libx264", "-pix_fmt yuv420p", "-flags +cgop", "-c:a aac", "-ar 48000", "-ac 2",
            "-b:a 128k", "-movflags +faststart", "-use_editlist 0", "-maxrate 20M",
        ):
            self.assertIn(expected, command)

    def test_runs_without_a_shell_and_keeps_paths_as_single_arguments(self):
        self.video = self.root / "my clip; del everything.mp4"
        self.video.write_bytes(b"x")
        tools = self.use_tools()
        self.render()
        self.assertTrue(all(not keywords.get("shell") for keywords in tools.keyword_arguments))
        self.assertIn(str(self.video.resolve()), tools.render_calls[0])

    def test_inputs_are_limited_to_local_files(self):
        tools = self.use_tools()
        self.render()
        command = tools.render_calls[0]
        self.assertEqual(command.count("-protocol_whitelist"), 2)
        self.assertEqual(command.count("-i"), 2)
        for index, item in enumerate(command):
            if item == "-protocol_whitelist":
                self.assertEqual(command[index + 1], "file")

    def test_never_overwrites_and_strips_source_metadata(self):
        tools = self.use_tools()
        self.render()
        command = tools.render_calls[0]
        self.assertIn("-n", command)
        self.assertNotIn("-y", command)
        self.assertEqual(command[command.index("-map_metadata") + 1], "-1")

    def test_music_and_original_are_mixed_at_chosen_volumes(self):
        tools = self.use_tools()
        self.render(music_volume=25, original_volume=80)
        graph = self.graph(tools)
        self.assertIn("volume=0.250", graph)
        self.assertIn("volume=0.800", graph)
        self.assertIn("amix=inputs=2", graph)

    def test_video_without_sound_uses_music_only(self):
        tools = self.use_tools(source=probe_body([video_stream()]))
        report = self.render()
        graph = self.graph(tools)
        self.assertNotIn("amix", graph)
        self.assertNotIn("[0:a:0]", graph)
        self.assertIn("[a]", graph)
        self.assertIsNone(report["settings"]["original_volume"])

    def test_muted_original_uses_music_only(self):
        tools = self.use_tools()
        self.render(original_volume=0)
        self.assertNotIn("[0:a:0]", self.graph(tools))

    def test_short_track_loops_with_a_warning(self):
        tools = self.use_tools(music=probe_body([audio_stream()], duration=5.0))
        report = self.render()
        self.assertIn("-stream_loop", tools.render_calls[0])
        self.assertTrue(any("loops" in warning for warning in report["warnings"]))

    def test_long_track_does_not_loop(self):
        tools = self.use_tools()
        self.render()
        self.assertNotIn("-stream_loop", tools.render_calls[0])

    def test_music_fades_out_at_the_end_of_the_video(self):
        tools = self.use_tools(source=probe_body([video_stream(), audio_stream()], duration=20.0))
        self.render()
        command = tools.render_calls[0]
        self.assertIn("afade=t=out:st=18.500:d=1.5", self.graph(tools))
        self.assertEqual(command[command.index("-t") + 1], "20.000")

    def test_fit_modes(self):
        tools = self.use_tools()
        self.render(fit="pad")
        self.assertIn("pad=1080:1920", self.graph(tools))
        tools = self.use_tools()
        self.render(fit="crop")
        self.assertIn("crop=1080:1920", self.graph(tools))

    def test_both_fit_modes_convert_to_limited_range(self):
        for fit in ("pad", "crop"):
            with self.subTest(fit=fit):
                tools = self.use_tools()
                self.render(fit=fit)
                self.assertIn("out_range=tv", self.graph(tools))
                command = tools.render_calls[0]
                self.assertEqual(command[command.index("-color_range") + 1], "tv")

    def test_uses_the_source_video_stream_index(self):
        streams = [audio_stream(index=0), video_stream(index=1)]
        tools = self.use_tools(source=probe_body(streams))
        self.render()
        self.assertIn("[0:1]", self.graph(tools))

    def test_interlaced_source_is_deinterlaced(self):
        tools = self.use_tools(source=probe_body([video_stream(field_order="tt"), audio_stream()]))
        self.render()
        self.assertIn("yadif", self.graph(tools))

    def test_frame_rate_is_kept_when_allowed_and_clamped_otherwise(self):
        for rate, expected in (("30000/1001", 29.97), ("15/1", 23), ("120/1", 60), ("0/0", 30)):
            with self.subTest(rate=rate):
                self.assertEqual(reel.output_fps({"fps": reel.parse_fps(rate)}), expected)

    def test_hdr_and_low_resolution_sources_warn(self):
        source = probe_body([video_stream(color_transfer="arib-std-b67", width=360, height=640), audio_stream()])
        self.use_tools(source=source)
        warnings = " ".join(self.render()["warnings"])
        self.assertIn("HDR", warnings)
        self.assertIn("low resolution", warnings)


class RenderResultTests(ReelTestCase):
    def setUp(self):
        super().setUp()
        self.add_track()

    def test_preview_and_review_record_are_saved(self):
        self.use_tools()
        report = self.render()
        self.assertEqual(report["status"], reel.STATUS_AWAITING_REVIEW)
        self.assertTrue(Path(report["preview_path"]).is_file())
        saved = json.loads(Path(report["record_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], reel.STATUS_AWAITING_REVIEW)
        self.assertEqual(saved["music"]["proof"], "C:/Licences/receipt.pdf")
        self.assertEqual(len(saved["preview_sha256"]), 64)

    def test_source_files_are_not_changed(self):
        self.use_tools()
        self.render()
        self.assertEqual(self.video.read_bytes(), b"original video bytes")
        self.assertEqual((self.music_dir / "piano.mp3").read_bytes(), b"music bytes")

    def test_each_render_gets_a_new_file(self):
        self.use_tools()
        self.assertNotEqual(self.render()["preview_path"], self.render()["preview_path"])

    def test_unsafe_characters_in_the_video_name_are_not_used(self):
        self.video = self.root / "my clip & (final).mp4"
        self.video.write_bytes(b"x")
        self.use_tools()
        self.assertRegex(self.render()["preview_file"], r"^my-clip-final-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}\.mp4$")

    def test_failed_render_removes_the_partial_file_and_shows_the_reason(self):
        self.use_tools(render_code=1)
        with self.assertRaisesRegex(reel.ReelError, "ffmpeg said no"):
            self.render()
        self.assertEqual(list(self.previews.iterdir()), [])

    def test_render_timeout_removes_the_partial_file(self):
        def slow(arguments, **keywords):
            if Path(arguments[0]).stem == "ffmpeg":
                Path(arguments[-1]).write_bytes(b"partial")
                raise subprocess.TimeoutExpired(arguments, 1)
            return tools(arguments, **keywords)

        tools = FakeTools()
        with mock.patch.object(reel.subprocess, "run", slow):
            with self.assertRaisesRegex(reel.ReelError, "took too long"):
                self.render()
        self.assertEqual(list(self.previews.iterdir()), [])

    def assert_needs_attention(self, pattern, **options):
        self.use_tools(**options)
        report = self.render()
        self.assertEqual(report["status"], reel.STATUS_NEEDS_ATTENTION)
        self.assertRegex(" ".join(report["problems"]), pattern)

    def test_moov_after_media_is_caught(self):
        self.assert_needs_attention("moov", output_bytes=MOOV_LAST)

    def test_edit_list_is_caught(self):
        self.assert_needs_attention("edit list", output_bytes=WITH_EDIT_LIST)

    def test_wrong_output_format_is_caught(self):
        cases = {
            "H.264": [video_stream(codec_name="vp9"), audio_stream()],
            "4:2:0": [video_stream(pix_fmt="yuv444p"), audio_stream()],
            "progressive": [video_stream(field_order="tt"), audio_stream()],
            "FPS": [video_stream(avg_frame_rate="120/1"), audio_stream()],
            "width": [video_stream(width=2160, height=3840), audio_stream()],
            "AAC": [video_stream()],
            "48 kHz": [video_stream(), audio_stream(sample_rate="96000")],
            "mono or stereo": [video_stream(), audio_stream(channels=6)],
        }
        for pattern, streams in cases.items():
            with self.subTest(pattern=pattern):
                self.assert_needs_attention(pattern, output=probe_body(streams))

    def test_full_range_output_is_caught(self):
        audio = audio_stream()
        self.assert_needs_attention("yuvj420p", output=probe_body([video_stream(pix_fmt="yuvj420p"), audio]))
        self.assert_needs_attention("full range", output=probe_body([video_stream(color_range="pc"), audio]))

    def test_limited_range_output_passes(self):
        self.use_tools(output=probe_body([video_stream(color_range="tv"), audio_stream()]))
        self.assertEqual(self.render()["problems"], [])

    def test_container_duration_and_bitrate_are_checked(self):
        streams = [video_stream(), audio_stream()]
        self.assert_needs_attention("MP4 or MOV", output=probe_body(streams, format_name="matroska,webm"))
        self.assert_needs_attention("duration", output=probe_body(streams, duration=1.0))
        self.assert_needs_attention("25 Mbps", output=probe_body(streams, bit_rate=30_000_000))

    def test_oversized_file_is_caught(self):
        with mock.patch.object(reel, "MAX_FILE_BYTES", 10):
            self.assert_needs_attention("300 MB")

    def test_problem_exits_with_an_error_code(self):
        self.use_tools(output_bytes=MOOV_LAST)
        report = self.render()
        with mock.patch.object(reel, "render_reel", return_value=report), mock.patch.multiple(
            reel, MUSIC_DIR=self.music_dir, PREVIEW_DIR=self.previews
        ), mock.patch("builtins.print"):
            self.assertEqual(reel.main(["render", "--video", str(self.video), "--track", "piano.mp3"]), 1)


class SafetyTests(ReelTestCase):
    def test_instagram_settings_never_reach_ffmpeg(self):
        self.add_track()
        tools = self.use_tools()
        report = self.render()
        for environment in tools.environments:
            self.assertFalse([name for name in environment if name.upper().startswith("IG_")])
        saved = Path(report["record_path"]).read_text(encoding="utf-8")
        self.assertNotIn(FAKE_TOKEN, saved)
        self.assertNotIn(FAKE_TOKEN, " ".join(arg for call in tools.calls for arg in call))

    def test_tool_has_no_network_or_publishing_imports(self):
        tree = ast.parse((reel.BASE_DIR / "reel_creator.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        forbidden = {
            "socket", "http", "urllib", "requests", "httpx", "ftplib", "smtplib", "mcp",
            "instagram_mcp_server", "instagram_drafts",
        }
        self.assertEqual(imported & forbidden, set())

    def test_tool_does_not_read_instagram_settings(self):
        source = (reel.BASE_DIR / "reel_creator.py").read_text(encoding="utf-8")
        for name in ("IG_ACCESS_TOKEN", "IG_PUBLISHING_MODE", "graph.facebook.com"):
            self.assertNotIn(name, source)


class InteractiveTests(ReelTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.multiple(reel, MUSIC_DIR=self.music_dir, PREVIEW_DIR=self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)
        printer = mock.patch("builtins.print")
        self.printed = printer.start()
        self.addCleanup(printer.stop)

    def answer(self, *answers):
        patcher = mock.patch("builtins.input", side_effect=list(answers))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_person_chooses_track_and_volumes(self):
        self.add_track("a.mp3", moods=["energetic"])
        self.add_track("b.mp3", moods=["calm"])
        tools = self.use_tools()
        self.answer(f'"{self.video}"', "calm", "2", "40", "70", "crop")
        self.assertEqual(reel.main(["create"]), 0)
        command = tools.render_calls[0]
        self.assertIn(str(self.music_dir / "a.mp3"), command)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("volume=0.400", graph)
        self.assertIn("volume=0.700", graph)
        self.assertIn("crop=1080:1920", graph)

    def test_defaults_are_used_for_blank_answers(self):
        self.add_track()
        tools = self.use_tools()
        self.answer(str(self.video), "", "", "", "", "")
        self.assertEqual(reel.main(["create"]), 0)
        graph = tools.render_calls[0][tools.render_calls[0].index("-filter_complex") + 1]
        self.assertIn("volume=0.300", graph)
        self.assertIn("pad=1080:1920", graph)

    def test_invalid_numbers_are_asked_again(self):
        self.add_track()
        tools = self.use_tools()
        self.answer(str(self.video), "", "9", "1", "999", "abc", "50", "100", "pad")
        self.assertEqual(reel.main(["create"]), 0)
        self.assertIn("volume=0.500", tools.render_calls[0][tools.render_calls[0].index("-filter_complex") + 1])

    def test_no_licensed_music_stops_before_rendering(self):
        (self.music_dir / "unlisted.mp3").write_bytes(b"x")
        tools = self.use_tools()
        self.answer(str(self.video), "")
        self.assertEqual(reel.main(["create"]), 1)
        self.assertEqual(tools.render_calls, [])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg isn't installed")
class RealFfmpegTests(unittest.TestCase):
    """Runs the real commands on synthetic clips. The clips are generated, not downloaded."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.music_dir = self.root / "music"
        self.music_dir.mkdir()
        self.previews = self.root / "previews"

    def generate(self, path, *arguments):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *arguments, str(path)],
            check=True,
            capture_output=True,
        )

    def make_track(self, seconds):
        self.generate(self.music_dir / "tone.m4a", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}")
        (self.music_dir / reel.LICENSES_FILE_NAME).write_text(
            json.dumps(
                {
                    "tracks": [
                        {
                            "file": "tone.m4a",
                            "license_type": "owned",
                            "business_use_verified": True,
                            "proof": "Generated by this test",
                        }
                    ]
                }
            )
        )

    def make_video(self, size="1280x720", with_audio=True):
        path = self.root / "clip.mp4"
        arguments = ["-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=4"]
        if with_audio:
            arguments += ["-f", "lavfi", "-i", "sine=frequency=220:duration=4"]
        self.generate(path, *arguments, "-pix_fmt", "yuv420p")
        return path

    def assert_renders(self, video, **options):
        report = reel.render_reel(video, "tone.m4a", music_dir=self.music_dir, output_dir=self.previews, **options)
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["status"], reel.STATUS_AWAITING_REVIEW)
        info = reel.probe_media(report["preview_path"], shutil.which("ffprobe"))
        self.assertEqual((info["width"], info["height"]), (1080, 1920))
        self.assertAlmostEqual(info["duration"], 4.0, delta=0.25)
        self.assertEqual(info["audio_sample_rate"], 48000)
        return report

    def test_landscape_video_with_sound_is_padded_and_mixed(self):
        self.make_track(10)
        self.assert_renders(self.make_video())

    def test_full_range_source_becomes_limited_range(self):
        self.make_track(10)
        path = self.root / "full-range.mp4"
        self.generate(
            path, "-f", "lavfi", "-i", "color=white:size=1280x720:rate=30:duration=4",
            "-vf", "scale=out_range=pc,format=yuvj420p", "-color_range", "pc",
        )
        ffprobe = shutil.which("ffprobe")
        self.assertEqual(reel.probe_media(path, ffprobe)["color_range"], "pc")
        self.assertGreaterEqual(self.brightest_luma(path), 250)
        for fit in ("pad", "crop"):
            with self.subTest(fit=fit):
                report = self.assert_renders(path, fit=fit)
                info = reel.probe_media(report["preview_path"], ffprobe)
                self.assertEqual(info["pix_fmt"], "yuv420p")
                self.assertNotEqual(info["color_range"], "pc")
                # Measures the pixels, because a label alone could be wrong. White is
                # 255 in full range and 235 in limited range.
                self.assertAlmostEqual(self.brightest_luma(report["preview_path"]), 235, delta=3)

    def brightest_luma(self, path):
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-frames:v", "1",
                "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YMAX:file=-",
                "-f", "null", "-",
            ],
            check=True, capture_output=True, text=True,
        )
        return int(float(result.stdout.rsplit("YMAX=", 1)[1].split()[0]))

    def test_crop_with_a_short_looping_track_and_no_source_sound(self):
        self.make_track(1)
        report = self.assert_renders(self.make_video(with_audio=False), fit="crop")
        self.assertTrue(any("loops" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
