"""Offline tests for Reel drafts bound to a local preview file, and for their upload.

Nothing here can reach Meta, upload or publish. Each test uses a fake token and
temporary folders, API responses are mocked, and HTTP connections are made to fail.
LoopbackUploadTests is the one exception to the mocks: it sends a real request, but
only to a temporary server on 127.0.0.1. Run with:

    .\\.venv\\Scripts\\python.exe -m unittest test_instagram_local_drafts -v
"""

import ast
import hashlib
import http.server
import inspect
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import instagram_drafts as drafts
import instagram_mcp_server as server
from mcp import Client
from mcp.shared.exceptions import MCPError
from test_instagram_publishing import (
    ACCOUNT,
    CONTAINER,
    FAKE_TOKEN,
    FINISHED,
    PUBLISHED,
    QUOTA_OK,
    FakeOpener,
    WorkflowTestCase,
    graph_error,
)

NAME = "IMG_6711-20261104-144518-3b06.mp4"
VIDEO = b"pretend this is an mp4"
UPLOAD_OK = {"success": True, "message": "Upload successful."}


class LocalDraftTestCase(WorkflowTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.previews = Path(temporary.name)
        patcher = mock.patch.object(drafts, "PREVIEWS_DIR", self.previews)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_preview(self, name=NAME, data=VIDEO, **record_changes):
        (self.previews / name).write_bytes(data)
        record = {
            "status": "awaiting_review",
            "preview_file": name,
            "preview_sha256": hashlib.sha256(data).hexdigest(),
            "problems": [],
        }
        record.update(record_changes)
        self.write_record(name, record)

    def write_record(self, name, record):
        (self.previews / name).with_suffix(".json").write_text(json.dumps(record), encoding="utf-8")

    def make_draft(self, name=NAME, caption="Hello", share_to_feed=True):
        return drafts.create_local_reel_draft(ACCOUNT, name, caption, share_to_feed)

    def load(self, draft):
        return drafts.load_draft(draft["draft_id"], draft["content_hash"], ACCOUNT)

    def assert_load_refused(self, draft, pattern):
        with self.assertRaisesRegex(drafts.DraftError, pattern):
            self.load(draft)

    def edit_saved_draft(self, draft, **changes):
        path = self.drafts_dir / f"{draft['draft_id']}.json"
        saved = json.loads(path.read_text("utf-8"))
        saved.update(changes)
        path.write_text(json.dumps(saved), "utf-8")


class CreateTests(LocalDraftTestCase):
    def test_draft_is_bound_to_the_files_hash_and_size(self):
        self.add_preview()
        draft = self.make_draft()
        self.assertEqual(
            draft["local_file"],
            {"name": NAME, "sha256": hashlib.sha256(VIDEO).hexdigest(), "size": len(VIDEO)},
        )
        self.assertEqual(draft["media_source"], drafts.MEDIA_SOURCE_LOCAL_FILE)
        self.assertIsNone(draft["media_url"])
        self.assertEqual(draft["post_type"], drafts.POST_TYPE_REEL)
        self.assertEqual(draft["status"], drafts.STATUS_PREPARED)
        self.assertEqual(self.load(draft)["draft_id"], draft["draft_id"])

    def test_hash_changes_with_everything_that_would_be_published(self):
        local = {"name": NAME, "sha256": "a" * 64, "size": 10}
        base = drafts.compute_local_content_hash(ACCOUNT, local, "Hello", True)
        changed = [
            drafts.compute_local_content_hash("1", local, "Hello", True),
            drafts.compute_local_content_hash(ACCOUNT, {**local, "name": "x.mp4"}, "Hello", True),
            drafts.compute_local_content_hash(ACCOUNT, {**local, "sha256": "b" * 64}, "Hello", True),
            drafts.compute_local_content_hash(ACCOUNT, {**local, "size": 11}, "Hello", True),
            drafts.compute_local_content_hash(ACCOUNT, local, "Other", True),
            drafts.compute_local_content_hash(ACCOUNT, local, "Hello", False),
        ]
        self.assertNotIn(base, changed)
        self.assertEqual(len(set(changed)), len(changed))

    def test_file_names_outside_the_previews_folder_are_refused(self):
        self.add_preview()
        secret_file = self.previews.parent / "secret.mp4"
        self.addCleanup(secret_file.unlink, missing_ok=True)
        secret_file.write_bytes(b"not for upload")
        for name in (
            "../secret.mp4",
            "..\\secret.mp4",
            str(secret_file),
            "folder/clip.mp4",
            "C:clip.mp4",
            f"{NAME}:hidden",
            f"{NAME}.",
            f"{NAME} ",
            "clip.MP4",
            "clip.mov",
            "clip.json",
            ".mp4",
            "",
            None,
            7,
        ):
            with self.subTest(name=name), self.assertRaisesRegex(drafts.DraftError, "reels/previews"):
                self.make_draft(name)
        self.assertEqual(list(self.drafts_dir.iterdir()), [])

    def test_missing_folder_named_like_a_file_and_empty_files_are_refused(self):
        with self.assertRaisesRegex(drafts.DraftError, "isn't in reels/previews"):
            self.make_draft()
        (self.previews / "folder.mp4").mkdir()
        with self.assertRaisesRegex(drafts.DraftError, "isn't in reels/previews"):
            self.make_draft("folder.mp4")
        self.add_preview("empty.mp4", b"")
        with self.assertRaisesRegex(drafts.DraftError, "empty"):
            self.make_draft("empty.mp4")

    def test_oversized_file_is_refused(self):
        self.add_preview()
        with mock.patch.object(drafts, "MAX_REEL_FILE_BYTES", len(VIDEO) - 1):
            with self.assertRaisesRegex(drafts.DraftError, "300 MB"):
                self.make_draft()

    def test_link_to_another_file_is_refused(self):
        outside = self.drafts_dir / "outside.mp4"
        outside.write_bytes(VIDEO)
        try:
            os.symlink(outside, self.previews / NAME)
        except (OSError, NotImplementedError):
            self.skipTest("This account can't create symbolic links.")
        self.write_record(
            NAME,
            {
                "status": "awaiting_review",
                "preview_file": NAME,
                "preview_sha256": hashlib.sha256(VIDEO).hexdigest(),
                "problems": [],
            },
        )
        with self.assertRaisesRegex(drafts.DraftError, "real file"):
            self.make_draft()

    def test_review_record_must_allow_the_file(self):
        cases = {
            "needs_attention": ({"status": "needs_attention"}, "status isn't awaiting_review"),
            "other file": ({"preview_file": "other.mp4"}, "different file"),
            "problems": ({"problems": ["The video isn't H.264."]}, "checks passed"),
            "no problems list": ({"problems": None}, "checks passed"),
            "wrong hash": ({"preview_sha256": "0" * 64}, "doesn't match"),
            "bad hash": ({"preview_sha256": "xyz"}, "no valid SHA-256"),
            "uppercase hash": ({"preview_sha256": "A" * 64}, "no valid SHA-256"),
        }
        for label, (changes, pattern) in cases.items():
            with self.subTest(label=label):
                self.add_preview(**changes)
                with self.assertRaisesRegex(drafts.DraftError, pattern):
                    self.make_draft()

    def test_missing_or_unreadable_record_is_refused(self):
        self.add_preview()
        record = (self.previews / NAME).with_suffix(".json")
        for text, pattern in (("not json", "couldn't be read"), ("[]", "couldn't be read")):
            with self.subTest(text=text):
                record.write_text(text)
                with self.assertRaisesRegex(drafts.DraftError, pattern):
                    self.make_draft()
        record.unlink()
        with self.assertRaisesRegex(drafts.DraftError, "no review record"):
            self.make_draft()

    def test_text_is_validated(self):
        self.add_preview()
        with self.assertRaisesRegex(drafts.DraftError, "2201 characters"):
            self.make_draft(caption="a" * 2201)
        with self.assertRaisesRegex(drafts.DraftError, "share_to_feed"):
            self.make_draft(share_to_feed=None)
        with self.assertRaisesRegex(drafts.DraftError, "caption must be text"):
            self.make_draft(caption=None)
        with self.assertRaisesRegex(drafts.DraftError, "access token"):
            drafts.create_local_reel_draft(ACCOUNT, NAME, f"x {FAKE_TOKEN}", True, FAKE_TOKEN)
        self.assertEqual(list(self.drafts_dir.iterdir()), [])

    def test_error_does_not_repeat_a_bad_file_name(self):
        with self.assertRaises(drafts.DraftError) as caught:
            self.make_draft(f"../{FAKE_TOKEN}.mp4")
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_creating_a_draft_does_not_change_the_preview(self):
        self.add_preview()
        record = (self.previews / NAME).with_suffix(".json")
        before = record.read_bytes()
        self.make_draft()
        self.assertEqual((self.previews / NAME).read_bytes(), VIDEO)
        self.assertEqual(record.read_bytes(), before)


class EveryLoadTests(LocalDraftTestCase):
    def setUp(self):
        super().setUp()
        self.add_preview()
        self.draft = self.make_draft()

    def test_same_size_change_to_the_file_is_refused(self):
        self.assertEqual(len(VIDEO), len(b"PRETEND this is an mp4"))
        (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")
        self.assert_load_refused(self.draft, "doesn't match")

    def test_file_replaced_with_its_record_updated_is_refused(self):
        self.add_preview(data=b"a different video")
        self.assert_load_refused(self.draft, "changed after this draft")

    def test_deleted_file_is_refused(self):
        (self.previews / NAME).unlink()
        self.assert_load_refused(self.draft, "isn't in reels/previews")

    def test_record_changed_to_needs_attention_is_refused(self):
        self.add_preview(status="needs_attention")
        self.assert_load_refused(self.draft, "status isn't")

    def test_deleted_record_is_refused(self):
        (self.previews / NAME).with_suffix(".json").unlink()
        self.assert_load_refused(self.draft, "no review record")

    def test_edited_draft_fields_are_refused(self):
        other = {"name": "other.mp4", "sha256": "a" * 64, "size": 1}
        for changes in (
            {"caption": "Edited"},
            {"share_to_feed": False},
            {"local_file": {**self.draft["local_file"], "size": 1}},
            {"local_file": {**self.draft["local_file"], "sha256": "a" * 64}},
            {"local_file": other},
            {"media_source": drafts.MEDIA_SOURCE_URL},
        ):
            with self.subTest(changes=changes):
                self.edit_saved_draft(self.draft, **changes)
                self.assert_load_refused(self.draft, "changed after it was prepared")
                self.edit_saved_draft(self.draft, **{k: self.draft[k] for k in changes})
        self.load(self.draft)

    def test_url_or_post_type_added_to_a_local_draft_is_refused(self):
        self.edit_saved_draft(self.draft, media_url="https://cdn.example.com/other.mp4")
        self.assert_load_refused(self.draft, "incomplete")
        self.edit_saved_draft(self.draft, media_url=None, post_type="post")
        self.assert_load_refused(self.draft, "incomplete")

    def test_unknown_source_and_missing_file_details_are_refused(self):
        for source in ("ftp", ["local_file"], None):
            with self.subTest(source=source):
                self.edit_saved_draft(self.draft, media_source=source)
                self.assert_load_refused(self.draft, "unknown media source")
        self.edit_saved_draft(self.draft, media_source=drafts.MEDIA_SOURCE_LOCAL_FILE, local_file=None)
        self.assert_load_refused(self.draft, "incomplete")

    def test_wrong_expected_hash_and_account_are_refused(self):
        with self.assertRaisesRegex(drafts.DraftError, "doesn't match this draft"):
            drafts.load_draft(self.draft["draft_id"], "0" * 64, ACCOUNT)
        with self.assertRaisesRegex(drafts.DraftError, "different Instagram account"):
            drafts.load_draft(self.draft["draft_id"], self.draft["content_hash"], "1")


class UrlDraftCompatibilityTests(LocalDraftTestCase):
    def test_url_draft_hash_is_unchanged(self):
        # Keep the legacy URL-draft hash stable for this synthetic fixture.
        self.assertEqual(
            drafts.compute_content_hash(
                "1001",
                "post",
                "https://example.com/test-image.jpg",
                "A sample Reel draft. #Example",
                None,
            ),
            "52084a6fce85ab6ddfde445f3d0616aab13daceee45984412319952dd62dd615",
        )

    async def test_url_draft_without_the_new_fields_still_loads(self):
        draft = await self.prepare()
        saved = self.stored(draft)
        self.assertNotIn("media_source", saved)
        self.assertNotIn("local_file", saved)
        self.assertFalse(drafts.is_local_file_draft(saved))
        self.assertEqual(self.load(draft)["media_url"], draft["media_url"])

    async def test_url_draft_can_not_be_turned_into_a_local_draft(self):
        draft = await self.prepare()
        self.add_preview()
        self.edit_saved_draft(
            draft,
            media_source=drafts.MEDIA_SOURCE_LOCAL_FILE,
            local_file={"name": NAME, "sha256": hashlib.sha256(VIDEO).hexdigest(), "size": len(VIDEO)},
        )
        self.assert_load_refused(draft, "changed after it was prepared")

    async def test_url_draft_still_gets_the_dialog(self):
        draft = await self.prepare()
        result = await self.publish(draft, self.person_types(action="decline"))
        self.assert_refused(result, "ended with: decline")
        self.assertEqual(len(self.dialogs), 1)
        self.assert_no_network()


class UploadOpener(FakeOpener):
    """Reads each upload body as a real connection would, and can run a step at a set point."""

    def __init__(self, outcomes, before_upload=None, after_container=None):
        super().__init__(outcomes)
        self.bodies = []
        self.before_upload = before_upload
        self.after_container = after_container

    def open(self, request, timeout=None):
        if hasattr(request.data, "read"):
            if self.before_upload:
                self.before_upload()
            self.bodies.append(b"".join(iter(lambda: request.data.read(7), b"")))
        response = super().open(request, timeout)
        if request.full_url.endswith("/media") and self.after_container:
            self.after_container()
        return response


class LocalUploadTestCase(LocalDraftTestCase):
    def setUp(self):
        super().setUp()
        self.add_preview()
        self.draft = self.make_draft()

    def use_uploads(self, *outcomes, **hooks):
        opener = UploadOpener(outcomes, **hooks)
        for outcome in outcomes:
            if isinstance(outcome, urllib.error.HTTPError):
                self.addCleanup(outcome.close)
        patcher = mock.patch.object(server.urllib.request, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opener = opener
        return opener

    def upload_requests(self):
        return [r for r in self.opener.requests if r.full_url.startswith("https://rupload.")]

    def container_requests(self):
        return [r for r in self.opener.requests if r.full_url.endswith("/media")]

    def assert_no_upload_or_publish(self):
        self.assertEqual(self.upload_requests(), [])
        self.assertEqual(self.opener.publish_calls(), [])

    def assert_unlocked(self):
        self.assertFalse((self.drafts_dir / f"{self.draft['draft_id']}.lock").exists())


class UploadFlowTests(LocalUploadTestCase):
    async def test_confirmed_draft_follows_metas_documented_order(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, PUBLISHED)
        result = await self.publish(self.draft)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(json.loads(result.content[0].text)["status"], drafts.STATUS_PUBLISHED)
        self.assertEqual(
            [(r.get_method(), r.full_url.split("?")[0]) for r in self.opener.requests],
            [
                ("GET", f"{server.GRAPH_API_ROOT}/{ACCOUNT}/content_publishing_limit"),
                ("POST", f"{server.GRAPH_API_ROOT}/{ACCOUNT}/media"),
                ("POST", "https://rupload.facebook.com/ig-api-upload/v26.0/555"),
                ("GET", f"{server.GRAPH_API_ROOT}/555"),
                ("POST", f"{server.GRAPH_API_ROOT}/{ACCOUNT}/media_publish"),
            ],
        )
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_PUBLISHED)
        self.assertIs(saved["upload_complete"], True)
        self.assertEqual(saved["media_id"], "999")
        self.assert_unlocked()

    async def test_container_request_is_resumable_and_has_no_url(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, PUBLISHED)
        await self.publish(self.draft)
        body = json.loads(self.container_requests()[0].data)
        self.assertEqual(
            body,
            {"media_type": "REELS", "upload_type": "resumable", "share_to_feed": True, "caption": "Hello"},
        )

    async def test_upload_request_matches_metas_documented_format(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, PUBLISHED)
        await self.publish(self.draft)
        request = self.upload_requests()[0]
        self.assertEqual(request.get_header("Authorization"), f"OAuth {FAKE_TOKEN}")
        self.assertEqual(request.get_header("Offset"), "0")
        self.assertEqual(request.get_header("File_size"), str(len(VIDEO)))
        self.assertEqual(request.get_header("Content-length"), str(len(VIDEO)))
        self.assertEqual(self.opener.bodies, [VIDEO])

    async def test_token_is_never_sent_to_the_upload_address_in_a_response(self):
        container = {"id": "555", "uri": "https://attacker.example/steal"}
        self.use_uploads(QUOTA_OK, container, UPLOAD_OK, FINISHED, PUBLISHED)
        await self.publish(self.draft)
        for request in self.opener.requests:
            self.assertNotIn("attacker", request.full_url)
        self.assertEqual(len(self.upload_requests()), 1)

    async def test_container_id_that_is_not_a_number_stops_before_the_upload(self):
        self.use_uploads(QUOTA_OK, {"id": "../../evil"})
        self.assert_refused(await self.publish(self.draft), "no valid container ID")
        self.assert_no_upload_or_publish()

    def test_upload_address_uses_the_same_api_version_as_other_requests(self):
        version = server.GRAPH_API_ROOT.rsplit("/", 1)[1]
        self.assertEqual(server.RUPLOAD_API_ROOT, f"https://rupload.facebook.com/ig-api-upload/{version}")


class ApprovalBeforeNetworkTests(LocalUploadTestCase):
    def setUp(self):
        super().setUp()
        self.use_uploads()

    def assert_nothing_happened(self):
        self.assert_no_network()
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(saved["container_id"])
        self.assertIsNone(saved["confirmed_at"])
        self.assert_unlocked()

    async def test_declining_or_cancelling_sends_nothing(self):
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                result = await self.publish(self.draft, self.person_types(action=action))
                self.assert_refused(result, f"ended with: {action}")
                self.assert_nothing_happened()

    async def test_wrong_phrases_send_nothing(self):
        right = f"PUBLISH {self.draft['content_hash'][:8].upper()}"
        for typed in ("", "yes", "PUBLISH", right.lower(), right + "0", "PUBLISH 00000000"):
            with self.subTest(typed=typed):
                self.assert_refused(await self.publish(self.draft, self.person_types(typed)), "didn't match")
                self.assert_nothing_happened()

    async def test_phrase_from_another_draft_sends_nothing(self):
        other = self.make_draft(caption="Another caption")
        phrase = f"PUBLISH {other['content_hash'][:8].upper()}"
        self.assert_refused(await self.publish(self.draft, self.person_types(phrase)), "didn't match")
        self.assert_nothing_happened()

    async def test_every_attempt_shows_the_dialog(self):
        for _ in range(2):
            await self.publish(self.draft, self.person_types("no"))
        self.assertEqual(len(self.dialogs), 2)

    async def test_dialog_shows_the_exact_file_and_caption(self):
        await self.publish(self.draft, self.person_types("no"))
        message = self.dialogs[0]
        self.assertTrue(message.startswith("PUBLISH TO INSTAGRAM? This will be public.\n"))
        for expected in (
            NAME,
            str(len(VIDEO)),
            hashlib.sha256(VIDEO).hexdigest(),
            "uploaded to Meta after you confirm",
            "Hello",
            self.draft["draft_id"],
            self.draft["content_hash"],
        ):
            self.assertIn(expected, message)
        self.assertNotIn("Media URL", message)
        self.assertNotIn(FAKE_TOKEN, message)

    async def test_publishing_off_stops_before_the_dialog(self):
        with mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: "off"}):
            self.assert_refused(await self.publish(self.draft), "Publishing is off")
        self.assertEqual(self.dialogs, [])
        self.assert_nothing_happened()

    async def test_missing_token_in_live_mode_stops_before_the_dialog(self):
        with mock.patch.dict(os.environ):
            del os.environ[server.TOKEN_ENV_VAR]
            self.assert_refused(await self.publish(self.draft), "not set")
        self.assertEqual(self.dialogs, [])
        self.assert_nothing_happened()

    async def test_dry_run_confirms_without_any_request_or_change(self):
        before = self.stored(self.draft)
        with mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: server.MODE_DRY_RUN}):
            result = await self.publish(self.draft)
        self.assertEqual(json.loads(result.content[0].text)["status"], "dry_run_confirmed")
        self.assertTrue(self.dialogs[0].startswith("TEST ONLY"))
        self.assertIn(NAME, self.dialogs[0])
        self.assertNotIn("uploaded to Meta", self.dialogs[0])
        self.assert_no_network()
        self.assertEqual(self.stored(self.draft), before)

    async def test_dry_run_with_a_wrong_phrase_is_refused(self):
        with mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: server.MODE_DRY_RUN}):
            self.assert_refused(await self.publish(self.draft, self.person_types("yes")), "didn't match")
        self.assert_nothing_happened()


class FileChangeTests(LocalUploadTestCase):
    async def test_changed_file_stops_before_the_dialog(self):
        self.use_uploads()
        (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")
        self.assert_refused(await self.publish(self.draft), "doesn't match")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_deleted_file_stops_before_the_dialog(self):
        self.use_uploads()
        (self.previews / NAME).unlink()
        self.assert_refused(await self.publish(self.draft), "isn't in reels/previews")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_file_changed_during_the_dialog_sends_nothing(self):
        self.use_uploads()
        confirm = self.person_types()

        async def change_then_confirm(context, params):
            (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")
            return await confirm(context, params)

        self.assert_refused(await self.publish(self.draft, change_then_confirm), "doesn't match")
        self.assert_no_network()

    async def test_file_changed_just_before_the_upload_is_not_sent(self):
        def change():
            (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")

        self.use_uploads(QUOTA_OK, CONTAINER, after_container=change)
        self.assert_refused(await self.publish(self.draft), "Nothing was uploaded or published.*doesn't match")
        self.assert_no_upload_or_publish()
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(saved["container_id"])
        self.assert_unlocked()

    async def test_record_changed_just_before_the_upload_is_not_sent(self):
        def change():
            self.add_preview(status="needs_attention")

        self.use_uploads(QUOTA_OK, CONTAINER, after_container=change)
        self.assert_refused(await self.publish(self.draft), "status isn't awaiting_review")
        self.assert_no_upload_or_publish()

    async def test_file_changed_during_the_upload_is_never_published(self):
        def change():
            (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")

        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, before_upload=change)
        self.assert_refused(await self.publish(self.draft), "changed during the upload")
        self.assertEqual(self.opener.publish_calls(), [])
        self.assertNotIn(("GET", "555?fields=status_code,status"), self.opener.calls())
        saved = self.stored(self.draft)
        self.assertIs(saved["upload_complete"], False)
        self.assertIsNone(saved["container_id"])

    def test_reader_only_matches_the_exact_bytes(self):
        local = {"name": NAME, "sha256": hashlib.sha256(VIDEO).hexdigest(), "size": len(VIDEO)}
        with drafts.open_verified_reel(local) as reader:
            self.assertFalse(reader.matches())
            self.assertEqual(reader.read(), VIDEO)
            self.assertEqual(reader.read(), b"")
            self.assertTrue(reader.matches())
        with drafts.open_verified_reel(local) as reader:
            reader.read(3)
            self.assertFalse(reader.matches())

    def test_reader_never_reads_past_the_approved_size(self):
        data = VIDEO
        self.add_preview(data=data)
        local = {"name": NAME, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        with drafts.open_verified_reel(local) as reader:
            with open(self.previews / NAME, "ab") as extra:
                extra.write(b"more")
            self.assertEqual(len(reader.read()), len(data))
            self.assertEqual(reader.read(100), b"")
            self.assertTrue(reader.matches())


def upload_error(status, retriable=None, message="upload refused", error_type="ProcessingFailedError"):
    details = {"type": error_type, "message": message}
    if retriable is not None:
        details["retriable"] = retriable
    body = json.dumps({"debug_info": details}).encode()
    return urllib.error.HTTPError("https://example.invalid", status, "error", {}, io.BytesIO(body))


class UploadFailureTests(LocalUploadTestCase):
    def assert_can_try_again(self):
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(saved["container_id"])
        self.assertIs(saved["upload_complete"], False)
        self.assertEqual(self.opener.publish_calls(), [])
        self.assert_unlocked()

    async def test_failures_that_may_pass_later_are_not_retried_automatically(self):
        cases = {
            "retriable": upload_error(500, retriable=True),
            "unknown": upload_error(500),
            "timeout": TimeoutError(),
            "network": urllib.error.URLError("no route"),
            "redirect": urllib.error.HTTPError("https://example.invalid", 302, "moved", {}, io.BytesIO(b"")),
            "not json": urllib.error.HTTPError("https://example.invalid", 502, "bad", {}, io.BytesIO(b"<html>")),
            "no success": {"success": False},
            "empty": {},
        }
        for label, outcome in cases.items():
            with self.subTest(label=label):
                self.use_uploads(QUOTA_OK, CONTAINER, outcome)
                self.assert_refused(await self.publish(self.draft), "Nothing was published")
                self.assertEqual(len(self.upload_requests()), 1)
                self.assert_can_try_again()

    async def test_final_refusal_marks_the_draft_failed(self):
        self.use_uploads(QUOTA_OK, CONTAINER, upload_error(400, retriable=False))
        result = await self.publish(self.draft)
        self.assert_refused(result, "ProcessingFailedError.*prepare a new draft")
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_FAILED)
        self.assertEqual(self.opener.publish_calls(), [])
        self.use_uploads()
        self.assert_refused(await self.publish(self.draft), "can't be published")
        self.assert_no_network()

    async def test_final_refusal_in_a_200_response_also_marks_the_draft_failed(self):
        refusal = {"debug_info": {"retriable": False, "type": "ProcessingFailedError", "message": "bad"}}
        self.use_uploads(QUOTA_OK, CONTAINER, refusal)
        self.assert_refused(await self.publish(self.draft), "ProcessingFailedError")
        self.assertEqual(self.stored(self.draft)["status"], drafts.STATUS_FAILED)

    async def test_retry_needs_a_new_phrase_and_starts_a_new_container_from_zero(self):
        self.use_uploads(QUOTA_OK, CONTAINER, TimeoutError())
        await self.publish(self.draft)
        self.assertEqual(len(self.dialogs), 1)
        self.use_uploads(QUOTA_OK, {"id": "777"}, UPLOAD_OK, FINISHED, PUBLISHED)
        result = await self.publish(self.draft)
        self.assertFalse(result.is_error, result.content[0].text)
        self.assertEqual(len(self.dialogs), 2)
        self.assertEqual(self.upload_requests()[0].full_url.rsplit("/", 1)[1], "777")
        self.assertEqual(self.upload_requests()[0].get_header("Offset"), "0")
        self.assertEqual(self.opener.bodies, [VIDEO])
        publish = [r for r in self.opener.requests if r.full_url.endswith("/media_publish")]
        self.assertEqual(json.loads(publish[0].data), {"creation_id": "777"})

    async def test_declined_retry_sends_nothing(self):
        self.use_uploads(QUOTA_OK, CONTAINER, TimeoutError())
        await self.publish(self.draft)
        self.use_uploads()
        self.assert_refused(await self.publish(self.draft, self.person_types(action="decline")), "decline")
        self.assert_no_network()

    async def test_interrupted_attempt_never_reuses_its_container(self):
        self.edit_saved_draft(self.draft, status=drafts.STATUS_CONTAINER_CREATED, container_id="111")
        self.use_uploads(QUOTA_OK, {"id": "777"}, UPLOAD_OK, FINISHED, PUBLISHED)
        self.assertFalse((await self.publish(self.draft)).is_error)
        self.assertEqual(len(self.dialogs), 1)
        for request in self.opener.requests:
            self.assertNotIn("/111", request.full_url)

    async def test_container_failure_uploads_nothing(self):
        self.use_uploads(QUOTA_OK, graph_error(400, 100))
        self.assert_refused(await self.publish(self.draft), "container wasn't created")
        self.assert_no_upload_or_publish()
        self.assert_can_try_again()

    async def test_full_quota_uploads_nothing(self):
        full = {"data": [{"quota_usage": 100, "config": {"quota_total": 100}}]}
        self.use_uploads(full)
        self.assert_refused(await self.publish(self.draft), "used 100 of its 100")
        self.assertEqual(self.container_requests(), [])
        self.assert_no_upload_or_publish()

    async def test_permission_refusal_mentions_the_token_without_showing_it(self):
        self.use_uploads(QUOTA_OK, CONTAINER, upload_error(403, message=f"bad token {FAKE_TOKEN}"))
        result = await self.publish(self.draft)
        text = result.content[0].text
        self.assert_refused(result, "may not have access to resumable uploads")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("[REDACTED]", text)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.stored(self.draft)))

    async def test_unexpected_error_text_is_not_shown(self):
        self.use_uploads(QUOTA_OK, CONTAINER, RuntimeError(f"secret {FAKE_TOKEN}"))
        result = await self.publish(self.draft)
        self.assert_refused(result, "Unexpected RuntimeError")
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.stored(self.draft)))


class ProcessingTests(LocalUploadTestCase):
    async def test_slow_processing_continues_without_a_second_upload(self):
        waiting = {"status_code": "IN_PROGRESS"}
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, *[waiting] * server.MAX_STATUS_CHECKS)
        self.assert_refused(await self.publish(self.draft), "not FINISHED")
        saved = self.stored(self.draft)
        self.assertEqual(saved["status"], drafts.STATUS_CONTAINER_CREATED)
        self.assertIs(saved["upload_complete"], True)
        self.assertEqual(self.opener.publish_calls(), [])

        self.use_uploads(QUOTA_OK, FINISHED, PUBLISHED)
        self.assertFalse((await self.publish(self.draft)).is_error)
        self.assertEqual(len(self.dialogs), 2)
        self.assertEqual(self.upload_requests(), [])
        self.assertEqual(self.container_requests(), [])
        self.assertEqual(len(self.opener.publish_calls()), 1)

    async def test_processing_error_or_expiry_is_never_published(self):
        for status_code in ("ERROR", "EXPIRED"):
            with self.subTest(status_code=status_code):
                draft = self.make_draft(caption=status_code)
                self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, {"status_code": status_code, "status": "x"})
                self.assert_refused(await self.publish(draft), "Nothing was published")
                self.assertEqual(self.stored(draft)["status"], drafts.STATUS_FAILED)
                self.assertEqual(self.opener.publish_calls(), [])

    async def test_status_read_failure_is_never_published(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, urllib.error.URLError("down"))
        self.assert_refused(await self.publish(self.draft), "status couldn't be read")
        self.assertEqual(self.opener.publish_calls(), [])
        self.assertIs(self.stored(self.draft)["upload_complete"], True)

    async def test_uncertain_publish_is_not_repeated_even_if_the_preview_is_deleted(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, graph_error(500, 2))
        self.assert_refused(await self.publish(self.draft), "may or may not be live")
        self.assertEqual(self.stored(self.draft)["status"], drafts.STATUS_PUBLISH_UNKNOWN)

        (self.previews / NAME).unlink()
        (self.previews / NAME).with_suffix(".json").unlink()
        self.use_uploads({"status_code": "PUBLISHED"})
        result = await self.publish(self.draft)
        self.assertEqual(json.loads(result.content[0].text)["status"], drafts.STATUS_PUBLISHED)
        self.assert_no_upload_or_publish()

    async def test_published_draft_sends_nothing_even_if_the_preview_is_deleted(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, PUBLISHED)
        await self.publish(self.draft)
        (self.previews / NAME).unlink()
        self.use_uploads()
        result = await self.publish(self.draft)
        self.assertIn("already published", result.content[0].text)
        self.assert_no_network()

    async def test_uploaded_flag_needs_a_container(self):
        self.edit_saved_draft(self.draft, upload_complete=True)
        self.assert_load_refused(self.draft, "incomplete")
        self.edit_saved_draft(self.draft, upload_complete="yes")
        self.assert_load_refused(self.draft, "incomplete")

    async def test_success_results_never_contain_the_token(self):
        self.use_uploads(QUOTA_OK, CONTAINER, UPLOAD_OK, FINISHED, PUBLISHED)
        result = await self.publish(self.draft)
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.stored(self.draft)))


class UploadTestModeTestCase(LocalUploadTestCase):
    MODE = server.MODE_UPLOAD_TEST

    def setUp(self):
        super().setUp()
        self.use_uploads()

    async def run_test_tool(self, draft=None, dialog=None):
        draft = draft or self.draft
        dialog = dialog or self.person_types()
        async with Client(server.mcp, elicitation_callback=dialog) as client:
            return await client.call_tool(
                "test_instagram_local_upload",
                {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]},
            )

    def assert_nothing_published(self):
        self.assertEqual(self.opener.publish_calls(), [])
        for request in self.opener.requests:
            self.assertNotIn("media_publish", request.full_url)

    def assert_draft_unchanged(self):
        saved = self.stored(self.draft)
        self.assertEqual(saved, self.stored_before)
        self.assertEqual(saved["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(saved["container_id"])
        self.assertIs(saved["upload_complete"], False)
        self.assertIsNone(saved["confirmed_at"])
        self.assertIsNone(saved["media_id"])
        self.assertIsNone(saved["published_at"])
        self.assert_unlocked()


class UploadTestFlowTests(UploadTestModeTestCase):
    def setUp(self):
        super().setUp()
        self.stored_before = self.stored(self.draft)

    async def test_stops_after_processing_and_never_publishes(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED)
        result = await self.run_test_tool()
        self.assertFalse(result.is_error, result.content[0].text)
        body = json.loads(result.content[0].text)
        self.assertEqual(body["status"], "upload_test_passed")
        self.assertEqual(body["test_container_id"], "555")
        self.assertIs(body["published"], False)
        self.assertEqual(
            [(r.get_method(), r.full_url.split("?")[0]) for r in self.opener.requests],
            [
                ("POST", f"{server.GRAPH_API_ROOT}/{ACCOUNT}/media"),
                ("POST", "https://rupload.facebook.com/ig-api-upload/v26.0/555"),
                ("GET", f"{server.GRAPH_API_ROOT}/555"),
            ],
        )
        self.assert_nothing_published()
        self.assertEqual(self.opener.outcomes, [])
        self.assert_draft_unchanged()

    async def test_publish_code_is_never_called_and_can_not_run_in_this_mode(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED)
        with mock.patch.object(server, "publish_container") as publish, mock.patch.object(
            server, "run_publish_attempt"
        ) as attempt:
            self.assertFalse((await self.run_test_tool()).is_error)
        publish.assert_not_called()
        attempt.assert_not_called()
        draft = {**self.stored(self.draft), "container_id": "555"}
        with self.assertRaisesRegex(server.ToolError, "needs live mode"):
            server.publish_container(FAKE_TOKEN, draft, [])
        self.assert_nothing_published()
        self.assert_draft_unchanged()

    def test_test_code_has_no_reference_to_publishing(self):
        for function in (
            server.run_upload_test,
            server.upload_test_worker,
            server.test_instagram_local_upload,
            server.open_draft_for_upload_test,
        ):
            source = inspect.getsource(function)
            code = source.replace(inspect.getdoc(function) or "", "")
            for name in ("media_publish", "publish_container", "run_publish_attempt", "publish_worker"):
                self.assertNotIn(name, code)

    async def test_upload_uses_the_exact_file_and_documented_format(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED)
        await self.run_test_tool()
        self.assertEqual(json.loads(self.container_requests()[0].data)["upload_type"], "resumable")
        request = self.upload_requests()[0]
        self.assertEqual(request.get_header("Authorization"), f"OAuth {FAKE_TOKEN}")
        self.assertEqual(request.get_header("Offset"), "0")
        self.assertEqual(request.get_header("File_size"), str(len(VIDEO)))
        self.assertEqual(self.opener.bodies, [VIDEO])

    async def test_slow_processing_is_inconclusive_and_never_published(self):
        waiting = {"status_code": "IN_PROGRESS"}
        self.use_uploads(CONTAINER, UPLOAD_OK, *[waiting] * server.MAX_STATUS_CHECKS)
        result = await self.run_test_tool()
        body = json.loads(result.content[0].text)
        self.assertEqual(body["status"], "upload_test_inconclusive")
        self.assertEqual(body["container_status"], "IN_PROGRESS")
        self.assert_nothing_published()
        self.assert_draft_unchanged()

    async def test_processing_errors_are_reported_and_leave_the_draft_alone(self):
        for status_code in ("ERROR", "EXPIRED"):
            with self.subTest(status_code=status_code):
                self.use_uploads(CONTAINER, UPLOAD_OK, {"status_code": status_code, "status": f"x {FAKE_TOKEN}"})
                result = await self.run_test_tool()
                self.assert_refused(result, f"Nothing was published.*{status_code}")
                self.assertNotIn(FAKE_TOKEN, result.content[0].text)
                self.assert_nothing_published()
                self.assert_draft_unchanged()

    async def test_status_read_failure_is_reported(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, urllib.error.URLError("down"))
        self.assert_refused(await self.run_test_tool(), "status couldn't be read")
        self.assert_nothing_published()
        self.assert_draft_unchanged()

    async def test_upload_failures_make_one_attempt_and_leave_the_draft_alone(self):
        cases = {
            "final": upload_error(400, retriable=False),
            "retriable": upload_error(500, retriable=True),
            "timeout": TimeoutError(),
            "no success": {"success": False},
        }
        for label, outcome in cases.items():
            with self.subTest(label=label):
                self.use_uploads(CONTAINER, outcome)
                self.assert_refused(await self.run_test_tool(), "Nothing was published")
                self.assertEqual(len(self.upload_requests()), 1)
                self.assert_nothing_published()
                self.assert_draft_unchanged()

    async def test_container_failure_uploads_nothing(self):
        for outcome in (graph_error(403, 10), {"id": "../../evil"}):
            with self.subTest(outcome=str(outcome)):
                self.use_uploads(outcome)
                self.assert_refused(await self.run_test_tool(), "Nothing was uploaded")
                self.assertEqual(self.upload_requests(), [])
                self.assert_draft_unchanged()

    async def test_permission_refusal_does_not_show_the_token(self):
        self.use_uploads(CONTAINER, upload_error(403, message=f"bad {FAKE_TOKEN}"))
        result = await self.run_test_tool()
        self.assert_refused(result, "may not have access to resumable uploads")
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.stored(self.draft)))

    async def test_success_result_does_not_contain_the_token(self):
        self.use_uploads({"id": "555", "uri": f"https://x.example/{FAKE_TOKEN}"}, UPLOAD_OK, FINISHED)
        result = await self.run_test_tool()
        self.assertNotIn(FAKE_TOKEN, result.content[0].text)
        for request in self.opener.requests:
            self.assertNotIn("x.example", request.full_url)

    async def test_file_changes_stop_the_test(self):
        def change():
            (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")

        self.use_uploads(CONTAINER, after_container=change)
        self.assert_refused(await self.run_test_tool(), "Nothing was uploaded or published.*doesn't match")
        self.assertEqual(self.upload_requests(), [])
        self.add_preview()
        self.use_uploads(CONTAINER, UPLOAD_OK, before_upload=change)
        self.assert_refused(await self.run_test_tool(), "changed during the upload")
        self.assertNotIn(("GET", "555?fields=status_code,status"), self.opener.calls())
        self.assert_nothing_published()

    async def test_each_run_needs_its_own_phrase_and_container(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED)
        await self.run_test_tool()
        self.use_uploads({"id": "777"}, UPLOAD_OK, FINISHED)
        result = await self.run_test_tool()
        self.assertEqual(json.loads(result.content[0].text)["test_container_id"], "777")
        self.assertEqual(len(self.dialogs), 2)

    async def test_held_lock_stops_the_test(self):
        (self.drafts_dir / f"{self.draft['draft_id']}.lock").write_text("")
        self.addCleanup((self.drafts_dir / f"{self.draft['draft_id']}.lock").unlink, missing_ok=True)
        self.assert_refused(await self.run_test_tool(), "holds this draft")
        self.assert_no_network()


class UploadTestApprovalTests(UploadTestModeTestCase):
    def setUp(self):
        super().setUp()
        self.stored_before = self.stored(self.draft)

    def assert_nothing_sent(self):
        self.assert_no_network()
        self.assert_draft_unchanged()

    async def test_declining_or_cancelling_sends_nothing(self):
        for action in ("decline", "cancel"):
            with self.subTest(action=action):
                result = await self.run_test_tool(dialog=self.person_types(action=action))
                self.assert_refused(result, f"Nothing was uploaded.*{action}")
                self.assert_nothing_sent()

    async def test_wrong_phrases_send_nothing(self):
        code = self.draft["content_hash"][:8].upper()
        for typed in (
            "",
            "yes",
            "UPLOAD TEST",
            f"upload test {code.lower()}",
            f"UPLOAD TEST {code}0",
            "UPLOAD TEST 00000000",
            f"PUBLISH {code}",
        ):
            with self.subTest(typed=typed):
                result = await self.run_test_tool(dialog=self.person_types(typed))
                self.assert_refused(result, "Nothing was uploaded.*didn't match")
                self.assert_nothing_sent()

    async def test_client_without_a_dialog_fails_closed(self):
        async with Client(server.mcp) as client:
            with self.assertRaisesRegex(MCPError, "elicitation"):
                await client.call_tool(
                    "test_instagram_local_upload",
                    {"draft_id": self.draft["draft_id"], "content_hash": self.draft["content_hash"]},
                )
        self.assert_nothing_sent()

    async def test_dialog_says_the_file_is_really_sent_and_shows_it_exactly(self):
        await self.run_test_tool(dialog=self.person_types("no"))
        message = self.dialogs[0]
        self.assertTrue(message.startswith("UPLOAD TEST"))
        for expected in (
            "sends the file to Meta",
            "won't publish",
            NAME,
            str(len(VIDEO)),
            hashlib.sha256(VIDEO).hexdigest(),
            "Hello",
            self.draft["draft_id"],
            self.draft["content_hash"],
            f"type exactly: UPLOAD TEST {self.draft['content_hash'][:8].upper()}",
        ):
            self.assertIn(expected, message)
        self.assertNotIn("PUBLISH ", message)
        self.assertNotIn(FAKE_TOKEN, message)
        self.assert_nothing_sent()

    async def test_only_upload_test_mode_can_run_it(self):
        for mode in ("", "off", "LIVE", "upload-test", server.MODE_DRY_RUN, server.MODE_LIVE):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: mode}):
                self.assert_refused(await self.run_test_tool(), "must be set to upload_test")
        self.assertEqual(self.dialogs, [])
        self.assert_nothing_sent()

    async def test_missing_token_stops_before_the_dialog(self):
        with mock.patch.dict(os.environ):
            del os.environ[server.TOKEN_ENV_VAR]
            self.assert_refused(await self.run_test_tool(), "not set")
        self.assertEqual(self.dialogs, [])
        self.assert_nothing_sent()

    async def test_changed_or_deleted_file_stops_before_the_dialog(self):
        (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")
        self.assert_refused(await self.run_test_tool(), "doesn't match")
        (self.previews / NAME).unlink()
        self.assert_refused(await self.run_test_tool(), "isn't in reels/previews")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_file_changed_during_the_dialog_sends_nothing(self):
        confirm = self.person_types()

        async def change_then_confirm(context, params):
            (self.previews / NAME).write_bytes(b"PRETEND this is an mp4")
            return await confirm(context, params)

        self.assert_refused(await self.run_test_tool(dialog=change_then_confirm), "doesn't match")
        self.assert_no_network()

    async def test_url_draft_is_refused(self):
        draft = await self.prepare()
        self.assert_refused(await self.run_test_tool(draft), "only works with a local-file draft")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_draft_used_by_a_real_attempt_is_refused(self):
        for status, container in (
            (drafts.STATUS_CONTAINER_CREATED, "111"),
            (drafts.STATUS_PUBLISH_UNKNOWN, "111"),
            (drafts.STATUS_PUBLISHED, "111"),
            (drafts.STATUS_FAILED, None),
        ):
            with self.subTest(status=status):
                self.edit_saved_draft(self.draft, status=status, container_id=container)
                self.assert_refused(await self.run_test_tool(), f"status is {status}")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_wrong_hash_is_refused(self):
        wrong = {**self.draft, "content_hash": "0" * 64}
        self.assert_refused(await self.run_test_tool(wrong), "doesn't match this draft")
        self.assertEqual(self.dialogs, [])
        self.assert_nothing_sent()

    async def test_phrase_can_not_be_passed_as_an_argument(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        tool = tools["test_instagram_local_upload"]
        self.assertEqual(set(tool.input_schema["properties"]), {"draft_id", "content_hash"})
        self.assertFalse(tool.annotations.destructive_hint)
        self.assertTrue(tool.annotations.open_world_hint)


class UploadTestSeparationTests(UploadTestModeTestCase):
    async def test_publish_tool_is_off_in_upload_test_mode(self):
        self.assert_refused(await self.publish(self.draft), "Publishing is off")
        url_draft = await self.prepare()
        self.assert_refused(await self.publish(url_draft), "Publishing is off")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_tested_draft_needs_the_publish_phrase_and_a_new_container_later(self):
        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED)
        await self.run_test_tool()
        tested_phrase = f"UPLOAD TEST {self.draft['content_hash'][:8].upper()}"
        self.use_uploads()
        with mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: server.MODE_LIVE}):
            result = await self.publish(self.draft, self.person_types(tested_phrase))
            self.assert_refused(result, "didn't match")
            self.assert_no_network()

            self.use_uploads(QUOTA_OK, {"id": "777"}, UPLOAD_OK, FINISHED, PUBLISHED)
            self.assertFalse((await self.publish(self.draft)).is_error)
        publish = [r for r in self.opener.requests if r.full_url.endswith("/media_publish")]
        self.assertEqual([json.loads(r.data) for r in publish], [{"creation_id": "777"}])
        for request in self.opener.requests:
            self.assertNotIn("/555", request.full_url)
        self.assertEqual(self.opener.bodies, [VIDEO])

    async def test_changing_mode_during_the_test_can_not_publish(self):
        def go_live():
            os.environ[server.PUBLISHING_MODE_ENV_VAR] = server.MODE_LIVE

        self.use_uploads(CONTAINER, UPLOAD_OK, FINISHED, before_upload=go_live)
        with mock.patch.dict(os.environ):
            body = json.loads((await self.run_test_tool()).content[0].text)
        self.assertEqual(body["status"], "upload_test_passed")
        self.assert_nothing_published()

    def test_existing_publish_phrase_is_unchanged(self):
        self.assertEqual(drafts.confirmation_phrase({"content_hash": "abcdef0123"}), "PUBLISH ABCDEF01")
        self.assertEqual(drafts.upload_test_phrase({"content_hash": "abcdef0123"}), "UPLOAD TEST ABCDEF01")


class LoopbackUploadTests(unittest.TestCase):
    """Sends a real HTTP request through urllib, but only to a server on this computer."""

    def setUp(self):
        received = self.received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received.update(
                    path=self.path,
                    body=self.rfile.read(length),
                    authorization=self.headers["Authorization"],
                    offset=self.headers["offset"],
                    file_size=self.headers["file_size"],
                    transfer_encoding=self.headers["Transfer-Encoding"],
                )
                body = json.dumps({"success": True, "message": "Upload successful."}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments):
                pass

        self.http_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.http_server.server_close)
        self.addCleanup(self.http_server.shutdown)
        threading.Thread(target=self.http_server.serve_forever, daemon=True).start()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.previews = Path(temporary.name)
        root = f"http://127.0.0.1:{self.http_server.server_address[1]}/ig-api-upload/v26.0"
        for patcher in (
            mock.patch.object(drafts, "PREVIEWS_DIR", self.previews),
            mock.patch.object(server, "RUPLOAD_API_ROOT", root),
            mock.patch.dict(os.environ, {"NO_PROXY": "*", "no_proxy": "*"}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_whole_file_is_streamed_with_the_documented_headers(self):
        data = os.urandom(3 * 1024 * 1024 + 17)
        (self.previews / NAME).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        (self.previews / NAME).with_suffix(".json").write_text(
            json.dumps(
                {
                    "status": "awaiting_review",
                    "preview_file": NAME,
                    "preview_sha256": digest,
                    "problems": [],
                }
            )
        )
        draft = {"local_file": {"name": NAME, "sha256": digest, "size": len(data)}}
        server.upload_local_reel(FAKE_TOKEN, draft, "555")
        self.assertEqual(self.received["path"], "/ig-api-upload/v26.0/555")
        self.assertEqual(self.received["body"], data)
        self.assertEqual(self.received["authorization"], f"OAuth {FAKE_TOKEN}")
        self.assertEqual(self.received["offset"], "0")
        self.assertEqual(self.received["file_size"], str(len(data)))
        self.assertIsNone(self.received["transfer_encoding"])


class PrepareToolTests(LocalDraftTestCase):
    async def call(self, **arguments):
        result = await server.mcp.call_tool("prepare_instagram_local_reel", arguments)
        return json.loads(result.content[0].text)

    def saved_drafts(self):
        return [path for path in self.drafts_dir.iterdir() if path.suffix == ".json"]

    async def assert_invalid(self, pattern, **arguments):
        before = self.saved_drafts()
        body = await self.call(**arguments)
        self.assertIs(body["valid"], False)
        self.assertIsNone(body["draft_id"])
        self.assertRegex(" ".join(body["errors"]), pattern)
        self.assertEqual(self.saved_drafts(), before)
        self.assert_no_network()

    async def test_valid_preview_becomes_a_bound_prepared_draft(self):
        self.add_preview()
        body = await self.call(file_name=NAME, caption="Hello #ShambhalaTech")
        self.assertIs(body["valid"], True)
        self.assertEqual(body["status"], drafts.STATUS_PREPARED)
        self.assertEqual(body["post_type"], drafts.POST_TYPE_REEL)
        self.assertEqual(body["media_source"], drafts.MEDIA_SOURCE_LOCAL_FILE)
        self.assertIs(body["share_to_feed"], True)
        self.assertEqual(
            body["local_file"],
            {"name": NAME, "sha256": hashlib.sha256(VIDEO).hexdigest(), "size": len(VIDEO)},
        )
        self.assertEqual(body["caption_stats"]["hashtags"], 1)
        self.assertIn("Nothing has been uploaded", body["next_step"])
        saved = self.stored(body)
        self.assertEqual(saved["content_hash"], body["content_hash"])
        self.assertEqual(self.load(body)["local_file"], body["local_file"])
        self.assert_no_network()

    async def test_share_to_feed_can_be_turned_off_and_changes_the_hash(self):
        self.add_preview()
        first = await self.call(file_name=NAME, share_to_feed=True)
        second = await self.call(file_name=NAME, share_to_feed=False)
        self.assertIs(second["share_to_feed"], False)
        self.assertNotEqual(first["content_hash"], second["content_hash"])
        self.assertNotEqual(first["draft_id"], second["draft_id"])

    async def test_empty_caption_is_only_a_warning(self):
        self.add_preview()
        body = await self.call(file_name=NAME)
        self.assertIs(body["valid"], True)
        self.assertIn("empty", body["warnings"][0])

    async def test_paths_and_other_file_types_are_refused(self):
        self.add_preview()
        for name in ("../secret.mp4", "..\\secret.mp4", "C:\\clips\\clip.mp4", "clip.mov", "clip.json", ""):
            with self.subTest(name=name):
                await self.assert_invalid("reels/previews", file_name=name)

    async def test_missing_preview_is_refused(self):
        await self.assert_invalid("isn't in reels/previews", file_name=NAME)

    async def test_record_must_be_awaiting_review_and_match(self):
        cases = (
            ({"status": "needs_attention"}, "status isn't awaiting_review"),
            ({"preview_file": "other.mp4"}, "different file"),
            ({"problems": ["bad"]}, "checks passed"),
            ({"preview_sha256": "0" * 64}, "doesn't match"),
        )
        for changes, pattern in cases:
            with self.subTest(changes=changes):
                self.add_preview(**changes)
                await self.assert_invalid(pattern, file_name=NAME)

    async def test_missing_record_is_refused(self):
        self.add_preview()
        (self.previews / NAME).with_suffix(".json").unlink()
        await self.assert_invalid("no review record", file_name=NAME)

    async def test_oversized_file_is_refused(self):
        self.add_preview()
        with mock.patch.object(drafts, "MAX_REEL_FILE_BYTES", len(VIDEO) - 1):
            await self.assert_invalid("300 MB", file_name=NAME)

    async def test_bad_caption_is_refused_without_repeating_it(self):
        self.add_preview()
        caption = "private text " + "a" * 2201
        await self.assert_invalid("characters", file_name=NAME, caption=caption)
        body = await self.call(file_name=NAME, caption=caption)
        self.assertNotIn("private text", json.dumps(body))

    async def test_prepared_draft_needs_confirmation_before_anything_is_sent(self):
        self.add_preview()
        body = await self.call(file_name=NAME, caption="Hello")
        for mode in (server.MODE_LIVE, server.MODE_DRY_RUN):
            with self.subTest(mode=mode), mock.patch.dict(os.environ, {server.PUBLISHING_MODE_ENV_VAR: mode}):
                self.assert_refused(await self.publish(body, self.person_types("no")), "didn't match")
        self.assertEqual(len(self.dialogs), 2)
        self.assert_no_network()
        self.assertEqual(self.stored(body)["status"], drafts.STATUS_PREPARED)
        self.assertIsNone(self.stored(body)["container_id"])

    async def test_works_with_no_token_or_publishing_mode_set(self):
        self.add_preview()
        with mock.patch.dict(os.environ):
            del os.environ[server.TOKEN_ENV_VAR]
            del os.environ[server.PUBLISHING_MODE_ENV_VAR]
            self.assertIs((await self.call(file_name=NAME))["valid"], True)
        self.assert_no_network()

    async def test_does_not_read_the_token(self):
        self.add_preview()
        source = inspect.getsource(server.prepare_instagram_local_reel)
        for name in ("environ", "read_token", "TOKEN_ENV_VAR", "graph_request"):
            self.assertNotIn(name, source)
        with mock.patch.object(server, "read_token") as read_token:
            await self.call(file_name=NAME)
        read_token.assert_not_called()
        self.assertNotIn(FAKE_TOKEN, json.dumps(self.stored(await self.call(file_name=NAME))))

    async def test_token_in_a_caption_is_still_caught_by_the_publish_gate(self):
        self.add_preview()
        body = await self.call(file_name=NAME, caption=f"oops {FAKE_TOKEN}")
        self.assert_refused(await self.publish(body), "no longer passes validation")
        self.assertEqual(self.dialogs, [])
        self.assert_no_network()

    async def test_tool_definition_is_offline_and_not_destructive(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        tool = tools["prepare_instagram_local_reel"]
        self.assertFalse(tool.annotations.destructive_hint)
        self.assertFalse(tool.annotations.open_world_hint)
        self.assertEqual(set(tool.input_schema["properties"]), {"file_name", "caption", "share_to_feed"})
        self.assertEqual(tool.input_schema["required"], ["file_name"])


class NoNetworkTests(LocalDraftTestCase):
    def test_draft_code_has_no_network_imports_or_addresses(self):
        source = Path(drafts.__file__).read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        forbidden = {"socket", "http", "http.client", "urllib.request", "requests", "httpx", "ftplib"}
        self.assertEqual(imported & forbidden, set())
        self.assertNotIn("rupload", source)
