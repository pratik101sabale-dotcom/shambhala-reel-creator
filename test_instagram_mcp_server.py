"""Offline tests for instagram_mcp_server.py, using mocked API responses only.

Every test replaces urllib's opener with a fake and uses a fake token, so nothing
here can reach Meta or read a real token. Run with:

    .\\.venv\\Scripts\\python.exe -m unittest -v
"""

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

import instagram_mcp_server as server
from mcp.server.mcpserver.exceptions import ToolError

FAKE_TOKEN = "FAKE-TOKEN-abc123"
ENGAGEMENT_TOOL = "get_instagram_post_engagement"


def http_error(status, body):
    """Build an HTTPError whose body is the given value, encoded as JSON."""
    return urllib.error.HTTPError(
        "https://example.invalid", status, "error", {}, io.BytesIO(json.dumps(body).encode())
    )


def graph_error(status, code, message="refused"):
    return http_error(status, {"error": {"type": "OAuthException", "code": code, "message": message}})


class FakeResponse(io.BytesIO):
    """A response body that also works as a context manager, like urllib's."""


class FakeOpener:
    """Returns or raises the queued outcomes in order, and records each request."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(json.dumps(outcome).encode())


class ToolTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {server.TOKEN_ENV_VAR: FAKE_TOKEN})
        env.start()
        self.addCleanup(env.stop)

    def use_outcomes(self, *outcomes):
        opener = FakeOpener(outcomes)
        for outcome in outcomes:
            if isinstance(outcome, urllib.error.HTTPError):
                self.addCleanup(outcome.close)
        patcher = mock.patch.object(server.urllib.request, "build_opener", return_value=opener)
        patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    async def call(self, name, **arguments):
        """Call a tool the way a client would and return its text."""
        result = await server.mcp.call_tool(name, arguments)
        return result.content[0].text

    def assert_safe_requests(self, opener):
        """Every request must be a GET, with the token only in the header."""
        for request in opener.requests:
            self.assertEqual(request.get_method(), "GET")
            self.assertNotIn(FAKE_TOKEN, request.full_url)
            self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_TOKEN}")


class ToolRegistrationTests(ToolTestCase):
    async def test_the_three_original_tools_are_registered_read_only(self):
        tools = {tool.name: tool for tool in await server.mcp.list_tools()}
        for name in ("get_instagram_profile", "get_recent_instagram_posts", ENGAGEMENT_TOOL):
            self.assertIn(name, tools)
            self.assertTrue(tools[name].annotations.read_only_hint)
            self.assertFalse(tools[name].annotations.destructive_hint)


class ExistingToolTests(ToolTestCase):
    async def test_profile_returns_the_api_response(self):
        opener = self.use_outcomes({"id": "1", "username": "shambhala"})
        data = json.loads(await self.call("get_instagram_profile"))
        self.assertEqual(data, {"id": "1", "username": "shambhala"})
        self.assertEqual(len(opener.requests), 1)
        self.assert_safe_requests(opener)

    async def test_recent_posts_keep_their_fields_and_shape(self):
        opener = self.use_outcomes(
            {
                "data": [{"id": "1", "timestamp": "t", "permalink": "p", "like_count": 9}],
                "paging": {"next": f"https://example.invalid?access_token={FAKE_TOKEN}"},
            }
        )
        posts = json.loads(await self.call("get_recent_instagram_posts", limit=5))
        self.assertEqual(
            posts, [{"id": "1", "caption": None, "timestamp": "t", "permalink": "p"}]
        )
        self.assertIn("fields=id,caption,timestamp,permalink&limit=5", opener.requests[0].full_url)
        self.assertEqual(len(opener.requests), 1)
        self.assert_safe_requests(opener)

    async def test_recent_posts_do_not_retry_on_a_permission_error(self):
        opener = self.use_outcomes(graph_error(403, 10, "denied"))
        with self.assertRaisesRegex(ToolError, "HTTP 403.*code: 10.*denied"):
            await self.call("get_recent_instagram_posts")
        self.assertEqual(len(opener.requests), 1)


class EngagementToolTests(ToolTestCase):
    async def test_returns_counts_and_keeps_zero_distinct_from_null(self):
        opener = self.use_outcomes(
            {
                "data": [
                    {
                        "id": "1",
                        "media_type": "VIDEO",
                        "timestamp": "t",
                        "permalink": "p",
                        "like_count": 0,
                        "comments_count": 3,
                    }
                ],
                "paging": {"next": f"https://example.invalid?access_token={FAKE_TOKEN}"},
            }
        )
        text = await self.call(ENGAGEMENT_TOOL, limit=5)
        self.assertEqual(
            json.loads(text),
            {
                "posts": [
                    {
                        "id": "1",
                        "media_type": "VIDEO",
                        "timestamp": "t",
                        "permalink": "p",
                        "like_count": 0,
                        "comments_count": 3,
                    }
                ],
                "warnings": [],
            },
        )
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertEqual(len(opener.requests), 1)
        self.assertIn("like_count,comments_count&limit=5", opener.requests[0].full_url)
        self.assert_safe_requests(opener)

    async def test_omitted_count_becomes_null_with_a_warning(self):
        self.use_outcomes(
            {
                "data": [
                    {"id": "1", "comments_count": 2},
                    {"id": "2", "like_count": 4, "comments_count": 1},
                ]
            }
        )
        data = json.loads(await self.call(ENGAGEMENT_TOOL))
        self.assertIsNone(data["posts"][0]["like_count"])
        self.assertEqual(data["posts"][1]["like_count"], 4)
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("like_count is null on 1 post(s)", data["warnings"][0])

    async def test_unsupported_field_retries_once_without_counts(self):
        opener = self.use_outcomes(
            graph_error(400, 100, "Tried accessing nonexisting field (like_count)"),
            {"data": [{"id": "1", "media_type": "VIDEO", "timestamp": "t", "permalink": "p"}]},
        )
        data = json.loads(await self.call(ENGAGEMENT_TOOL, limit=1))
        self.assertIsNone(data["posts"][0]["like_count"])
        self.assertIsNone(data["posts"][0]["comments_count"])
        self.assertEqual(data["posts"][0]["id"], "1")
        self.assertEqual(len(data["warnings"]), 1)
        self.assertIn("nonexisting field", data["warnings"][0])
        self.assertNotIn("permissions", data["warnings"][0])
        self.assertEqual(len(opener.requests), 2)
        self.assertNotIn("like_count", opener.requests[1].full_url)
        self.assert_safe_requests(opener)

    async def test_missing_permission_for_counts_returns_posts_with_a_hint(self):
        for code in (3, 10, 200, 299):
            with self.subTest(code=code):
                opener = self.use_outcomes(
                    graph_error(403, code), {"data": [{"id": "1"}]}
                )
                data = json.loads(await self.call(ENGAGEMENT_TOOL))
                self.assertIsNone(data["posts"][0]["like_count"])
                self.assertIn("pages_read_engagement", data["warnings"][0])
                self.assertEqual(len(opener.requests), 2)

    async def test_missing_permission_on_both_requests_is_a_clear_error(self):
        opener = self.use_outcomes(graph_error(403, 10), graph_error(403, 10))
        with self.assertRaisesRegex(ToolError, "HTTP 403.*pages_read_engagement"):
            await self.call(ENGAGEMENT_TOOL)
        self.assertEqual(len(opener.requests), 2)

    async def test_expired_token_and_rate_limit_are_not_retried(self):
        for status, code in ((401, 190), (403, 4)):
            with self.subTest(code=code):
                opener = self.use_outcomes(graph_error(status, code, "stop"))
                with self.assertRaisesRegex(ToolError, f"code: {code}.*stop"):
                    await self.call(ENGAGEMENT_TOOL)
                self.assertEqual(len(opener.requests), 1)

    async def test_unreadable_error_body_reports_only_the_status(self):
        opener = self.use_outcomes(http_error(500, ["not", "an", "error", "object"]))
        with self.assertRaisesRegex(ToolError, r"returned HTTP 500\.$"):
            await self.call(ENGAGEMENT_TOOL)
        self.assertEqual(len(opener.requests), 1)

    async def test_non_object_error_field_reports_only_the_status(self):
        self.use_outcomes(http_error(500, {"error": "boom"}))
        with self.assertRaisesRegex(ToolError, r"returned HTTP 500\.$"):
            await self.call(ENGAGEMENT_TOOL)

    async def test_token_in_an_api_message_is_redacted_in_warnings_and_errors(self):
        leak = f"bad token {FAKE_TOKEN}"
        self.use_outcomes(graph_error(400, 100, leak), {"data": [{"id": "1"}]})
        text = await self.call(ENGAGEMENT_TOOL)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("[REDACTED]", text)

        self.use_outcomes(graph_error(401, 190, leak))
        with self.assertRaises(ToolError) as caught:
            await self.call(ENGAGEMENT_TOOL)
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))
        self.assertIn("[REDACTED]", str(caught.exception))

    async def test_malformed_response_is_an_error(self):
        self.use_outcomes({"data": "not a list"})
        with self.assertRaisesRegex(ToolError, "unexpected format"):
            await self.call(ENGAGEMENT_TOOL)

    async def test_network_failure_is_an_error(self):
        self.use_outcomes(urllib.error.URLError("offline"))
        with self.assertRaisesRegex(ToolError, "Could not reach"):
            await self.call(ENGAGEMENT_TOOL)

    async def test_limit_is_validated_before_any_request(self):
        opener = self.use_outcomes()
        for limit in (0, 11):
            with self.subTest(limit=limit), self.assertRaises(ToolError):
                await self.call(ENGAGEMENT_TOOL, limit=limit)
        self.assertEqual(opener.requests, [])

    async def test_missing_token_is_an_error_before_any_request(self):
        opener = self.use_outcomes()
        with mock.patch.dict(os.environ, {server.TOKEN_ENV_VAR: ""}):
            with self.assertRaisesRegex(ToolError, "is not set"):
                await self.call(ENGAGEMENT_TOOL)
        self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
