"""Local MCP server for one Instagram account (stdio transport).

Three tools only read: get_instagram_profile, get_recent_instagram_posts, and
get_instagram_post_engagement. Each call sends one GET request to the Instagram
Graph API, except that get_instagram_post_engagement sends a second GET request
if Meta refuses the engagement fields.

Three tools handle publishing. prepare_instagram_draft validates content and saves
a local draft without using the network. prepare_instagram_local_reel does the same
for a Reel rendered in reels/previews. publish_instagram_draft is the only code that
can publish, and only when all of these are true:

- IG_PUBLISHING_MODE is "live". It is off by default.
- The draft is unchanged and matches the content_hash in the request.
- A person typed the draft's confirmation phrase into a dialog shown by the MCP
  client. The phrase is requested on every attempt and isn't a tool argument, so
  the AI assistant calling the tool can't supply it.

test_instagram_local_upload sends a local-file draft to Meta to prove the token and
upload work, then stops. It only runs when IG_PUBLISHING_MODE is "upload_test", needs
its own typed phrase, and has no code path to media_publish.

The access token comes only from the IG_ACCESS_TOKEN environment variable. It
travels in the Authorization header, so it never appears in a URL, and it is
removed from all text returned to the client.

stdout carries the MCP protocol, so this file must never print to it.
"""

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from typing import Annotated, Literal

import instagram_drafts as drafts
from instagram_config import ACCOUNT_ENV_VAR, configured_account_id
from mcp.server.mcpserver import Elicit, ElicitationResult, MCPServer, Resolve
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

TOKEN_ENV_VAR = "IG_ACCESS_TOKEN"
PUBLISHING_MODE_ENV_VAR = "IG_PUBLISHING_MODE"
MODE_DRY_RUN = "dry_run"
MODE_LIVE = "live"
# Allows test_instagram_local_upload only. publish_instagram_draft treats it as off.
MODE_UPLOAD_TEST = "upload_test"
GRAPH_API_ROOT = "https://graph.facebook.com/v26.0"
# Meta's host for local video files. The token is only ever sent to this fixed address,
# never to the upload address in a response.
RUPLOAD_API_ROOT = "https://rupload.facebook.com/ig-api-upload/v26.0"
INSTAGRAM_ACCOUNT_ID = configured_account_id() or "0"
PROFILE_FIELDS = (
    "id,username,name,biography,website,profile_picture_url,"
    "followers_count,follows_count,media_count"
)
POST_FIELDS = ("id", "caption", "timestamp", "permalink")
ENGAGEMENT_BASE_FIELDS = ("id", "media_type", "timestamp", "permalink")
ENGAGEMENT_COUNT_FIELDS = ("like_count", "comments_count")
ENGAGEMENT_FIELDS = ENGAGEMENT_BASE_FIELDS + ENGAGEMENT_COUNT_FIELDS
# Graph API error codes that mean the token lacks access: 3 (capability),
# 10 and 200-299 (permissions). 100 (invalid parameter) covers unsupported fields.
PERMISSION_ERROR_CODES = {3, 10, *range(200, 300)}
UNSUPPORTED_FIELD_ERROR_CODE = 100
PERMISSION_HINT = (
    " The token may need the instagram_basic and pages_read_engagement permissions."
)
MAX_POSTS = 10
TIMEOUT_SECONDS = 15
# Publishing requests can take longer than reads because Meta fetches the media.
PUBLISH_TIMEOUT_SECONDS = 60
# Applies to each network read or write during an upload, not to the whole upload.
UPLOAD_TIMEOUT_SECONDS = 300
# Meta recommends checking a container once per minute for no more than 5 minutes.
STATUS_CHECK_INTERVAL_SECONDS = 60
MAX_STATUS_CHECKS = 6
PUBLISH_PERMISSION_HINT = (
    " The token may need the instagram_basic, instagram_content_publish and "
    "pages_read_engagement permissions."
)
# A refusal with these codes may not be final, so media_publish may have worked.
AMBIGUOUS_PUBLISH_ERROR_CODES = {-2, -1, 1, 2}

# WARNING keeps routine request details out of the server's stderr log.
mcp = MCPServer("shambhala-instagram", log_level="WARNING")


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, because urllib would resend the token to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_token():
    """Return the token from the environment, or raise a ToolError that omits it."""
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise ToolError(f"{TOKEN_ENV_VAR} is not set in the server's environment.")
    # Malformed values are rejected here because HTTP libraries can quote a bad
    # header value in their error messages.
    if not all(33 <= ord(char) <= 126 for char in token):
        raise ToolError(f"{TOKEN_ENV_VAR} contains invalid characters.")
    if INSTAGRAM_ACCOUNT_ID == "0":
        raise ToolError(f"Set {ACCOUNT_ENV_VAR} to your own numeric Instagram professional account ID before starting the server.")
    return token


def redact(value, token):
    """Remove the token from every string inside a decoded JSON value."""
    if isinstance(value, str):
        return value.replace(token, "[REDACTED]")
    if isinstance(value, dict):
        return {key: redact(item, token) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    return value


def read_error_details(error):
    """Return the Graph API error object, or {} if the body can't be understood.

    An HTTPError body can only be read once, so call this at most once per error.
    """
    try:
        details = json.loads(error.read().decode("utf-8")).get("error", {})
    except (ValueError, AttributeError, OSError):
        return {}
    return details if isinstance(details, dict) else {}


def format_error_details(status, details, token):
    """Build an error message from known Graph API fields, never the raw response."""
    message = f"Instagram Graph API returned HTTP {status}."
    for label, key in (("type", "type"), ("code", "code"), ("message", "message")):
        if key in details:
            message += f" {label}: {redact(str(details[key]), token)}"
    return message


def describe_http_error(error, token):
    """Build the error message for an HTTPError."""
    return format_error_details(error.code, read_error_details(error), token)


def fetch_profile(token):
    """Send the single GET request and return the decoded JSON profile."""
    request = urllib.request.Request(
        f"{GRAPH_API_ROOT}/{INSTAGRAM_ACCOUNT_ID}?fields={PROFILE_FIELDS}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirects)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_media_page(token, fields, limit):
    """Send one GET request for the given media fields and return the decoded page."""
    request = urllib.request.Request(
        f"{GRAPH_API_ROOT}/{INSTAGRAM_ACCOUNT_ID}/media"
        f"?fields={','.join(fields)}&limit={limit}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirects)
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_recent_posts(token, limit):
    """Send the single GET request and return the decoded JSON media page."""
    return fetch_media_page(token, POST_FIELDS, limit)


def is_permission_error(details):
    """Return True if a Graph API error object says the token lacks access."""
    return details.get("code") in PERMISSION_ERROR_CODES


def fetch_engagement_page(token, limit):
    """Return (media page, warning), retrying without counts if Meta refuses them.

    The warning is None when the counts were requested successfully. Other errors,
    such as an expired token or a rate limit, are not retried.
    """
    try:
        return fetch_media_page(token, ENGAGEMENT_FIELDS, limit), None
    except urllib.error.HTTPError as error:
        details = read_error_details(error)
        message = format_error_details(error.code, details, token)
        if is_permission_error(details):
            message += PERMISSION_HINT
        elif details.get("code") != UNSUPPORTED_FIELD_ERROR_CODE:
            raise ToolError(message) from None
    page = fetch_media_page(token, ENGAGEMENT_BASE_FIELDS, limit)
    return page, f"Engagement counts are unavailable, so they are null. {message}"


def select_post_fields(page, limit, fields=POST_FIELDS):
    """Keep only the expected fields, or return None if the page is malformed.

    This drops the page's "paging" block, whose links can contain an access token.
    Instagram omits fields it has no value for, such as "caption" on posts without
    one, so they become null here.
    """
    posts = page.get("data") if isinstance(page, dict) else None
    if not isinstance(posts, list) or not all(isinstance(post, dict) for post in posts):
        return None
    return [{field: post.get(field) for field in fields} for post in posts[:limit]]


def describe_missing_counts(posts):
    """Return one warning for each count that Instagram omitted from some posts."""
    warnings = []
    for field in ENGAGEMENT_COUNT_FIELDS:
        missing = sum(post[field] is None for post in posts)
        if missing:
            warnings.append(
                f"{field} is null on {missing} post(s) because Instagram didn't "
                "return it. This can happen when the owner hides like counts or "
                "the media type doesn't support the field."
            )
    return warnings


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Instagram profile",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
    structured_output=False,
)
async def get_instagram_profile() -> str:
    """Read the Instagram account's public profile fields and counts as JSON.

    Returns id, username, name, biography, website, profile_picture_url,
    followers_count, follows_count, and media_count. Read-only.
    """
    token = read_token()
    # "from None" and the generic messages below keep the original exception,
    # whose text could include request details, out of errors and logs.
    try:
        profile = await asyncio.to_thread(fetch_profile, token)
    except urllib.error.HTTPError as error:
        raise ToolError(describe_http_error(error, token)) from None
    except (urllib.error.URLError, TimeoutError):
        raise ToolError(
            "Could not reach the Instagram Graph API (network error or timeout)."
        ) from None
    except ValueError:
        raise ToolError("The Instagram Graph API response was not valid JSON.") from None
    except Exception as error:
        raise ToolError(
            f"Unexpected {type(error).__name__} while reading the profile."
        ) from None
    return json.dumps(redact(profile, token), ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get recent Instagram posts",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
    structured_output=False,
)
async def get_recent_instagram_posts(
    limit: Annotated[
        int, Field(ge=1, le=MAX_POSTS, description="How many recent posts to return.")
    ] = MAX_POSTS,
) -> str:
    """Read the Instagram account's most recent posts as a JSON array.

    Each post has id, caption, timestamp, and permalink. caption is null if the
    post has no caption. Returns up to 10 posts. Read-only.
    """
    token = read_token()
    # "from None" and the generic messages below keep the original exception,
    # whose text could include request details, out of errors and logs.
    try:
        page = await asyncio.to_thread(fetch_recent_posts, token, limit)
    except urllib.error.HTTPError as error:
        raise ToolError(describe_http_error(error, token)) from None
    except (urllib.error.URLError, TimeoutError):
        raise ToolError(
            "Could not reach the Instagram Graph API (network error or timeout)."
        ) from None
    except ValueError:
        raise ToolError("The Instagram Graph API response was not valid JSON.") from None
    except Exception as error:
        raise ToolError(
            f"Unexpected {type(error).__name__} while reading the posts."
        ) from None
    posts = select_post_fields(page, limit)
    if posts is None:
        raise ToolError("The Instagram Graph API response had an unexpected format.")
    return json.dumps(redact(posts, token), ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Get Instagram post engagement",
        read_only_hint=True,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=True,
    ),
    structured_output=False,
)
async def get_instagram_post_engagement(
    limit: Annotated[
        int, Field(ge=1, le=MAX_POSTS, description="How many recent posts to return.")
    ] = MAX_POSTS,
) -> str:
    """Read like and comment counts for the account's most recent posts as JSON.

    Returns an object with "posts" and "warnings". Each post has id, media_type,
    timestamp, permalink, like_count, and comments_count. A count is null if
    Instagram doesn't provide it, and "warnings" explains why. Returns up to 10
    posts. Read-only.
    """
    token = read_token()
    # "from None" and the generic messages below keep the original exception,
    # whose text could include request details, out of errors and logs.
    try:
        page, refusal = await asyncio.to_thread(fetch_engagement_page, token, limit)
    except ToolError:
        raise
    except urllib.error.HTTPError as error:
        details = read_error_details(error)
        message = format_error_details(error.code, details, token)
        if is_permission_error(details):
            message += PERMISSION_HINT
        raise ToolError(message) from None
    except (urllib.error.URLError, TimeoutError):
        raise ToolError(
            "Could not reach the Instagram Graph API (network error or timeout)."
        ) from None
    except ValueError:
        raise ToolError("The Instagram Graph API response was not valid JSON.") from None
    except Exception as error:
        raise ToolError(
            f"Unexpected {type(error).__name__} while reading the engagement data."
        ) from None
    posts = select_post_fields(page, limit, ENGAGEMENT_FIELDS)
    if posts is None:
        raise ToolError("The Instagram Graph API response had an unexpected format.")
    # After a refusal every count is null, so per-field warnings would only repeat it.
    warnings = [refusal] if refusal else describe_missing_counts(posts)
    result = {"posts": posts, "warnings": warnings}
    return json.dumps(redact(result, token), ensure_ascii=False, indent=2)


class GraphRequestFailed(Exception):
    """A publishing request failed. The message is redacted and safe to show."""

    def __init__(self, message, http_status=None, details=None):
        super().__init__(message)
        self.http_status = http_status
        self.details = details or {}

    def may_have_published(self):
        """Return False only if Meta clearly refused a media_publish request."""
        return (
            self.http_status is None
            or self.http_status >= 500
            or not self.details
            or self.details.get("code") in AMBIGUOUS_PUBLISH_ERROR_CODES
            or self.details.get("is_transient") is True
        )


class UploadFailed(Exception):
    """A local upload failed. The message is redacted and safe to show.

    retriable is False only if Meta said that trying again can't help.
    """

    def __init__(self, message, retriable=None):
        super().__init__(message)
        self.retriable = retriable


class PublishConfirmation(BaseModel):
    """The form shown to a person by the MCP client."""

    confirmation: str = Field(description="Type the exact phrase shown above.")


def graph_request(token, path, payload=None):
    """Send one GET, or POST if payload is given, and return the decoded JSON object.

    Every failure becomes a GraphRequestFailed that can't contain the token.
    """
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{GRAPH_API_ROOT}/{path}",
        data=data,
        headers=headers,
        method="GET" if data is None else "POST",
    )
    opener = urllib.request.build_opener(NoRedirects)
    # "from None" and the generic messages keep the original exception, whose text
    # could include request details, out of errors and logs.
    try:
        with opener.open(request, timeout=PUBLISH_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        details = read_error_details(error)
        message = format_error_details(error.code, details, token)
        for label, key in (("subcode", "error_subcode"), ("detail", "error_user_msg")):
            if key in details:
                message += f" {label}: {redact(str(details[key]), token)}"
        if is_permission_error(details):
            message += PUBLISH_PERMISSION_HINT
        raise GraphRequestFailed(message, error.code, details) from None
    except (urllib.error.URLError, TimeoutError):
        raise GraphRequestFailed(
            "Could not reach the Instagram Graph API (network error or timeout)."
        ) from None
    except ValueError:
        raise GraphRequestFailed(
            "The Instagram Graph API response was not valid JSON."
        ) from None
    except Exception as error:
        raise GraphRequestFailed(
            f"Unexpected {type(error).__name__} during the Instagram request."
        ) from None
    if not isinstance(body, dict):
        raise GraphRequestFailed("The Instagram Graph API response had an unexpected format.")
    return body


def build_container_payload(draft):
    """Return the POST /media body, following Meta's content publishing guide."""
    if drafts.is_local_file_draft(draft):
        payload = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "share_to_feed": draft["share_to_feed"],
        }
    elif draft["post_type"] == drafts.POST_TYPE_REEL:
        payload = {
            "media_type": "REELS",
            "video_url": draft["media_url"],
            "share_to_feed": draft["share_to_feed"],
        }
    else:
        # Meta's image examples send image_url with no media_type.
        payload = {"image_url": draft["media_url"]}
    if draft["caption"]:
        payload["caption"] = draft["caption"]
    return payload


def check_publishing_quota(token, warnings):
    """Stop if the account has used its quota, and only warn if the check fails.

    The limit comes from the API because Meta's pages disagree on 50 or 100 per day.
    Meta also enforces it on media_publish, so an unavailable check is still safe.
    """
    try:
        body = graph_request(
            token, f"{INSTAGRAM_ACCOUNT_ID}/content_publishing_limit?fields=quota_usage,config"
        )
        usage = body["data"][0]
        used = usage["quota_usage"]
        total = usage["config"]["quota_total"]
        if not (isinstance(used, int) and isinstance(total, int)):
            raise TypeError
    except GraphRequestFailed as error:
        warnings.append(f"The publishing quota couldn't be checked. {error}")
        return
    except (KeyError, IndexError, TypeError):
        warnings.append("The publishing quota response had an unexpected format.")
        return
    if used >= total:
        raise ToolError(
            f"The account has used {used} of its {total} API posts for this period. "
            "Nothing was published, so try again later."
        )


def create_container(token, draft):
    """Create the media container and record it. This doesn't publish anything."""
    try:
        body = graph_request(
            token, f"{INSTAGRAM_ACCOUNT_ID}/media", build_container_payload(draft)
        )
    except GraphRequestFailed as error:
        raise ToolError(
            f"Nothing was published, because the container wasn't created. {error}"
        ) from None
    container_id = body.get("id")
    if not isinstance(container_id, str) or not container_id.isdigit():
        raise ToolError("Nothing was published, because Meta returned no valid container ID.")
    draft["container_id"] = container_id
    draft["status"] = drafts.STATUS_CONTAINER_CREATED
    drafts.save_draft(draft)
    return container_id


def describe_upload_failure(status, body, token):
    """Return an UploadFailed built from known fields, never the raw response."""
    prefix = "The upload failed." if status is None else f"The upload failed with HTTP {status}."
    body = body if isinstance(body, dict) else {}
    details = body.get("debug_info")
    if not isinstance(details, dict):
        details = body.get("error")
    details = details if isinstance(details, dict) else {}
    message = prefix
    for label, key in (("type", "type"), ("code", "code"), ("message", "message")):
        if key in details:
            message += f" {label}: {redact(str(details[key]), token)}"
    if status in {401, 403}:
        message += " The token may not have access to resumable uploads for this account."
    return UploadFailed(message, details.get("retriable") if isinstance(details.get("retriable"), bool) else None)


def send_upload(token, container_id, reader, size):
    """Send one local file in one request, starting at byte 0. Raises UploadFailed."""
    request = urllib.request.Request(
        f"{RUPLOAD_API_ROOT}/{container_id}",
        data=reader,
        headers={
            "Authorization": f"OAuth {token}",
            "offset": "0",
            "file_size": str(size),
            "Content-Length": str(size),
        },
        method="POST",
    )
    opener = urllib.request.build_opener(NoRedirects)
    # "from None" and the generic messages keep the original exception, whose text
    # could include request details, out of errors and logs.
    try:
        with opener.open(request, timeout=UPLOAD_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            error_body = json.loads(error.read().decode("utf-8"))
        except (ValueError, AttributeError, OSError):
            error_body = {}
        raise describe_upload_failure(error.code, error_body, token) from None
    except (urllib.error.URLError, TimeoutError):
        raise UploadFailed("The upload couldn't finish (network error or timeout).") from None
    except ValueError:
        raise UploadFailed("Meta's upload response was not valid JSON.") from None
    except Exception as error:
        raise UploadFailed(f"Unexpected {type(error).__name__} during the upload.") from None
    if not isinstance(body, dict) or body.get("success") is not True:
        raise describe_upload_failure(None, body, token)


def upload_local_reel(token, draft, container_id):
    """Check the preview again, upload it, and confirm the approved bytes were the ones sent."""
    local_file = draft["local_file"]
    with drafts.open_verified_reel(local_file) as reader:
        send_upload(token, container_id, reader, local_file["size"])
        if not reader.matches():
            raise UploadFailed("The preview changed during the upload.")


def abandon_container(draft, status, last_error=None):
    """Forget an incomplete container, so the next attempt starts from a new one.

    Meta doesn't document how to resume an upload. An abandoned container was never
    published and expires by itself, so this can't cause a duplicate post.
    """
    draft.update(container_id=None, upload_complete=False, status=status, last_error=last_error)
    drafts.save_draft(draft)


def ensure_local_upload(token, draft):
    """Return a container holding the whole file, uploading it if that hasn't happened."""
    if draft.get("upload_complete"):
        return draft["container_id"]
    if draft["container_id"]:
        # An earlier attempt stopped before its upload finished.
        abandon_container(draft, drafts.STATUS_PREPARED)
    container_id = create_container(token, draft)
    try:
        upload_local_reel(token, draft, container_id)
    except drafts.DraftError as error:
        abandon_container(draft, drafts.STATUS_PREPARED, str(error))
        raise ToolError(f"Nothing was uploaded or published. {error}") from None
    except UploadFailed as error:
        if error.retriable is False:
            abandon_container(draft, drafts.STATUS_FAILED, str(error))
            raise ToolError(
                f"Nothing was published. {error} Meta says trying again won't help, so fix "
                "the video and prepare a new draft."
            ) from None
        abandon_container(draft, drafts.STATUS_PREPARED, str(error))
        raise ToolError(
            f"Nothing was published. {error} It won't be retried automatically. Calling this "
            "tool again asks for confirmation, then starts a new upload."
        ) from None
    draft.update(upload_complete=True, last_error=None)
    drafts.save_draft(draft)
    return container_id


def wait_for_container(token, container_id):
    """Return the container's status, polling at the rate Meta recommends."""
    status_code = None
    for check in range(MAX_STATUS_CHECKS):
        if check:
            time.sleep(STATUS_CHECK_INTERVAL_SECONDS)
        body = graph_request(token, f"{container_id}?fields=status_code,status")
        status_code = body.get("status_code")
        if status_code != "IN_PROGRESS":
            return status_code, body.get("status")
    return status_code, None


def record_published(draft, media_id, warnings):
    """Record a confirmed publish and return the result for the client."""
    draft.update(
        status=drafts.STATUS_PUBLISHED,
        media_id=media_id,
        published_at=drafts.utc_now(),
        last_error=None,
    )
    try:
        drafts.save_draft(draft)
    except OSError:
        warnings.append("The post is live, but the local draft file couldn't be updated.")
    return {
        "status": drafts.STATUS_PUBLISHED,
        "draft_id": draft["draft_id"],
        "media_id": media_id,
        "warnings": warnings,
    }


def publish_container(token, draft, warnings):
    """Send media_publish exactly once. A failure here is never retried automatically."""
    # publish_instagram_draft already checks the mode. This is a second lock on the only
    # code that sends media_publish, so no other mode can ever reach it.
    if os.environ.get(PUBLISHING_MODE_ENV_VAR, "") != MODE_LIVE:
        raise ToolError("Nothing was published. Publishing needs live mode.")
    # This is saved first, so a crash during the request can't look like "not published".
    draft["status"] = drafts.STATUS_PUBLISH_UNKNOWN
    drafts.save_draft(draft)
    try:
        body = graph_request(
            token,
            f"{INSTAGRAM_ACCOUNT_ID}/media_publish",
            {"creation_id": draft["container_id"]},
        )
    except GraphRequestFailed as error:
        draft["last_error"] = str(error)
        if error.may_have_published():
            drafts.save_draft(draft)
            raise ToolError(
                f"The post may or may not be live. {error} It won't be retried. Call this "
                "tool again to check its status, and check the account too."
            ) from None
        draft["status"] = drafts.STATUS_CONTAINER_CREATED
        drafts.save_draft(draft)
        raise ToolError(f"Meta refused to publish, so nothing was published. {error}") from None

    media_id = body.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise ToolError(
            "Meta's response had no media ID, so the post may or may not be live. It won't "
            "be retried. Call this tool again to check its status."
        )
    # Meta can drop part of a request even though the media published.
    for field in ("config_issue", "caption_add_on_attachment_status"):
        if body.get(field):
            warnings.append(
                f"The post is live, but Meta reported {field}: {redact(str(body[field]), token)}."
            )
    return record_published(draft, media_id, warnings)


def run_publish_attempt(token, draft):
    """Take one confirmed draft through the container, status, and publish steps."""
    warnings = []
    check_publishing_quota(token, warnings)
    if drafts.is_local_file_draft(draft):
        container_id = ensure_local_upload(token, draft)
    else:
        container_id = draft["container_id"] or create_container(token, draft)
    try:
        status_code, status_detail = wait_for_container(token, container_id)
    except GraphRequestFailed as error:
        raise ToolError(
            f"Nothing was published, because the container status couldn't be read. {error}"
        ) from None
    if status_code == "PUBLISHED":
        # An earlier attempt must have succeeded, so this must not publish again.
        warnings.append("Meta reports this container was already published.")
        return record_published(draft, None, warnings)
    if status_code in {"ERROR", "EXPIRED"}:
        draft["status"] = drafts.STATUS_FAILED
        draft["last_error"] = f"Container status {status_code}: {redact(str(status_detail), token)}"
        drafts.save_draft(draft)
        raise ToolError(
            f"Nothing was published. {draft['last_error']}. Fix the media if needed, then "
            "prepare a new draft."
        )
    if status_code != "FINISHED":
        raise ToolError(
            f"Nothing was published. The container's status is {redact(str(status_code), token)}, "
            "not FINISHED. Call this tool again later to continue with the same container."
        )
    return publish_container(token, draft, warnings)


def publish_worker(token, draft_id, content_hash):
    """Publish a confirmed draft, under a lock.

    This runs to completion in its own thread, so a client that cancels the tool
    call can't lose the result of a request that is already in flight.
    """
    try:
        with drafts.claim_draft(draft_id):
            # Reloaded because another process could have used the draft during the dialog.
            draft = drafts.load_draft(draft_id, content_hash, INSTAGRAM_ACCOUNT_ID, token)
            if draft["status"] not in {drafts.STATUS_PREPARED, drafts.STATUS_CONTAINER_CREATED}:
                raise ToolError("The draft changed status during confirmation. Nothing was sent.")
            draft["confirmed_at"] = drafts.utc_now()
            drafts.save_draft(draft)
            return run_publish_attempt(token, draft)
    except drafts.DraftError as error:
        raise ToolError(str(error)) from None
    except OSError:
        raise ToolError(
            "A local draft file couldn't be updated, so the attempt stopped. Check the "
            "account before trying again."
        ) from None


def reconcile_unknown_publish(token, draft_id, content_hash):
    """Check an unknown outcome using one GET request. This never publishes."""
    try:
        with drafts.claim_draft(draft_id):
            draft = drafts.load_draft(draft_id, content_hash, INSTAGRAM_ACCOUNT_ID, token)
            body = graph_request(token, f"{draft['container_id']}?fields=status_code,status")
            status_code = body.get("status_code")
            if status_code == "PUBLISHED":
                return record_published(draft, None, ["Meta confirms this container was published."])
    except GraphRequestFailed as error:
        raise ToolError(f"The post's status is still unknown. {error}") from None
    except drafts.DraftError as error:
        raise ToolError(str(error)) from None
    except OSError:
        raise ToolError("The post's status is still unknown. A local file couldn't be used.") from None
    raise ToolError(
        f"The post's status is still unknown. Meta reports {redact(str(status_code), token)}. "
        "Publishing stays blocked for this draft to prevent a duplicate. Check "
        "get_recent_instagram_posts, and prepare a new draft only if the post isn't there."
    )


def build_confirmation_message(draft, mode):
    """Describe the exact draft in the dialog that a person answers.

    Only an exact dry run gets the test wording. Any other mode gets the live warning,
    so a mistake here can't make a real post look like a test.
    """
    if draft["post_type"] == drafts.POST_TYPE_REEL:
        placement = "Feed and Reels" if draft["share_to_feed"] else "Reels only"
        post_type = f"Reel ({placement})"
    else:
        post_type = "Image post"
    if mode == MODE_DRY_RUN:
        heading = "TEST ONLY — Nothing will be published to Instagram"
        action = "To pass this test"
    else:
        heading = "PUBLISH TO INSTAGRAM? This will be public."
        action = "To publish"
    if drafts.is_local_file_draft(draft):
        local_file = draft["local_file"]
        label = "Local file" if mode == MODE_DRY_RUN else "Local file (uploaded to Meta after you confirm)"
        media = (
            f"{label}: {local_file['name']}\n"
            f"File size: {local_file['size']} bytes\n"
            f"File SHA-256: {local_file['sha256']}\n"
        )
    else:
        media = f"Media URL: {draft['media_url']}\n"
    return (
        f"{heading}\n\n"
        f"Account ID: {draft['account_id']}\n"
        f"Type: {post_type}\n"
        f"{media}"
        f"Draft ID: {draft['draft_id']}\n"
        f"Content hash: {draft['content_hash']}\n\n"
        "----- Caption -----\n"
        f"{draft['caption'] or '(no caption)'}\n"
        "----- End of caption -----\n\n"
        f"{action}, type exactly: {drafts.confirmation_phrase(draft)}\n"
        "To stop, decline this request."
    )


def open_draft_for_publishing(draft_id, content_hash):
    """Return (mode, draft), or raise ToolError if publishing can't proceed."""
    mode = os.environ.get(PUBLISHING_MODE_ENV_VAR, "")
    if mode not in {MODE_DRY_RUN, MODE_LIVE}:
        raise ToolError(
            f"Nothing was published. Publishing is off. {PUBLISHING_MODE_ENV_VAR} must be "
            f"set to {MODE_DRY_RUN} or {MODE_LIVE} before the server starts."
        )
    # A dry run needs no token, so one is only required for live mode.
    secret = read_token() if mode == MODE_LIVE else os.environ.get(TOKEN_ENV_VAR, "").strip()
    try:
        return mode, drafts.load_draft(draft_id, content_hash, INSTAGRAM_ACCOUNT_ID, secret)
    except drafts.DraftError as error:
        raise ToolError(str(error)) from None


def needs_confirmation(mode, draft):
    """Return True if this call could send a publish request, or is a dry run."""
    return mode == MODE_DRY_RUN or draft["status"] in {
        drafts.STATUS_PREPARED,
        drafts.STATUS_CONTAINER_CREATED,
    }


def ask_publish_confirmation(
    draft_id: str, content_hash: str
) -> Elicit[PublishConfirmation] | PublishConfirmation:
    """Ask the person for the phrase, after every check that doesn't need them.

    The SDK runs this before the tool body. Using Resolve and Elicit works on every
    protocol version, while ctx.elicit fails on 2026-07-28 and later. A client that
    can't show the dialog gets an error, and the tool body never runs.
    """
    mode, draft = open_draft_for_publishing(draft_id, content_hash)
    if not needs_confirmation(mode, draft):
        # This call can't publish. The empty value could never match a phrase.
        return PublishConfirmation(confirmation="")
    return Elicit(build_confirmation_message(draft, mode), PublishConfirmation)


def require_user_confirmation(answer, draft):
    """Return only if a person typed this draft's phrase. Otherwise raise ToolError."""
    if answer.action != "accept":
        raise ToolError(f"Nothing was published. The confirmation dialog ended with: {answer.action}.")
    if answer.data.confirmation.strip() != drafts.confirmation_phrase(draft):
        raise ToolError("Nothing was published. The confirmation phrase didn't match.")


@mcp.tool(
    annotations=ToolAnnotations(
        title="Prepare Instagram draft",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
    structured_output=False,
)
async def prepare_instagram_draft(
    post_type: Annotated[
        Literal["post", "reel"],
        Field(description="post for one JPEG image, or reel for one MP4 or MOV video."),
    ],
    media_url: Annotated[str, Field(description="Public https URL that Meta will fetch.")],
    caption: Annotated[str, Field(description="Caption text. It is stored exactly.")] = "",
    share_to_feed: Annotated[
        bool | None,
        Field(description="Reels only. Also allow the reel in Feed. Defaults to true."),
    ] = None,
) -> str:
    """Validate an Instagram post or reel, and save it as a local draft if it passes.

    This never publishes and never uses the network. Returns JSON with "valid",
    "errors", "warnings", "caption_stats", and for a valid draft "draft_id",
    "content_hash", and "unverified_requirements". Show the draft to the user. Each
    draft is fixed, so any change needs a new draft.
    """
    if post_type == drafts.POST_TYPE_REEL and share_to_feed is None:
        share_to_feed = True
    secret = os.environ.get(TOKEN_ENV_VAR, "").strip()
    errors, warnings = drafts.validate_content(
        post_type, media_url, caption, share_to_feed, secret
    )
    stats = drafts.caption_stats(caption)
    if errors:
        # The input isn't repeated here, because it may contain something secret.
        result = {
            "valid": False,
            "errors": errors,
            "warnings": warnings,
            "caption_stats": stats,
            "draft_id": None,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    try:
        draft = drafts.create_draft(
            INSTAGRAM_ACCOUNT_ID, post_type, media_url, caption, share_to_feed
        )
    except OSError:
        raise ToolError("The draft couldn't be saved to the drafts folder.") from None
    mode = os.environ.get(PUBLISHING_MODE_ENV_VAR, "")
    result = {
        "valid": True,
        "draft_id": draft["draft_id"],
        "content_hash": draft["content_hash"],
        "status": draft["status"],
        "account_id": draft["account_id"],
        "post_type": post_type,
        "media_url": media_url,
        "caption": caption,
        "share_to_feed": share_to_feed,
        "errors": [],
        "warnings": warnings,
        "caption_stats": stats,
        "unverified_requirements": list(drafts.UNVERIFIED_REQUIREMENTS[post_type]),
        "publishing_mode": mode if mode in {MODE_DRY_RUN, MODE_LIVE} else "off",
        "next_step": (
            "Nothing has been published. Show this draft to the user. Call "
            "publish_instagram_draft only if the user asks to publish this exact draft."
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Prepare local Instagram Reel",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=False,
    ),
    structured_output=False,
)
async def prepare_instagram_local_reel(
    file_name: Annotated[
        str, Field(description="Plain .mp4 file name in reels/previews, with no folders.")
    ],
    caption: Annotated[str, Field(description="Caption text. It is stored exactly.")] = "",
    share_to_feed: Annotated[
        bool, Field(description="Also allow the reel in Feed. Defaults to true.")
    ] = True,
) -> str:
    """Validate a Reel rendered in reels/previews, and save it as a local draft if it passes.

    The file needs an awaiting_review record that matches its SHA-256. The draft is bound
    to that hash and the file's size. This never publishes or uploads, and doesn't use the
    network or the access token. Show the draft to the user. Each draft is fixed, so any
    change needs a new draft. Returns JSON with "valid" and "errors", and for a valid
    draft "draft_id", "content_hash" and "local_file".
    """
    stats = drafts.caption_stats(caption)
    _, warnings = drafts.validate_local_content(caption, share_to_feed)
    try:
        # Hashing a large file can take a while, so it runs outside the event loop.
        draft = await asyncio.to_thread(
            drafts.create_local_reel_draft, INSTAGRAM_ACCOUNT_ID, file_name, caption, share_to_feed
        )
    except drafts.DraftError as error:
        # The input isn't repeated here, because it may contain something secret.
        result = {
            "valid": False,
            "errors": [str(error)],
            "warnings": warnings,
            "caption_stats": stats,
            "draft_id": None,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    except OSError:
        raise ToolError("The draft couldn't be saved to the drafts folder.") from None
    result = {
        "valid": True,
        "draft_id": draft["draft_id"],
        "content_hash": draft["content_hash"],
        "status": draft["status"],
        "account_id": draft["account_id"],
        "post_type": draft["post_type"],
        "media_source": draft["media_source"],
        "local_file": draft["local_file"],
        "caption": caption,
        "share_to_feed": share_to_feed,
        "errors": [],
        "warnings": warnings,
        "caption_stats": stats,
        "next_step": (
            "Nothing has been uploaded or published. Show this draft to the user. Call "
            "publish_instagram_draft only if the user asks to publish this exact draft."
        ),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool(
    annotations=ToolAnnotations(
        title="Publish Instagram draft",
        read_only_hint=False,
        destructive_hint=True,
        idempotent_hint=False,
        open_world_hint=True,
    ),
    structured_output=False,
)
async def publish_instagram_draft(
    draft_id: Annotated[str, Field(description="draft_id from prepare_instagram_draft.")],
    content_hash: Annotated[str, Field(description="content_hash from the same draft.")],
    answer: Annotated[ElicitationResult[PublishConfirmation], Resolve(ask_publish_confirmation)],
) -> str:
    """Publish one prepared draft to Instagram, after a person confirms it in a dialog.

    Call this only when the user has asked to publish this exact draft. Never call it
    on your own initiative or in a test. The client shows the user the draft and asks
    them to type a phrase. If the user declines, stop and don't call this again unless
    they ask. Publishing also requires IG_PUBLISHING_MODE=live. A local-file draft is
    uploaded to Meta only after the user confirms. Returns JSON.
    """
    # Loaded again because the draft could have changed while the dialog was open.
    mode, draft = open_draft_for_publishing(draft_id, content_hash)

    if mode == MODE_DRY_RUN:
        require_user_confirmation(answer, draft)
        result = {
            "status": "dry_run_confirmed",
            "draft_id": draft_id,
            "draft_status": draft["status"],
            "note": "The phrase matched. Dry run, so nothing was sent to Meta or changed.",
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    token = read_token()
    if draft["status"] == drafts.STATUS_PUBLISHED:
        result = {
            "status": drafts.STATUS_PUBLISHED,
            "draft_id": draft_id,
            "media_id": draft["media_id"],
            "warnings": ["This draft was already published. Nothing was sent."],
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    if draft["status"] == drafts.STATUS_FAILED:
        raise ToolError(
            f"This draft can't be published. {redact(str(draft['last_error']), token)} "
            "Prepare a new draft."
        )
    if draft["status"] == drafts.STATUS_PUBLISH_UNKNOWN:
        result = await asyncio.to_thread(reconcile_unknown_publish, token, draft_id, content_hash)
        return json.dumps(redact(result, token), ensure_ascii=False, indent=2)

    require_user_confirmation(answer, draft)
    result = await asyncio.to_thread(publish_worker, token, draft_id, content_hash)
    return json.dumps(redact(result, token), ensure_ascii=False, indent=2)


def open_draft_for_upload_test(draft_id, content_hash):
    """Return (token, draft), or raise a ToolError if this test can't run on the draft."""
    if os.environ.get(PUBLISHING_MODE_ENV_VAR, "") != MODE_UPLOAD_TEST:
        raise ToolError(
            f"Nothing was uploaded. {PUBLISHING_MODE_ENV_VAR} must be set to {MODE_UPLOAD_TEST} "
            "before the server starts."
        )
    token = read_token()
    try:
        draft = drafts.load_draft(draft_id, content_hash, INSTAGRAM_ACCOUNT_ID, token)
        is_local = drafts.is_local_file_draft(draft)
    except drafts.DraftError as error:
        raise ToolError(str(error)) from None
    if not is_local:
        raise ToolError("Nothing was uploaded. This test only works with a local-file draft.")
    # Any other status means a real publish attempt has used this draft.
    if draft["status"] != drafts.STATUS_PREPARED:
        raise ToolError(f"Nothing was uploaded. The draft's status is {draft['status']}, not prepared.")
    return token, draft


def build_upload_test_message(draft):
    """Describe the test in the dialog, including that the file really leaves this computer."""
    local_file = draft["local_file"]
    placement = "Feed and Reels" if draft["share_to_feed"] else "Reels only"
    return (
        "UPLOAD TEST — This sends the file to Meta, but won't publish it to Instagram\n\n"
        "After you confirm, the file leaves this computer and Meta stores it in an unpublished "
        "container, which expires after 24 hours. This test has no way to publish it.\n\n"
        f"Account ID: {draft['account_id']}\n"
        f"Type: Reel ({placement})\n"
        f"Local file: {local_file['name']}\n"
        f"File size: {local_file['size']} bytes\n"
        f"File SHA-256: {local_file['sha256']}\n"
        f"Draft ID: {draft['draft_id']}\n"
        f"Content hash: {draft['content_hash']}\n\n"
        "----- Caption -----\n"
        f"{draft['caption'] or '(no caption)'}\n"
        "----- End of caption -----\n\n"
        f"To send this test upload, type exactly: {drafts.upload_test_phrase(draft)}\n"
        "To stop, decline this request."
    )


def ask_upload_test_confirmation(
    draft_id: str, content_hash: str
) -> Elicit[PublishConfirmation] | PublishConfirmation:
    """Ask for the test phrase, after every check that doesn't need a person.

    A client that can't show the dialog gets an error, and the tool body never runs.
    """
    _, draft = open_draft_for_upload_test(draft_id, content_hash)
    return Elicit(build_upload_test_message(draft), PublishConfirmation)


def require_upload_test_confirmation(answer, draft):
    """Return only if a person typed this draft's test phrase. Otherwise raise ToolError."""
    if answer.action != "accept":
        raise ToolError(f"Nothing was uploaded. The confirmation dialog ended with: {answer.action}.")
    if answer.data.confirmation.strip() != drafts.upload_test_phrase(draft):
        raise ToolError("Nothing was uploaded. The confirmation phrase didn't match.")


def run_upload_test(token, draft):
    """Create a container, upload the file, and read its status. This never publishes.

    The container ID is only returned, never stored as the draft's container_id, so a
    later real publish can't pick up this container.
    """
    try:
        body = graph_request(token, f"{INSTAGRAM_ACCOUNT_ID}/media", build_container_payload(draft))
    except GraphRequestFailed as error:
        raise ToolError(f"Nothing was uploaded, because the container wasn't created. {error}") from None
    container_id = body.get("id")
    if not isinstance(container_id, str) or not container_id.isdigit():
        raise ToolError("Nothing was uploaded, because Meta returned no valid container ID.")
    try:
        upload_local_reel(token, draft, container_id)
    except drafts.DraftError as error:
        raise ToolError(f"Nothing was uploaded or published. {error}") from None
    except UploadFailed as error:
        raise ToolError(f"Nothing was published. {error} It won't be retried automatically.") from None
    try:
        status_code, status_detail = wait_for_container(token, container_id)
    except GraphRequestFailed as error:
        raise ToolError(
            f"Nothing was published. The upload finished, but its status couldn't be read. {error}"
        ) from None
    if status_code in {"ERROR", "EXPIRED"}:
        raise ToolError(
            f"Nothing was published. Meta couldn't process the upload. Container status "
            f"{status_code}: {redact(str(status_detail), token)}"
        )
    passed = status_code == "FINISHED"
    return {
        "status": "upload_test_passed" if passed else "upload_test_inconclusive",
        "draft_id": draft["draft_id"],
        "test_container_id": container_id,
        "container_status": redact(str(status_code), token),
        "published": False,
        "note": (
            "Nothing was published. Meta holds an unpublished test container, which expires "
            "after 24 hours. The draft is unchanged, and this test can't publish it."
        ),
    }


def upload_test_worker(token, draft_id, content_hash):
    """Run one confirmed test under the draft's lock, and leave the draft unchanged."""
    try:
        with drafts.claim_draft(draft_id):
            # Reloaded because the file or draft could have changed during the dialog.
            draft = drafts.load_draft(draft_id, content_hash, INSTAGRAM_ACCOUNT_ID, token)
            if draft["status"] != drafts.STATUS_PREPARED:
                raise ToolError("The draft changed status during confirmation. Nothing was sent.")
            return run_upload_test(token, draft)
    except drafts.DraftError as error:
        raise ToolError(str(error)) from None


@mcp.tool(
    annotations=ToolAnnotations(
        title="Test local Instagram upload",
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=False,
        open_world_hint=True,
    ),
    structured_output=False,
)
async def test_instagram_local_upload(
    draft_id: Annotated[str, Field(description="draft_id from prepare_instagram_local_reel.")],
    content_hash: Annotated[str, Field(description="content_hash from the same draft.")],
    answer: Annotated[
        ElicitationResult[PublishConfirmation], Resolve(ask_upload_test_confirmation)
    ],
) -> str:
    """Send one local-file draft to Meta as an unpublished test, after a person confirms.

    This sends the real file to Meta but can never publish it. Call this only when the
    user has asked for an upload test of this exact draft, never on your own initiative.
    The client shows the user the draft and asks them to type a phrase. If the user
    declines, stop and don't call this again unless they ask. It requires
    IG_PUBLISHING_MODE=upload_test. Returns JSON.
    """
    # Loaded again because the draft could have changed while the dialog was open.
    token, draft = open_draft_for_upload_test(draft_id, content_hash)
    require_upload_test_confirmation(answer, draft)
    result = await asyncio.to_thread(upload_test_worker, token, draft_id, content_hash)
    return json.dumps(redact(result, token), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run("stdio")
