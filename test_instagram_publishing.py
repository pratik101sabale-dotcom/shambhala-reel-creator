"""Offline tests for the Instagram draft and publishing workflow.

Nothing here can reach Meta or publish. Each test uses a fake token and a temporary
drafts folder, HTTP connections are made to fail, and every API response is a mock.
A fake MCP client answers the confirmation dialog. Run with:

    .\\.venv\\Scripts\\python.exe -m unittest -v
"""

import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import instagram_drafts as drafts
import instagram_mcp_server as server
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult

FAKE_TOKEN = "FAKE-TOKEN-abc123"
ACCOUNT = server.INSTAGRAM_ACCOUNT_ID
IMAGE_URL = "https://cdn.example.com/photos/retreat.jpg"
REEL_URL = "https://cdn.example.com/videos/retreat.mp4"
QUOTA_OK = {"data": [{"quota_usage": 1, "config": {"quota_total": 100, "quota_duration": 86400}}]}
FINISHED = {"status_code": "FINISHED", "id": "555"}
CONTAINER = {"id": "555"}
PUBLISHED = {"id": "999"}
PROTOCOL_MODERN = "auto"
PROTOCOL_LEGACY = "legacy"


def graph_error(status, code, subcode=None, message="refused", **extra):
    details = {"type": "OAuthException", "code": code, "message": message, **extra}
    if subcode is not None:
        details["error_subcode"] = subcode
    body = json.dumps({"error": details}).encode()
    return urllib.error.HTTPError("https://example.invalid", status, "error", {}, io.BytesIO(body))


class FakeResponse(io.BytesIO):
    """A response body that also works as a context manager, like urllib's."""


class FakeOpener:
    """Returns or raises the queued outcomes in order, and records each request."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if not self.outcomes:
            raise AssertionError(f"Unexpected {request.get_method()} {request.full_url}")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())

    def calls(self):
        """Return each request as (method, path after the API root)."""
        prefix = f"{server.GRAPH_API_ROOT}/"
        return [(r.get_method(), r.full_url.removeprefix(prefix)) for r in self.requests]

    def publish_calls(self):
        return [c for c in self.calls() if c[0] == "POST" and c[1].endswith("/media_publish")]


def refuse_connection(self):
    raise AssertionError("A test tried to open a real network connection.")


class WorkflowTestCase(unittest.IsolatedAsyncioTestCase):
    MODE = server.MODE_LIVE

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.drafts_dir = Path(temporary.name)
        environment = {server.TOKEN_ENV_VAR: FAKE_TOKEN, server.PUBLISHING_MODE_ENV_VAR: self.MODE}
        for patcher in (
            mock.patch.dict(os.environ, environment),
            mock.patch.object(drafts, "DRAFTS_DIR", self.drafts_dir),
            mock.patch.object(http.client.HTTPConnection, "connect", refuse_connection),
            mock.patch.object(http.client.HTTPSConnection, "connect", refuse_connection),
            mock.patch.object(server.time, "sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dialogs = []
        self.opener = self.use_outcomes()

    def use_outcomes(self, *outcomes):
        opener = FakeOpener(outcomes)
        for outcome in outcomes:
            if isinstance(outcome, urllib.error.HTTPError):
                self.addCleanup(outcome.close)
        patcher = mock.patch.object(server.urllib.request, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opener = opener
        return opener

    def person_types(self, text=None, action="accept"):
        """Return a fake client dialog. With text=None the person types the right phrase."""

        async def answer(context, params):
            self.dialogs.append(params.message)
            if action != "accept":
                return ElicitResult(action=action)
            typed = text
            if typed is None:
                typed = params.message.split("type exactly: ")[1].split("\n")[0]
            return ElicitResult(action="accept", content={"confirmation": typed})

        return answer

    async def prepare(self, **overrides):
        arguments = {"post_type": "post", "media_url": IMAGE_URL, "caption": "Morning practice"}
        arguments.update(overrides)
        result = await server.mcp.call_tool("prepare_instagram_draft", arguments)
        return json.loads(result.content[0].text)

    async def publish(self, draft, dialog=None, mode=PROTOCOL_MODERN):
        """Call the tool through a real in-process MCP session, as a client would."""
        dialog = dialog or self.person_types()
        async with Client(server.mcp, elicitation_callback=dialog, mode=mode) as client:
            return await client.call_tool(
                "publish_instagram_draft",
                {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]},
            )

    def stored(self, draft):
        return json.loads((self.drafts_dir / f"{draft['draft_id']}.json").read_text("utf-8"))

    def assert_refused(self, result, pattern):
        self.assertTrue(result.is_error, result.content[0].text)
        self.assertRegex(result.content[0].text, pattern)

    def assert_no_network(self):
        self.assertEqual(self.opener.calls(), [])


class CaptionValidationTests(unittest.TestCase):
    def test_limits_are_inclusive(self):
        at_limit = " ".join(f"#t{i}" for i in range(30)) + " " + " ".join(f"@u{i}" for i in range(20))
        self.assertEqual(drafts.validate_caption(at_limit), ([], []))
        self.assertEqual(drafts.validate_caption("a" * 2200), ([], []))

    def test_over_limit_captions_are_errors(self):
        cases = {
            "a" * 2201: "2201 characters",
            " ".join(f"#t{i}" for i in range(31)): "31 hashtags",
            " ".join(f"@u{i}" for i in range(21)): "21 @mentions",
            "bad\x00text": "control",
        }
        for caption, expected in cases.items():
            with self.subTest(expected=expected):
                errors, _ = drafts.validate_caption(caption)
                self.assertEqual(len(errors), 1)
                self.assertIn(expected, errors[0])

    def test_emoji_use_the_cautious_length(self):
        self.assertEqual(drafts.caption_length("😀"), 2)
        self.assertTrue(drafts.validate_caption("😀" * 1101)[0])

    def test_email_addresses_and_url_fragments_are_not_counted(self):
        stats = drafts.caption_stats("Write to hello@example.com or see a.com/x#top #real @real")
        self.assertEqual((stats["hashtags"], stats["mentions"]), (1, 1))

    def test_empty_caption_is_only_a_warning(self):
        errors, warnings = drafts.validate_caption("  ")
        self.assertEqual(errors, [])
        self.assertIn("empty", warnings[0])

    def test_newlines_are_allowed(self):
        self.assertEqual(drafts.validate_caption("one\ntwo\r\n\tthree"), ([], []))


class MediaUrlValidationTests(unittest.TestCase):
    def test_valid_urls(self):
        for post_type, url in (
            ("post", "https://cdn.example.com/a.JPG"),
            ("post", "https://cdn.example.com/a%20b.jpeg?sig=abc&exp=1"),
            ("reel", "https://cdn.example.com/a.mp4"),
            ("reel", "https://cdn.example.com:8443/a.MOV"),
        ):
            with self.subTest(url=url):
                self.assertEqual(drafts.validate_media_url(url, post_type), ([], []))

    def test_invalid_urls(self):
        cases = {
            "": "required",
            "http://cdn.example.com/a.jpg": "https",
            "ftp://cdn.example.com/a.jpg": "https",
            "cdn.example.com/a.jpg": "https",
            "https://cdn.example.com/a b.jpg": "percent-encoded",
            "https://cdn.example.com/a%zz.jpg": "percent",
            "https://cdn.example.com/é.jpg": "percent-encoded",
            "https://user:pw@cdn.example.com/a.jpg": "username",
            "https://localhost/a.jpg": "public host",
            "https://printer/a.jpg": "public host",
            "https://media.internal/a.jpg": "public host",
            "https://127.0.0.1/a.jpg": "private or local",
            "https://192.168.1.5/a.jpg": "private or local",
            "https://[::1]/a.jpg": "private or local",
            "https://[bad/a.jpg": "malformed",
            "https://cdn.example.com:99999/a.jpg": "malformed",
            "https:///a.jpg": "no host",
            "https://cdn.example.com/a.jpg#top": "fragment",
            "https://cdn.example.com/a.jpg?access_token=x": "secret",
            "https://cdn.example.com/a.png": "not .png",
            "https://cdn.example.com/a.mp4": "not .mp4",
            "https://cdn.example.com/" + "a" * 2048 + ".jpg": "longer than",
        }
        for url, expected in cases.items():
            with self.subTest(url=url[:60]):
                errors, _ = drafts.validate_media_url(url, "post")
                self.assertTrue(errors, "expected an error")
                self.assertIn(expected, " ".join(errors))

    def test_image_is_not_a_valid_reel(self):
        errors, _ = drafts.validate_media_url(IMAGE_URL, "reel")
        self.assertIn("not .jpg", errors[0])

    def test_unknown_file_type_is_only_a_warning(self):
        errors, warnings = drafts.validate_media_url("https://cdn.example.com/media/42", "post")
        self.assertEqual(errors, [])
        self.assertIn("doesn't show a file type", warnings[0])


class PrepareToolTests(WorkflowTestCase):
    async def test_valid_post_is_saved_without_network_or_a_dialog(self):
        data = await self.prepare()
        self.assertTrue(data["valid"])
        self.assertEqual(data["status"], drafts.STATUS_PREPARED)
        self.assertEqual(data["publishing_mode"], "live")
        self.assertIsNone(data["share_to_feed"])
        self.assertIn("8 MB", " ".join(data["unverified_requirements"]))
        self.assertEqual(self.stored(data)["caption"], "Morning practice")
        self.assert_no_network()

    async def test_works_with_no_token_set(self):
        with mock.patch.dict(os.environ):
            del os.environ[server.TOKEN_ENV_VAR]
            data = await self.prepare()
        self.assertTrue(data["valid"])
        self.assert_no_network()

    async def test_reel_defaults_to_sharing_to_feed(self):
        data = await self.prepare(post_type="reel", media_url=REEL_URL)
        self.assertIs(data["share_to_feed"], True)
        self.assertIn("15 minutes", " ".join(data["unverified_requirements"]))

    async def test_share_to_feed_is_rejected_on_a_post(self):
        data = await self.prepare(share_to_feed=False)
        self.assertFalse(data["valid"])
        self.assertIn("only applies to reels", data["errors"][0])

    async def test_invalid_draft_is_not_saved_and_input_is_not_repeated(self):
        data = await self.prepare(media_url="http://cdn.example.com/private-name.jpg")
        self.assertFalse(data["valid"])
        self.assertIsNone(data["draft_id"])
        self.assertEqual(list(self.drafts_dir.iterdir()), [])
        self.assertNotIn("private-name", json.dumps(data))

    async def test_token_in_the_content_is_refused_and_not_shown(self):
        for overrides in (
            {"caption": f"oops {FAKE_TOKEN}"},
            {"media_url": f"https://cdn.example.com/{FAKE_TOKEN}.jpg"},
        ):
            with self.subTest(overrides=list(overrides)):
                data = await self.prepare(**overrides)
                self.assertFalse(data["valid"])
                self.assertNotIn(FAKE_TOKEN, json.dumps(data))
        self.assertEqual(list(self.drafts_dir.iterdir()), [])

    async def test_each_call_creates_a_separate_draft(self):
        first, second = await self.prepare(), await self.prepare()
        self.assertNotEqual(first["draft_id"], second["draft_id"])
        self.assertEqual(first["content_hash"], second["content_hash"])

    async def test_hash_changes_with_every_published_field(self):
        base = (ACCOUNT, "reel", REEL_URL, "caption", True)
        hashes = {drafts.compute_content_hash(*base)}
        for index, value in enumerate(("other", "post", IMAGE_URL, "caption!", False)):
            changed = list(base)
            changed[index] = value
            hashes.add(drafts.compute_content_hash(*changed))
        self.assertEqual(len(hashes), 6)


class ToolDefinitionTests(WorkflowTestCase):
    async def test_all_seven_tools_exist_and_only_publish_is_destructive(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        self.assertEqual(
            set(tools),
            {
                "get_instagram_profile",
                "get_recent_instagram_posts",
                "get_instagram_post_engagement",
                "prepare_instagram_draft",
                "prepare_instagram_local_reel",
                "publish_instagram_draft",
                "test_instagram_local_upload",
            },
        )
        self.assertEqual(
            {name for name, tool in tools.items() if tool.annotations.destructive_hint},
            {"publish_instagram_draft"},
        )
        self.assertFalse(tools["prepare_instagram_draft"].annotations.open_world_hint)

    async def test_the_confirmation_can_not_be_passed_as_a_tool_argument(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        schema = tools["publish_instagram_draft"].input_schema
        self.assertEqual(set(schema["properties"]), {"draft_id", "content_hash"})


class PublishGateTests(WorkflowTestCase):
    """Each of these must stop before any request reaches Meta."""

    async def test_mode_off_stops_before_the_dialog(self):
        draft = await self.prepare()
        for value in ("", "true", "LIVE", "off", "1"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {server.PUBLISHING_MODE_ENV_VAR: value}
            ):
                self.assert_refused(await self.publish(draft), "Publishing is off")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_unset_mode_stops_before_the_dialog(self):
        draft = await self.prepare()
        with mock.patch.dict(os.environ):
            del os.environ[server.PUBLISHING_MODE_ENV_VAR]
            self.assert_refused(await self.publish(draft), "Publishing is off")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_missing_token_stops_before_the_dialog(self):
        draft = await self.prepare()
        with mock.patch.dict(os.environ, {server.TOKEN_ENV_VAR: ""}):
            self.assert_refused(await self.publish(draft), "is not set")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_declining_or_cancelling_stops(self):
        draft = await self.prepare()
        for action in ("decline", "cancel"):
            for mode in (PROTOCOL_MODERN, PROTOCOL_LEGACY):
                with self.subTest(action=action, mode=mode):
                    result = await self.publish(draft, self.person_types(action=action), mode)
                    self.assert_refused(result, f"Nothing was published.*{action}")
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)
        self.assert_no_network()

    async def test_wrong_phrases_stop(self):
        draft = await self.prepare()
        right = f"PUBLISH {draft['content_hash'][:8].upper()}"
        for typed in ("", "yes", "PUBLISH", right.lower(), right + "0", "PUBLISH 00000000"):
            with self.subTest(typed=typed):
                result = await self.publish(draft, self.person_types(typed))
                self.assert_refused(result, "phrase didn't match")
        self.assert_no_network()

    async def test_a_phrase_from_another_draft_stops(self):
        first = await self.prepare(caption="First")
        second = await self.prepare(caption="Second")
        phrase = f"PUBLISH {first['content_hash'][:8].upper()}"
        result = await self.publish(second, self.person_types(phrase))
        self.assert_refused(result, "phrase didn't match")
        self.assert_no_network()

    async def test_client_without_dialog_support_fails_closed(self):
        draft = await self.prepare()
        for mode in (PROTOCOL_MODERN, PROTOCOL_LEGACY):
            with self.subTest(mode=mode):
                async with Client(server.mcp, mode=mode) as client:
                    with self.assertRaisesRegex(MCPError, "elicitation"):
                        await client.call_tool(
                            "publish_instagram_draft",
                            {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]},
                        )
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)
        self.assert_no_network()

    async def test_malformed_dialog_answer_fails_closed(self):
        draft = await self.prepare()

        async def bad_answer(context, params):
            return ElicitResult(action="accept", content={"wrong_field": "x"})

        for mode in (PROTOCOL_MODERN, PROTOCOL_LEGACY):
            with self.subTest(mode=mode):
                self.assertTrue((await self.publish(draft, bad_answer, mode)).is_error)
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)
        self.assert_no_network()

    async def test_wrong_hash_stops_before_the_dialog(self):
        draft = await self.prepare()
        other = dict(draft, content_hash="0" * 64)
        self.assert_refused(await self.publish(other), "doesn't match this draft")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_edited_draft_file_stops_before_the_dialog(self):
        draft = await self.prepare()
        path = self.drafts_dir / f"{draft['draft_id']}.json"
        stored = json.loads(path.read_text("utf-8"))
        stored["caption"] = "Something the user never saw"
        path.write_text(json.dumps(stored), "utf-8")
        self.assert_refused(await self.publish(draft), "changed after it was prepared")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_consistently_edited_draft_still_fails_the_requested_hash(self):
        draft = await self.prepare()
        path = self.drafts_dir / f"{draft['draft_id']}.json"
        stored = json.loads(path.read_text("utf-8"))
        stored["caption"] = "Changed"
        stored["content_hash"] = drafts.compute_content_hash(
            ACCOUNT, "post", IMAGE_URL, "Changed", None
        )
        path.write_text(json.dumps(stored), "utf-8")
        self.assert_refused(await self.publish(draft), "doesn't match this draft")
        self.assert_no_network()

    async def test_bad_draft_ids_never_touch_other_files(self):
        secret_file = self.drafts_dir.parent / "outside.json"
        for draft_id in ("../outside", "..\\outside", "ABC", "", "0" * 15, "g" * 16):
            with self.subTest(draft_id=draft_id):
                result = await self.publish({"draft_id": draft_id, "content_hash": "0" * 64})
                self.assert_refused(result, "not a valid draft ID")
        self.assertFalse(secret_file.exists())
        self.assert_no_network()

    async def test_unknown_draft_id_is_refused(self):
        result = await self.publish({"draft_id": "0" * 16, "content_hash": "0" * 64})
        self.assert_refused(result, "No draft has that ID")

    async def test_corrupt_draft_files_are_refused(self):
        draft = await self.prepare()
        path = self.drafts_dir / f"{draft['draft_id']}.json"
        for content in ("{not json", "[]", "{}"):
            with self.subTest(content=content):
                path.write_text(content, "utf-8")
                self.assert_refused(await self.publish(draft), "couldn't be read|incomplete")
        self.assert_no_network()

    async def test_lock_from_an_interrupted_attempt_blocks_publishing(self):
        draft = await self.prepare()
        lock = self.drafts_dir / f"{draft['draft_id']}.lock"
        lock.touch()
        self.assert_refused(await self.publish(draft), "interrupted")
        self.assertTrue(lock.exists())
        self.assert_no_network()


class DryRunTests(WorkflowTestCase):
    MODE = server.MODE_DRY_RUN

    async def test_dry_run_shows_the_dialog_and_sends_nothing(self):
        draft = await self.prepare()
        result = await self.publish(draft)
        self.assertFalse(result.is_error)
        data = json.loads(result.content[0].text)
        self.assertEqual(data["status"], "dry_run_confirmed")
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(self.stored(draft)["confirmed_at"])
        self.assert_no_network()

    async def test_dry_run_dialog_says_it_is_only_a_test(self):
        draft = await self.prepare()
        await self.publish(draft, self.person_types("no"))
        message = self.dialogs[0]
        self.assertTrue(
            message.startswith("TEST ONLY — Nothing will be published to Instagram\n")
        )
        self.assertNotIn("This will be public", message)
        self.assertNotIn("To publish", message)
        for expected in (draft["draft_id"], draft["content_hash"], "type exactly: "):
            self.assertIn(expected, message)
        self.assert_no_network()

    async def test_dry_run_needs_no_token(self):
        draft = await self.prepare()
        with mock.patch.dict(os.environ):
            del os.environ[server.TOKEN_ENV_VAR]
            result = await self.publish(draft)
        self.assertFalse(result.is_error)
        self.assert_no_network()

    async def test_dry_run_still_rejects_a_wrong_phrase(self):
        draft = await self.prepare()
        self.assert_refused(await self.publish(draft, self.person_types("yes")), "didn't match")
        self.assert_no_network()


class PublishFlowTests(WorkflowTestCase):
    async def test_dialog_shows_the_exact_draft(self):
        draft = await self.prepare(post_type="reel", media_url=REEL_URL, caption="Exact\ncaption 🙏")
        await self.publish(draft, self.person_types("no"))
        message = self.dialogs[0]
        for expected in (REEL_URL, "Exact\ncaption 🙏", draft["draft_id"], draft["content_hash"], ACCOUNT):
            self.assertIn(expected, message)
        self.assertIn("Feed and Reels", message)
        self.assertNotIn(FAKE_TOKEN, message)

    async def test_live_dialog_warns_that_the_post_will_be_public(self):
        draft = await self.prepare()
        await self.publish(draft, self.person_types("no"))
        message = self.dialogs[0]
        self.assertTrue(message.startswith("PUBLISH TO INSTAGRAM? This will be public.\n"))
        self.assertIn("To publish, type exactly: ", message)
        self.assertNotIn("TEST ONLY", message)
        self.assertNotIn("Nothing will be published", message)
        self.assert_no_network()

    async def test_image_post_sends_the_documented_requests_in_order(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        result = await self.publish(draft)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(
            opener.calls(),
            [
                ("GET", f"{ACCOUNT}/content_publishing_limit?fields=quota_usage,config"),
                ("POST", f"{ACCOUNT}/media"),
                ("GET", "555?fields=status_code,status"),
                ("POST", f"{ACCOUNT}/media_publish"),
            ],
        )
        self.assertEqual(
            json.loads(opener.requests[1].data),
            {"image_url": IMAGE_URL, "caption": "Morning practice"},
        )
        self.assertEqual(json.loads(opener.requests[3].data), {"creation_id": "555"})
        data = json.loads(result.content[0].text)
        self.assertEqual((data["status"], data["media_id"]), ("published", "999"))
        stored = self.stored(draft)
        self.assertEqual((stored["status"], stored["media_id"]), ("published", "999"))
        self.assertIsNotNone(stored["confirmed_at"])
        self.assertFalse((self.drafts_dir / f"{draft['draft_id']}.lock").exists())

    async def test_older_protocol_clients_get_the_same_dialog_and_flow(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        result = await self.publish(draft, mode=PROTOCOL_LEGACY)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(len(self.dialogs), 1)
        self.assertEqual(len(opener.publish_calls()), 1)

    async def test_reel_sends_the_documented_fields(self):
        draft = await self.prepare(post_type="reel", media_url=REEL_URL, share_to_feed=False)
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        result = await self.publish(draft)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(
            json.loads(opener.requests[1].data),
            {
                "media_type": "REELS",
                "video_url": REEL_URL,
                "share_to_feed": False,
                "caption": "Morning practice",
            },
        )

    async def test_empty_caption_is_left_out_of_the_request(self):
        draft = await self.prepare(caption="")
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        await self.publish(draft)
        self.assertEqual(json.loads(opener.requests[1].data), {"image_url": IMAGE_URL})

    async def test_token_only_travels_in_the_header(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        result = await self.publish(draft)
        for request in opener.requests:
            self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")
            self.assertNotIn(FAKE_TOKEN, request.full_url)
            self.assertNotIn(FAKE_TOKEN, (request.data or b"").decode())
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertNotIn(FAKE_TOKEN, (self.drafts_dir / f"{draft['draft_id']}.json").read_text("utf-8"))

    async def test_publishing_twice_sends_media_publish_only_once(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        await self.publish(draft)
        result = await self.publish(draft)
        self.assertFalse(result.is_error)
        self.assertIn("already published", result.content[0].text)
        self.assertEqual(len(opener.publish_calls()), 1)
        self.assertEqual(len(self.dialogs), 1)

    async def test_polls_at_the_documented_rate_until_finished(self):
        draft = await self.prepare()
        progress = {"status_code": "IN_PROGRESS"}
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, progress, progress, FINISHED, PUBLISHED)
        result = await self.publish(draft)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(server.time.sleep.call_args_list, [mock.call(60), mock.call(60)])
        self.assertEqual(len(opener.publish_calls()), 1)

    async def test_container_still_processing_is_not_published_and_can_resume(self):
        draft = await self.prepare()
        progress = {"status_code": "IN_PROGRESS"}
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, *[progress] * server.MAX_STATUS_CHECKS)
        self.assert_refused(await self.publish(draft), "not FINISHED")
        self.assertEqual(opener.publish_calls(), [])
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_CONTAINER_CREATED)
        self.assertEqual(server.time.sleep.call_count, server.MAX_STATUS_CHECKS - 1)

        opener = self.use_outcomes(QUOTA_OK, FINISHED, PUBLISHED)
        result = await self.publish(draft)
        self.assertFalse(result.is_error, result.content[0].text)
        # The resume reuses the container and still needs a second confirmation.
        self.assertNotIn(("POST", f"{ACCOUNT}/media"), opener.calls())
        self.assertEqual(len(self.dialogs), 2)

    async def test_failed_and_expired_containers_end_the_draft(self):
        for status_code in ("ERROR", "EXPIRED"):
            with self.subTest(status_code=status_code):
                draft = await self.prepare()
                opener = self.use_outcomes(
                    QUOTA_OK, CONTAINER, {"status_code": status_code, "status": "2207026"}
                )
                self.assert_refused(await self.publish(draft), f"Nothing was published.*{status_code}")
                self.assertEqual(opener.publish_calls(), [])
                self.assertEqual(self.stored(draft)["status"], drafts.STATUS_FAILED)
                self.assert_refused(await self.publish(draft), "Prepare a new draft")

    async def test_unexpected_container_status_does_not_publish(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, {"status_code": "NEW_VALUE"})
        self.assert_refused(await self.publish(draft), "NEW_VALUE")
        self.assertEqual(opener.publish_calls(), [])

    async def test_status_check_failure_does_not_publish(self):
        draft = await self.prepare()
        opener = self.use_outcomes(QUOTA_OK, CONTAINER, graph_error(400, 100, 33))
        self.assert_refused(await self.publish(draft), "status couldn't be read")
        self.assertEqual(opener.publish_calls(), [])
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_CONTAINER_CREATED)

    async def test_container_already_published_is_never_published_again(self):
        draft = await self.prepare()
        progress = {"status_code": "IN_PROGRESS"}
        self.use_outcomes(QUOTA_OK, CONTAINER, *[progress] * server.MAX_STATUS_CHECKS)
        await self.publish(draft)
        opener = self.use_outcomes(QUOTA_OK, {"status_code": "PUBLISHED"})
        result = await self.publish(draft)
        self.assertFalse(result.is_error)
        self.assertEqual(opener.publish_calls(), [])
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PUBLISHED)

    async def test_exhausted_quota_stops_before_creating_a_container(self):
        draft = await self.prepare()
        full = {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
        opener = self.use_outcomes(full)
        self.assert_refused(await self.publish(draft), "100 of its 100")
        self.assertEqual(len(opener.calls()), 1)
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)

    async def test_unavailable_quota_check_becomes_a_warning(self):
        for quota in (graph_error(400, 100), {"data": []}, {"data": [{"quota_usage": "x", "config": {"quota_total": 1}}]}):
            with self.subTest(quota=type(quota).__name__):
                draft = await self.prepare()
                self.use_outcomes(quota, CONTAINER, FINISHED, PUBLISHED)
                result = await self.publish(draft)
                self.assertFalse(result.is_error, result.content[0].text)
                self.assertIn("quota", json.loads(result.content[0].text)["warnings"][0])

    async def test_failed_container_creation_can_be_tried_again(self):
        draft = await self.prepare()
        for outcome, pattern in (
            (graph_error(400, 9004, 2207052, "cannot fetch"), "wasn't created.*2207052.*"),
            (urllib.error.URLError("offline"), "wasn't created.*Could not reach"),
            ({"unexpected": True}, "no valid container ID"),
            ({"id": "../x"}, "no valid container ID"),
        ):
            with self.subTest(pattern=pattern):
                opener = self.use_outcomes(QUOTA_OK, outcome)
                self.assert_refused(await self.publish(draft), pattern)
                self.assertEqual(opener.publish_calls(), [])
                self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PREPARED)
                self.assertFalse((self.drafts_dir / f"{draft['draft_id']}.lock").exists())

    async def test_permission_error_includes_the_publishing_hint(self):
        draft = await self.prepare()
        self.use_outcomes(QUOTA_OK, graph_error(403, 10, message="denied"))
        self.assert_refused(await self.publish(draft), "denied.*instagram_content_publish")

    async def test_clear_refusal_keeps_the_container_for_a_confirmed_retry(self):
        cases = (
            (9007, 2207027, "not ready"),
            (9, 2207042, "daily limit"),
            (190, None, "token expired"),
            (4, 2207051, "restricted"),
        )
        for code, subcode, message in cases:
            with self.subTest(code=code):
                draft = await self.prepare()
                opener = self.use_outcomes(
                    QUOTA_OK, CONTAINER, FINISHED, graph_error(400, code, subcode, message)
                )
                self.assert_refused(await self.publish(draft), f"nothing was published.*{message}")
                self.assertEqual(len(opener.publish_calls()), 1)
                self.assertEqual(self.stored(draft)["status"], drafts.STATUS_CONTAINER_CREATED)

    async def test_uncertain_publish_outcomes_are_never_retried(self):
        cases = {
            "timeout": TimeoutError("timed out"),
            "network": urllib.error.URLError("reset"),
            "http 500": graph_error(500, 2, message="service"),
            "http 503 no body": urllib.error.HTTPError(
                "https://example.invalid", 503, "error", {}, io.BytesIO(b"<html>")
            ),
            "documented server error": graph_error(400, -1, 2207001),
            "transient flag": graph_error(400, 100, is_transient=True),
            "no media id": {"ok": True},
            "invalid json shape": ["not", "an", "object"],
        }
        for name, outcome in cases.items():
            with self.subTest(name=name):
                draft = await self.prepare()
                opener = self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, outcome)
                self.assert_refused(await self.publish(draft), "may or may not be live")
                self.assertEqual(len(opener.publish_calls()), 1)
                self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PUBLISH_UNKNOWN)

    async def test_unknown_outcome_blocks_publishing_when_called_again(self):
        draft = await self.prepare()
        self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, TimeoutError())
        await self.publish(draft)
        self.dialogs.clear()

        # Meta reports FINISHED, which could be a delay, so this still doesn't publish.
        opener = self.use_outcomes(FINISHED)
        self.assert_refused(await self.publish(draft), "still unknown.*FINISHED")
        self.assertEqual(opener.calls(), [("GET", "555?fields=status_code,status")])
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PUBLISH_UNKNOWN)

        opener = self.use_outcomes(graph_error(400, 100, 33))
        self.assert_refused(await self.publish(draft), "still unknown")
        self.assertEqual(opener.publish_calls(), [])

        opener = self.use_outcomes({"status_code": "PUBLISHED"})
        result = await self.publish(draft)
        self.assertFalse(result.is_error)
        self.assertEqual(opener.publish_calls(), [])
        self.assertEqual(self.stored(draft)["status"], drafts.STATUS_PUBLISHED)
        self.assertEqual(self.dialogs, [])

    async def test_crash_during_media_publish_leaves_the_outcome_unknown(self):
        """The unknown status must be on disk before the publish request is sent."""
        draft = await self.prepare()
        seen = {}
        original_open = FakeOpener.open

        def spying_open(opener, request, timeout=None):
            if request.full_url.endswith("/media_publish"):
                seen["status"] = self.stored(draft)["status"]
            return original_open(opener, request, timeout)

        self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, PUBLISHED)
        with mock.patch.object(FakeOpener, "open", spying_open):
            await self.publish(draft)
        self.assertEqual(seen["status"], drafts.STATUS_PUBLISH_UNKNOWN)

    async def test_partial_success_is_published_with_a_warning(self):
        draft = await self.prepare()
        self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, {"id": "999", "config_issue": "CAPTION_NOT_ATTACHED"})
        result = await self.publish(draft)
        data = json.loads(result.content[0].text)
        self.assertEqual(data["status"], "published")
        self.assertIn("CAPTION_NOT_ATTACHED", data["warnings"][0])

    async def test_token_in_api_messages_is_redacted_everywhere(self):
        leak = f"bad token {FAKE_TOKEN}"
        draft = await self.prepare()
        self.use_outcomes(QUOTA_OK, graph_error(400, 100, message=leak, error_user_msg=leak))
        result = await self.publish(draft)
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertIn("[REDACTED]", result.content[0].text)

        self.use_outcomes(QUOTA_OK, CONTAINER, FINISHED, graph_error(500, 2, message=leak))
        result = await self.publish(draft)
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertNotIn(FAKE_TOKEN, (self.drafts_dir / f"{draft['draft_id']}.json").read_text("utf-8"))

        self.use_outcomes(graph_error(500, 2, message=leak))
        result = await self.publish(draft)
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)

    async def test_status_change_during_the_dialog_stops_the_attempt(self):
        draft = await self.prepare()

        async def other_process_publishes(context, params):
            stored = self.stored(draft)
            stored["status"] = drafts.STATUS_PUBLISHED
            drafts.save_draft(stored)
            typed = params.message.split("type exactly: ")[1].split("\n")[0]
            return ElicitResult(action="accept", content={"confirmation": typed})

        result = await self.publish(draft, other_process_publishes)
        self.assertIn("already published", result.content[0].text)
        self.assert_no_network()


class SafetyNetTests(WorkflowTestCase):
    def test_real_connections_are_blocked_during_tests(self):
        with self.assertRaisesRegex(AssertionError, "real network"):
            http.client.HTTPSConnection("example.invalid").connect()


if __name__ == "__main__":
    unittest.main()
