"""Offline tests for the one-click Reel launcher.

Nothing here can reach Meta. The tests use a fake token and temporary folders. The
end-to-end tests start the real server over stdio with its Meta addresses replaced by a
temporary server on 127.0.0.1. Run with:

    .\\.venv\\Scripts\\python.exe -m unittest test_reel_launcher -v
"""

import ast
import contextlib
import getpass
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

import instagram_drafts as drafts
import reel_creator as reel
import reel_launcher as launcher

FAKE_TOKEN = "FAKE-TOKEN-launcher-123"
NAME = "clip-20261104-120000-abcd.mp4"
VIDEO = b"pretend this is an mp4"
ROOT = Path(launcher.__file__).resolve().parent
BAT = ROOT / "Shambhala Reel Creator.bat"

WRAPPER = """
import sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
import instagram_drafts as drafts
import instagram_mcp_server as server
drafts.DRAFTS_DIR = Path(sys.argv[2])
drafts.PREVIEWS_DIR = Path(sys.argv[3])
server.GRAPH_API_ROOT = sys.argv[4] + "/graph"
server.RUPLOAD_API_ROOT = sys.argv[4] + "/rupload"
server.mcp.run("stdio")
"""


def answers(*values):
    """Script input(). A leftover or missing answer fails the test."""
    return mock.patch("builtins.input", side_effect=list(values))


class FakeMeta:
    """A temporary server on 127.0.0.1 that answers as Meta's documentation describes."""

    def __init__(self, publish_status=200):
        self.requests = []
        meta = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                meta.requests.append(("GET", self.path, self.headers.get("Authorization"), b""))
                if "content_publishing_limit" in self.path:
                    self.reply(200, {"data": [{"quota_usage": 1, "config": {"quota_total": 100}}]})
                elif "status_code" in self.path:
                    self.reply(200, {"status_code": "FINISHED"})
                else:
                    self.reply(200, {"data": [{"id": "999", "permalink": "https://www.instagram.com/reel/TEST/"}]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                meta.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path.startswith("/rupload"):
                    self.reply(200, {"success": True, "message": "Upload successful."})
                elif self.path.endswith("/media_publish"):
                    if publish_status != 200:
                        self.reply(publish_status, {"error": {"code": 2, "message": f"bad {FAKE_TOKEN}"}})
                    else:
                        self.reply(200, {"id": "999"})
                else:
                    self.reply(200, {"id": "555", "uri": "https://attacker.example/x"})

            def log_message(self, *arguments):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self):
        return [path.split("?")[0] for _, path, _, _ in self.requests]


class BatchFileTests(unittest.TestCase):
    def setUp(self):
        self.text = BAT.read_text(encoding="utf-8")

    def test_uses_windows_line_endings(self):
        data = BAT.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))

    def test_clears_publishing_settings_and_runs_the_project_python(self):
        self.assertIn('set "IG_PUBLISHING_MODE="', self.text)
        self.assertIn('set "IG_ACCESS_TOKEN="', self.text)
        self.assertIn('".venv\\Scripts\\python.exe" reel_launcher.py', self.text)
        self.assertIn('cd /d "%~dp0"', self.text)

    def test_never_stores_settings_or_turns_on_live_mode(self):
        lowered = self.text.lower()
        for forbidden in ("setx", "live", "reg add", "token=e", "http"):
            self.assertNotIn(forbidden, lowered)

    def _run_copy(self, folder):
        """Run a copy of the launcher file from folder, as a double-click would."""
        shutil.copy(BAT, folder / BAT.name)
        return subprocess.run(
            f'cmd.exe /d /c ""{folder / BAT.name}""',
            cwd=os.environ["SYSTEMROOT"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_missing_python_is_reported_and_the_window_waits(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self._run_copy(Path(folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python environment is missing", result.stdout)
        self.assertIn("Press any key", result.stdout)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_a_python_crash_stays_visible_and_keeps_its_exit_code(self):
        venv = ROOT / ".venv"
        if not (venv / "Scripts" / "python.exe").exists():
            self.skipTest("No project Python to copy")
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / ".venv" / "Scripts").mkdir(parents=True)
            shutil.copy(venv / "pyvenv.cfg", folder / ".venv")
            shutil.copy(venv / "Scripts" / "python.exe", folder / ".venv" / "Scripts")
            (folder / "reel_launcher.py").write_text(
                "import sys\nprint('crash detail', file=sys.stderr)\nsys.exit(7)\n", encoding="utf-8"
            )
            result = self._run_copy(folder)
        self.assertEqual(result.returncode, 7)
        self.assertIn("crash detail", result.stderr)
        self.assertIn("stopped with code 7", result.stdout)
        self.assertIn("Press any key", result.stdout)


class SourceSafetyTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "reel_launcher.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_has_no_instagram_network_code_of_its_own(self):
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        forbidden = {"socket", "http", "urllib", "requests", "httpx", "instagram_mcp_server"}
        self.assertEqual(imported & forbidden, set())
        for text in ("graph.facebook.com", "rupload", "media_publish", "graph_request"):
            self.assertNotIn(text, self.source)

    def test_never_works_out_or_supplies_the_phrase(self):
        for text in ("confirmation_phrase", "upload_test_phrase", "type exactly", "PUBLISH ", "[:8]"):
            self.assertNotIn(text, self.source)

    def test_never_reads_the_token_from_the_environment(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Subscript) and "environ" in ast.unparse(node.value):
                self.assertEqual(ast.unparse(node.slice), "'PATH'")
            if isinstance(node, ast.Call) and "environ.get" in ast.unparse(node.func):
                self.assertEqual(ast.unparse(node.args[0]), "'PATH'")

    def test_only_opens_the_null_device_for_writing(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "open":
                self.assertEqual(ast.unparse(node.args[0]), "os.devnull")
        for text in ("write_text", "write_bytes", "setx", "json.dump("):
            self.assertNotIn(text, self.source)

    def test_only_calls_the_expected_server_tools(self):
        self.assertEqual(
            set(re.findall(r"\"([a-z_]+_instagram_[a-z_]+|get_recent_instagram_posts)\"", self.source)),
            {"prepare_instagram_local_reel", "publish_instagram_draft", "get_recent_instagram_posts"},
        )
        self.assertNotIn("test_instagram_local_upload", self.source)


class ServerEnvironmentTests(unittest.TestCase):
    def test_publishing_is_off_without_a_token_whatever_your_environment_says(self):
        with mock.patch.dict(os.environ, {"IG_PUBLISHING_MODE": "live", "IG_ACCESS_TOKEN": FAKE_TOKEN}):
            env = launcher.build_server_env()
        self.assertNotIn("IG_PUBLISHING_MODE", env)
        self.assertNotIn("IG_ACCESS_TOKEN", env)

    def test_live_mode_only_comes_with_a_token_entered_in_this_run(self):
        env = launcher.build_server_env("entered-now")
        self.assertEqual(env["IG_PUBLISHING_MODE"], "live")
        self.assertEqual(env["IG_ACCESS_TOKEN"], "entered-now")

    def test_token_and_mode_are_not_added_to_this_process(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("IG_PUBLISHING_MODE", None)
            os.environ.pop("IG_ACCESS_TOKEN", None)
            launcher.build_server_env(FAKE_TOKEN)
            self.assertNotIn("IG_PUBLISHING_MODE", os.environ)
            self.assertNotIn("IG_ACCESS_TOKEN", os.environ)

    def test_sdk_does_not_pass_on_instagram_settings_by_default(self):
        from mcp.client.stdio import get_default_environment

        with mock.patch.dict(os.environ, {"IG_PUBLISHING_MODE": "live", "IG_ACCESS_TOKEN": FAKE_TOKEN}):
            env = get_default_environment()
        self.assertNotIn("IG_PUBLISHING_MODE", env)
        self.assertNotIn("IG_ACCESS_TOKEN", env)


class TokenPromptTests(unittest.TestCase):
    def read(self, value):
        with mock.patch.object(getpass, "getpass", return_value=value), contextlib.redirect_stdout(io.StringIO()) as out:
            return launcher.read_access_token(), out.getvalue()

    def test_returns_the_token_without_printing_it(self):
        token, printed = self.read(f"  {FAKE_TOKEN}\n")
        self.assertEqual(token, FAKE_TOKEN)
        self.assertNotIn(FAKE_TOKEN, printed)

    def test_bad_tokens_stop_without_being_shown(self):
        for value in ("", "   ", "abc def", "abc\ndef", "abc\x16def", "abc•def"):
            with self.subTest(value=value), self.assertRaises(launcher.StopLauncher) as caught:
                self.read(value)
            if value.strip():
                self.assertNotIn(value, str(caught.exception))

    def test_window_that_can_not_hide_input_reads_nothing(self):
        def warn_like_python(prompt):
            import warnings

            warnings.warn("Can not control echo", getpass.GetPassWarning)
            return FAKE_TOKEN

        with mock.patch.object(getpass, "getpass", warn_like_python), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(launcher.StopLauncher, "can't hide input"):
                launcher.read_access_token()


class CaptionTests(unittest.TestCase):
    def test_every_suggestion_passes_the_draft_rules(self):
        topics = ("", None, "A new website launch", "x" * 5000, "😀 emoji launch", "Line\nbreak\ttopic\x00")
        for topic in topics:
            for caption in launcher.suggest_captions(topic):
                with self.subTest(topic=topic):
                    self.assertEqual(drafts.validate_caption(caption), ([], []))
                    self.assertIn("#ShambhalaTech", caption)

    def test_default_topic_is_used_when_blank(self):
        self.assertIn(launcher.DEFAULT_TOPIC, launcher.suggest_captions("  ")[0])

    def test_topic_can_not_add_mentions_or_hashtags(self):
        caption = launcher.suggest_captions("Launch @someone #spam #spam2")[0]
        text, tags = caption.split("\n\n")
        self.assertNotIn("@", caption)
        self.assertNotIn("#", text)
        self.assertLessEqual(len(tags.split()), 6)

    def test_hashtags_come_from_the_topic_without_repeats(self):
        tags = launcher.suggest_hashtags("Website launch for website owners").split()
        self.assertEqual(tags[0], "#ShambhalaTech")
        self.assertIn("#Website", tags)
        self.assertIn("#Launch", tags)
        self.assertEqual(len({tag.lower() for tag in tags}), len(tags))
        self.assertLessEqual(len(tags), 6)

    def test_person_chooses_a_suggestion(self):
        with answers("Launch day", "2"), contextlib.redirect_stdout(io.StringIO()):
            self.assertIn("DM us", launcher.choose_caption())

    def test_person_can_write_their_own(self):
        with answers("", "4", "My own words #Mine"), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(launcher.choose_caption(), "My own words #Mine")

    def test_own_caption_over_the_limit_is_asked_again(self):
        with answers("", "4", "a" * 2201, "1"), contextlib.redirect_stdout(io.StringIO()) as out:
            caption = launcher.choose_caption()
        self.assertIn(launcher.DEFAULT_TOPIC, caption)
        self.assertIn("2201 characters", out.getvalue())


class VideoSelectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.video = Path(folder.name) / "clip.mp4"
        self.video.write_bytes(b"x")

    def fake_tkinter(self, chosen):
        dialog = types.SimpleNamespace(askopenfilename=mock.Mock(return_value=chosen))
        module = types.SimpleNamespace(Tk=mock.Mock(), filedialog=dialog)
        return mock.patch.dict(sys.modules, {"tkinter": module, "tkinter.filedialog": dialog}), dialog

    def test_file_window_result_is_used(self):
        patcher, dialog = self.fake_tkinter(str(self.video))
        with patcher:
            self.assertEqual(launcher.choose_video(), self.video.resolve())
        self.assertIn(".mp4", dialog.askopenfilename.call_args.kwargs["filetypes"][0][1])

    def test_cancelling_the_window_stops(self):
        patcher, _ = self.fake_tkinter("")
        with patcher, self.assertRaisesRegex(launcher.StopLauncher, "No video"):
            launcher.choose_video()

    def test_wrong_file_type_is_refused(self):
        other = self.video.with_suffix(".txt")
        other.write_text("x")
        patcher, _ = self.fake_tkinter(str(other))
        with patcher, self.assertRaisesRegex(reel.ReelError, "isn't a supported video"):
            launcher.choose_video()

    def test_falls_back_to_a_typed_path(self):
        broken = types.SimpleNamespace(Tk=mock.Mock(side_effect=RuntimeError()), filedialog=mock.Mock())
        with mock.patch.dict(sys.modules, {"tkinter": broken, "tkinter.filedialog": broken.filedialog}):
            with answers(f'"{self.video}"'), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(launcher.choose_video(), self.video.resolve())


class RenderReviewTests(unittest.TestCase):
    INFO = {"duration": 20.0, "has_audio": True}

    def report(self, problems=()):
        return {
            "preview_path": f"C:/previews/{NAME}",
            "record_path": "C:/previews/x.json",
            "status": "needs_attention" if problems else "awaiting_review",
            "problems": list(problems),
            "warnings": [],
        }

    def run_review(self, *typed, report=None, tracks=None):
        tracks = tracks or [{"file": "a.mp3", "title": "A", "artist": "", "license_type": "owned", "reasons": []}]
        with (
            mock.patch.object(reel, "recommend_tracks", return_value=(tracks, [])),
            mock.patch.object(reel, "render_reel", return_value=report or self.report()) as render,
            mock.patch.object(launcher, "open_preview") as preview,
            answers(*typed),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            try:
                return launcher.render_and_review("clip.mp4", self.INFO, "ffprobe"), render, preview
            except launcher.StopLauncher as error:
                return error, render, preview

    def test_accepting_the_preview_returns_it(self):
        result, render, preview = self.run_review("", "1", "25", "80", "crop", "1")
        self.assertEqual(result["status"], "awaiting_review")
        render.assert_called_once_with("clip.mp4", "a.mp3", 25, 80, "crop")
        preview.assert_called_once()

    def test_stopping_after_the_preview_stops(self):
        result, _, _ = self.run_review("", "", "", "", "", "3")
        self.assertIsInstance(result, launcher.StopLauncher)
        self.assertIn(NAME, str(result))

    def test_pressing_enter_at_review_stops_rather_than_accepts(self):
        result, _, _ = self.run_review("", "", "", "", "", "")
        self.assertIsInstance(result, launcher.StopLauncher)

    def test_rendering_again_uses_new_settings(self):
        result, render, _ = self.run_review("", "", "30", "100", "", "2", "n", "15", "100", "", "1")
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual([call.args[2] for call in render.call_args_list], [30, 15])

    def test_failed_checks_stop_before_the_preview_is_offered(self):
        result, _, preview = self.run_review("", "", "", "", "", report=self.report(["The video isn't H.264."]))
        self.assertIsInstance(result, launcher.StopLauncher)
        self.assertIn("didn't pass", str(result))
        preview.assert_not_called()

    def test_no_approved_music_stops_before_rendering(self):
        with (
            mock.patch.object(reel, "recommend_tracks", return_value=([], [])),
            mock.patch.object(reel, "render_reel") as render,
            answers(""),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(launcher.StopLauncher, "no approved music"),
        ):
            launcher.render_and_review("clip.mp4", self.INFO, "ffprobe")
        render.assert_not_called()

    def test_only_tracks_with_complete_licence_records_are_offered(self):
        with tempfile.TemporaryDirectory() as folder:
            music = Path(folder)
            (music / "unlisted.mp3").write_bytes(b"x")
            with (
                mock.patch.object(reel, "MUSIC_DIR", music),
                mock.patch.object(reel.shutil, "which", return_value="ffprobe"),
                answers(""),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(launcher.StopLauncher, "no approved music"),
            ):
                launcher.choose_track(20.0, "ffprobe")


class PublishingChoiceTests(unittest.TestCase):
    DRAFT = {
        "draft_id": "a" * 16,
        "content_hash": "b" * 64,
        "caption": "Hello",
        "local_file": {"name": NAME, "size": 22},
    }

    def offer(self, *typed):
        with (
            mock.patch.object(launcher, "read_access_token", return_value=FAKE_TOKEN) as token,
            mock.patch.object(launcher, "publish_draft", return_value=True) as publish,
            answers(*typed),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            result = launcher.offer_publishing(self.DRAFT)
        return result, token, publish, out.getvalue()

    def test_default_answer_keeps_publishing_off_and_asks_for_no_token(self):
        for typed in ("", "n", "no", "maybe", "publish"):
            with self.subTest(typed=typed):
                result, token, publish, printed = self.offer(typed)
                self.assertFalse(result)
                token.assert_not_called()
                publish.assert_not_called()
                self.assertIn("no token was requested", printed)

    def test_only_an_explicit_yes_asks_for_the_token(self):
        result, token, publish, _ = self.offer("yes")
        self.assertTrue(result)
        token.assert_called_once()
        publish.assert_called_once_with(self.DRAFT, FAKE_TOKEN)

    def test_flow_stops_at_each_stage_without_publishing(self):
        video = Path("clip.mp4")
        info = {"has_video": True, "duration": 10.0, "has_audio": False}
        with (
            mock.patch.object(reel, "find_tools", return_value=("ffmpeg", "ffprobe")),
            mock.patch.object(launcher, "choose_video", return_value=video),
            mock.patch.object(reel, "probe_media", return_value=info),
            mock.patch.object(
                launcher, "render_and_review", side_effect=launcher.StopLauncher("Stopped.")
            ),
            mock.patch.object(launcher, "prepare_draft") as prepare,
            mock.patch.object(launcher, "publish_draft") as publish,
            mock.patch.object(reel, "MUSIC_DIR", Path(tempfile.gettempdir())),
            mock.patch.object(reel, "PREVIEW_DIR", Path(tempfile.gettempdir())),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(launcher.main([]), 1)
        prepare.assert_not_called()
        publish.assert_not_called()

    def test_missing_ffmpeg_explains_installation_and_stops(self):
        with (
            mock.patch.object(reel.shutil, "which", return_value=None),
            mock.patch.object(launcher, "refresh_path"),
            mock.patch.object(launcher, "choose_video") as choose,
            mock.patch.object(reel, "MUSIC_DIR", Path(tempfile.gettempdir())),
            mock.patch.object(reel, "PREVIEW_DIR", Path(tempfile.gettempdir())),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(launcher.main([]), 1)
        choose.assert_not_called()
        self.assertIn("winget install", out.getvalue())

    def test_unexpected_error_shows_only_its_type(self):
        with (
            mock.patch.object(launcher, "refresh_path"),
            mock.patch.object(launcher, "run", side_effect=RuntimeError(f"secret {FAKE_TOKEN}")),
            mock.patch.object(reel, "MUSIC_DIR", Path(tempfile.gettempdir())),
            mock.patch.object(reel, "PREVIEW_DIR", Path(tempfile.gettempdir())),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(launcher.main([]), 1)
        self.assertIn("RuntimeError", out.getvalue())
        self.assertNotIn(FAKE_TOKEN, out.getvalue())

    def test_failed_attempt_is_not_retried_and_warns_about_duplicates(self):
        async def fail(draft, token):
            return True, f"Refused {token}", None

        with mock.patch.object(launcher, "_publish", side_effect=fail) as attempt, contextlib.redirect_stdout(
            io.StringIO()
        ) as out:
            self.assertFalse(launcher.publish_draft(self.DRAFT, FAKE_TOKEN))
        self.assertEqual(attempt.call_count, 1)
        self.assertNotIn(FAKE_TOKEN, out.getvalue())
        self.assertIn("[REDACTED]", out.getvalue())
        self.assertIn("post it twice", out.getvalue())


class ConfirmationRequestTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, typed):
        params = types.SimpleNamespace(message="SERVER MESSAGE\ntype this")
        with answers(typed), contextlib.redirect_stdout(io.StringIO()) as out:
            return await launcher.show_confirmation_request(None, params), out.getvalue()

    async def test_shows_the_servers_message_exactly(self):
        _, printed = await self.ask("")
        self.assertIn("SERVER MESSAGE\ntype this", printed)

    async def test_blank_answer_declines(self):
        for typed in ("", "   "):
            result, _ = await self.ask(typed)
            self.assertEqual(result.action, "decline")

    async def test_only_what_was_typed_is_sent(self):
        result, _ = await self.ask("what I typed")
        self.assertEqual(result.action, "accept")
        self.assertEqual(result.content, {"confirmation": "what I typed"})


class EndToEndTestCase(unittest.TestCase):
    """Runs the real launcher functions and real server over stdio, with no route to Meta."""

    PUBLISH_STATUS = 200

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.drafts_dir = self.root / "drafts"
        self.previews = self.root / "previews"
        self.drafts_dir.mkdir()
        self.previews.mkdir()
        (self.previews / NAME).write_bytes(VIDEO)
        (self.previews / NAME).with_suffix(".json").write_text(
            json.dumps(
                {
                    "status": "awaiting_review",
                    "preview_file": NAME,
                    "preview_sha256": hashlib.sha256(VIDEO).hexdigest(),
                    "problems": [],
                }
            )
        )
        wrapper = self.root / "wrapper.py"
        wrapper.write_text(WRAPPER)
        self.meta = FakeMeta(self.PUBLISH_STATUS)
        self.addCleanup(self.meta.close)
        arguments = [str(wrapper), str(ROOT), str(self.drafts_dir), str(self.previews), self.meta.root]
        patcher = mock.patch.object(launcher, "SERVER_ARGS", arguments)
        patcher.start()
        self.addCleanup(patcher.stop)

    def publish(self, draft, *typed):
        """Run one attempt. A typed value of None means the correct phrase from the dialog."""
        self.dialogs = []
        output = io.StringIO()

        def answer(prompt=""):
            text = output.getvalue()
            self.dialogs.append(text)
            value = typed[len(self.dialogs) - 1]
            if value is None:
                return re.findall(r"type exactly: (.+)", text)[-1]
            return value

        with mock.patch("builtins.input", answer), contextlib.redirect_stdout(output):
            result = launcher.publish_draft(draft, FAKE_TOKEN)
        self.output = output.getvalue()
        return result


class EndToEndTests(EndToEndTestCase):
    def test_prepare_runs_with_publishing_off_and_sends_nothing(self):
        draft = launcher.prepare_draft(NAME, "Hello #ShambhalaTech")
        self.assertTrue(draft["valid"])
        self.assertEqual(draft["local_file"]["sha256"], hashlib.sha256(VIDEO).hexdigest())
        self.assertEqual(self.meta.requests, [])

    def test_prepare_refuses_a_file_outside_the_previews_folder(self):
        with self.assertRaisesRegex(launcher.StopLauncher, "reels/previews"):
            launcher.prepare_draft("../secret.mp4", "Hello")
        self.assertEqual(self.meta.requests, [])

    def test_prepare_refuses_a_preview_that_changed(self):
        (self.previews / NAME).write_bytes(b"changed")
        with self.assertRaisesRegex(launcher.StopLauncher, "doesn't match"):
            launcher.prepare_draft(NAME, "Hello")

    def test_right_phrase_goes_through_the_servers_full_flow(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        self.assertTrue(self.publish(draft, None))
        self.assertEqual(
            self.meta.paths(),
            [
                f"/graph/{'1001'}/content_publishing_limit",
                "/graph/1001/media",
                "/rupload/555",
                "/graph/555",
                "/graph/1001/media_publish",
                "/graph/1001/media",
            ],
        )
        self.assertEqual(self.meta.requests[2][3], VIDEO)
        self.assertEqual(self.meta.requests[2][2], f"OAuth {FAKE_TOKEN}")
        self.assertIn("PUBLISH TO INSTAGRAM? This will be public.", self.dialogs[0])
        self.assertIn(NAME, self.dialogs[0])
        self.assertIn("Post ID: 999", self.output)
        self.assertIn("https://www.instagram.com/reel/TEST/", self.output)
        self.assertNotIn(FAKE_TOKEN, self.output)
        saved = json.loads((self.drafts_dir / f"{draft['draft_id']}.json").read_text("utf-8"))
        self.assertEqual(saved["status"], drafts.STATUS_PUBLISHED)
        self.assertNotIn(FAKE_TOKEN, json.dumps(saved))

    def test_wrong_phrase_sends_nothing(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        self.assertFalse(self.publish(draft, "PUBLISH 00000000"))
        self.assertEqual(self.meta.requests, [])
        self.assertIn("didn't match", self.output)

    def test_pressing_enter_declines_and_sends_nothing(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        self.assertFalse(self.publish(draft, ""))
        self.assertEqual(self.meta.requests, [])
        self.assertIn("ended with: decline", self.output)
        self.assertEqual(len(self.dialogs), 1)

    def test_changed_file_stops_before_any_dialog(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        (self.previews / NAME).write_bytes(b"changed")
        self.assertFalse(self.publish(draft))
        self.assertEqual(self.dialogs, [])
        self.assertEqual(self.meta.requests, [])

    def test_publish_tool_refuses_with_publishing_off(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        with mock.patch.object(launcher, "build_server_env", lambda token=None: {"PYTHONUTF8": "1"}):
            self.assertFalse(self.publish(draft))
        self.assertIn("Publishing is off", self.output)
        self.assertEqual(self.meta.requests, [])

    def test_self_check_reports_publishing_off(self):
        tools, refused, text = launcher.asyncio.run(launcher._list_server_tools())
        self.assertIn("publish_instagram_draft", tools)
        self.assertTrue(refused)
        self.assertIn("Publishing is off", text)
        self.assertEqual(self.meta.requests, [])


class UncertainPublishTests(EndToEndTestCase):
    PUBLISH_STATUS = 500

    def test_uncertain_result_is_not_retried_and_warns_about_duplicates(self):
        draft = launcher.prepare_draft(NAME, "Hello")
        self.assertFalse(self.publish(draft, None))
        self.assertEqual(self.meta.paths().count("/graph/1001/media_publish"), 1)
        self.assertIn("may or may not be live", self.output)
        self.assertIn("post it twice", self.output)
        self.assertNotIn(FAKE_TOKEN, self.output)
        saved = json.loads((self.drafts_dir / f"{draft['draft_id']}.json").read_text("utf-8"))
        self.assertEqual(saved["status"], drafts.STATUS_PUBLISH_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
