"""Validation and local storage for Instagram post and reel drafts.

Nothing in this file uses the network. For a URL draft, validation only inspects the
text it is given, so facts that require the media file itself, such as its size or
codec, are listed in unverified_requirements for a person to check.

A local-file draft points to a Reel rendered by reel_creator.py instead of a URL. It
is bound to the file's SHA-256 and size, and the file and its review record are
checked again every time the draft loads.

A draft can't be edited. Its content_hash covers everything that would be
published, so any change needs a new draft and a new confirmation.

Limits come from Meta's IG User Media reference:
https://developers.facebook.com/docs/instagram-platform/instagram-graph-api/reference/ig-user/media
"""

import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, unquote, urlsplit

DRAFTS_DIR = Path(__file__).resolve().parent / "drafts"
# Local-file drafts can only use this folder, so a draft can't point to any other file
# on the computer.
PREVIEWS_DIR = Path(__file__).resolve().parent / "reels" / "previews"

POST_TYPE_POST = "post"
POST_TYPE_REEL = "reel"

# URL drafts predate this field, so a draft without it is a URL draft.
MEDIA_SOURCE_URL = "url"
MEDIA_SOURCE_LOCAL_FILE = "local_file"
# The status reel_creator.py gives a preview that passed its checks.
PREVIEW_STATUS_AWAITING_REVIEW = "awaiting_review"
MAX_REEL_FILE_BYTES = 300 * 1000 * 1000

# Nothing has been sent to Meta, or a container creation attempt failed.
STATUS_PREPARED = "prepared"
# Meta holds a container, and it has not been published.
STATUS_CONTAINER_CREATED = "container_created"
# A media_publish request was sent and the outcome is unknown. It may be live.
STATUS_PUBLISH_UNKNOWN = "publish_unknown"
STATUS_PUBLISHED = "published"
# Meta reported the container as ERROR or EXPIRED, so this draft can't continue.
STATUS_FAILED = "failed"

MAX_CAPTION_LENGTH = 2200
MAX_HASHTAGS = 30
MAX_MENTIONS = 20
MAX_URL_LENGTH = 2048

ALLOWED_EXTENSIONS = {
    POST_TYPE_POST: {".jpg", ".jpeg"},
    POST_TYPE_REEL: {".mp4", ".mov"},
}
KNOWN_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".bmp", ".tif",
    ".tiff", ".svg", ".mpo", ".jps", ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".m4v", ".wmv", ".flv",
}
SECRET_QUERY_PARAMETERS = {"access_token", "client_secret", "password"}
PRIVATE_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home", ".corp")

UNVERIFIED_REQUIREMENTS = {
    POST_TYPE_POST: (
        "The URL is publicly reachable and stays reachable until publishing ends.",
        "The file is a real JPEG, not MPO or JPS, and is at most 8 MB.",
        "The aspect ratio is between 4:5 and 1.91:1.",
        "The width is at least 320 px. Meta scales images outside 320-1440 px.",
    ),
    POST_TYPE_REEL: (
        "The URL is publicly reachable and stays reachable until publishing ends.",
        "The file is MOV or MP4 with no edit lists and the moov atom at the front.",
        "Video is HEVC or H.264, progressive, closed GOP, 4:2:0, 23-60 FPS, VBR, at most 25 Mbps.",
        "Audio is AAC, at most 48 kHz, mono or stereo, about 128 kbps.",
        "The duration is 3 seconds to 15 minutes, and the file is at most 300 MB.",
        "The width is at most 1920 px. 9:16 is recommended.",
    ),
}

_DRAFT_ID_PATTERN = re.compile(r"[0-9a-f]{16}")
# Plain names only, so no folders, drive letters, alternate streams or trailing dots.
_PREVIEW_NAME_PATTERN = re.compile(r"[A-Za-z0-9_-]+\.mp4")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_URL_CHARACTERS = re.compile(r"[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_HASHTAG = re.compile(r"(?<![\w#])#\w+")
_MENTION = re.compile(r"(?<![\w@.])@[\w.]+")


class DraftError(Exception):
    """A draft can't be used. The message is safe to show to the client."""


def utc_now():
    """Return the current time as an ISO 8601 string in UTC."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def caption_length(caption):
    """Count UTF-16 units, the cautious reading of the limit, so emoji count as 2.

    Meta doesn't say how it counts characters, and this count is never lower than
    the number of code points, so a caption that passes here fits either way.
    """
    return len(caption.encode("utf-16-le", "surrogatepass")) // 2


def caption_stats(caption):
    """Return the caption measurements that the limits apply to."""
    return {
        "length": caption_length(caption),
        "max_length": MAX_CAPTION_LENGTH,
        "hashtags": len(_HASHTAG.findall(caption)),
        "max_hashtags": MAX_HASHTAGS,
        "mentions": len(_MENTION.findall(caption)),
        "max_mentions": MAX_MENTIONS,
    }


def validate_caption(caption):
    """Return (errors, warnings) for a caption. Messages never quote the caption."""
    if not caption.strip():
        return [], ["The caption is empty, so the post will have no caption."]
    errors = []
    stats = caption_stats(caption)
    if stats["length"] > MAX_CAPTION_LENGTH:
        errors.append(
            f"The caption is {stats['length']} characters, counting each emoji as "
            f"up to 2. The limit is {MAX_CAPTION_LENGTH}."
        )
    if stats["hashtags"] > MAX_HASHTAGS:
        errors.append(
            f"The caption has {stats['hashtags']} hashtags. The limit is {MAX_HASHTAGS}."
        )
    if stats["mentions"] > MAX_MENTIONS:
        errors.append(
            f"The caption has {stats['mentions']} @mentions. The limit is {MAX_MENTIONS}."
        )
    if any(
        unicodedata.category(char) in {"Cc", "Cs"} and char not in "\n\r\t"
        for char in caption
    ):
        errors.append("The caption contains control or invalid characters.")
    return errors, []


def _validate_host(host):
    """Return an error message if the host can't be reached by Meta's servers."""
    host = host.rstrip(".").lower()
    if not host:
        return "The media URL has no host."
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(PRIVATE_HOST_SUFFIXES):
            return "The media URL must use a public host, not a local or internal name."
        return None
    if not address.is_global:
        return "The media URL must not use a private or local IP address."
    return None


def validate_media_url(media_url, post_type):
    """Return (errors, warnings) for a media URL. Messages never quote the URL."""
    if not media_url:
        return ["The media URL is required."], []
    if len(media_url) > MAX_URL_LENGTH:
        return [f"The media URL is longer than {MAX_URL_LENGTH} characters."], []
    if not _URL_CHARACTERS.fullmatch(media_url) or _BAD_PERCENT_ESCAPE.search(media_url):
        return [
            "The media URL has characters that must be percent-encoded, or an "
            "invalid percent escape. Meta rejects URLs that break the IETF standard."
        ], []
    try:
        parts = urlsplit(media_url)
        host = parts.hostname or ""
        parts.port  # Reading it raises ValueError if the port is invalid.
    except ValueError:
        return ["The media URL is malformed."], []
    if parts.scheme.lower() != "https":
        return ["The media URL must start with https://."], []
    if parts.username is not None or parts.password is not None:
        return ["The media URL must not contain a username or password."], []

    errors = []
    host_error = _validate_host(host)
    if host_error:
        errors.append(host_error)
    if parts.fragment:
        errors.append("The media URL must not contain a #fragment.")
    query_names = {name.lower() for name, _ in parse_qsl(parts.query, keep_blank_values=True)}
    if query_names & SECRET_QUERY_PARAMETERS:
        errors.append("The media URL must not contain an access token or other secret.")

    warnings = []
    extension = PurePosixPath(unquote(parts.path)).suffix.lower()
    allowed = ALLOWED_EXTENSIONS[post_type]
    if extension not in allowed:
        expected = " or ".join(sorted(allowed))
        if extension in KNOWN_MEDIA_EXTENSIONS:
            errors.append(f"A {post_type} needs a {expected} file, not {extension}.")
        else:
            warnings.append(
                f"The URL doesn't show a file type, so check that it serves a {expected} file."
            )
    return errors, warnings


def validate_content(post_type, media_url, caption, share_to_feed, secret=""):
    """Return (errors, warnings) for all draft content.

    secret is the access token, if one is set. It is only compared, never returned.
    """
    if post_type not in ALLOWED_EXTENSIONS:
        return [f"post_type must be {POST_TYPE_POST} or {POST_TYPE_REEL}."], []
    errors, warnings = validate_media_url(media_url, post_type)
    caption_errors, caption_warnings = validate_caption(caption)
    errors += caption_errors
    warnings += caption_warnings
    if post_type == POST_TYPE_POST and share_to_feed is not None:
        errors.append("share_to_feed only applies to reels.")
    if post_type == POST_TYPE_REEL and not isinstance(share_to_feed, bool):
        errors.append("share_to_feed must be true or false for a reel.")
    if secret and (secret in media_url or secret in caption):
        errors.append("The draft contains the access token. Remove it, and replace the token.")
    return errors, warnings


def compute_content_hash(account_id, post_type, media_url, caption, share_to_feed):
    """Return the SHA-256 of everything that would be published."""
    content = {
        "account_id": account_id,
        "caption": caption,
        "media_url": media_url,
        "post_type": post_type,
        "share_to_feed": share_to_feed,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def compute_local_content_hash(account_id, local_file, caption, share_to_feed):
    """Return the SHA-256 of a local-file draft, including the file's own hash and size.

    media_source is included, so this can never equal the hash of a URL draft.
    """
    content = {
        "account_id": account_id,
        "caption": caption,
        "file_name": local_file["name"],
        "file_sha256": local_file["sha256"],
        "file_size": local_file["size"],
        "media_source": MEDIA_SOURCE_LOCAL_FILE,
        "post_type": POST_TYPE_REEL,
        "share_to_feed": share_to_feed,
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def is_local_file_draft(draft):
    """Return True for a local-file draft, False for a URL draft, or refuse anything else."""
    if not isinstance(draft, dict):
        # load_draft reports this as an incomplete draft file.
        raise TypeError("A draft must be a JSON object.")
    source = draft.get("media_source", MEDIA_SOURCE_URL)
    if not isinstance(source, str) or source not in {MEDIA_SOURCE_URL, MEDIA_SOURCE_LOCAL_FILE}:
        raise DraftError("The draft has an unknown media source.")
    return source == MEDIA_SOURCE_LOCAL_FILE


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_preview_record(record_path, file_name):
    """Return the preview's SHA-256 from its review record, if the record allows its use."""
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DraftError("This preview has no review record.") from None
    except (OSError, ValueError):
        raise DraftError("The preview's review record couldn't be read.") from None
    if not isinstance(record, dict):
        raise DraftError("The preview's review record couldn't be read.")
    if record.get("status") != PREVIEW_STATUS_AWAITING_REVIEW:
        raise DraftError(f"The preview's status isn't {PREVIEW_STATUS_AWAITING_REVIEW}.")
    if record.get("preview_file") != file_name:
        raise DraftError("The review record belongs to a different file.")
    if record.get("problems") != []:
        raise DraftError("The preview's review record doesn't show that its checks passed.")
    expected_hash = record.get("preview_sha256")
    if not isinstance(expected_hash, str) or not _SHA256_PATTERN.fullmatch(expected_hash):
        raise DraftError("The preview's review record has no valid SHA-256.")
    return expected_hash


def inspect_local_reel(file_name):
    """Return the name, SHA-256 and size of a preview, or raise DraftError.

    The file must be directly inside PREVIEWS_DIR, and its review record must say it
    passed its checks and must match the file's current contents.
    """
    if not isinstance(file_name, str) or not _PREVIEW_NAME_PATTERN.fullmatch(file_name):
        raise DraftError("The file must be an .mp4 name from reels/previews, with no folders.")
    path = PREVIEWS_DIR / file_name
    try:
        if path.is_symlink() or path.resolve().parent != PREVIEWS_DIR.resolve():
            raise DraftError("The preview must be a real file inside reels/previews.")
        if not path.is_file():
            raise DraftError("That preview isn't in reels/previews.")
        size = path.stat().st_size
        if size == 0:
            raise DraftError("The preview is empty.")
        if size > MAX_REEL_FILE_BYTES:
            raise DraftError("The preview is larger than the 300 MB limit for a reel.")
        expected_hash = _read_preview_record(path.with_suffix(".json"), file_name)
        actual_hash = _file_sha256(path)
    except OSError:
        raise DraftError("The preview couldn't be read.") from None
    if actual_hash != expected_hash:
        raise DraftError("The preview doesn't match the hash in its review record.")
    return {"name": file_name, "sha256": actual_hash, "size": size}


def validate_local_content(caption, share_to_feed, file_name="", secret=""):
    """Return (errors, warnings) for the text of a local-file draft."""
    if not isinstance(caption, str):
        return ["The caption must be text."], []
    errors, warnings = validate_caption(caption)
    if not isinstance(share_to_feed, bool):
        errors.append("share_to_feed must be true or false for a reel.")
    if secret and (secret in caption or secret in str(file_name)):
        errors.append("The draft contains the access token. Remove it, and replace the token.")
    return errors, warnings


def _check_local_file(local_file):
    """Raise DraftError unless the preview still matches what the draft is bound to."""
    current = inspect_local_reel(local_file["name"])
    if current != {key: local_file[key] for key in ("name", "sha256", "size")}:
        raise DraftError("The preview changed after this draft was prepared. Prepare a new draft.")


def _local_draft_errors(draft, secret):
    """Return why a saved local-file draft can't be used. Raises DraftError for the file."""
    if draft["post_type"] != POST_TYPE_REEL or draft["media_url"] is not None:
        raise DraftError("The draft file is incomplete.")
    uploaded = draft.get("upload_complete", False)
    if not isinstance(uploaded, bool) or (uploaded and not draft["container_id"]):
        raise DraftError("The draft file is incomplete.")
    local_file = draft["local_file"]
    errors, _ = validate_local_content(
        draft["caption"], draft["share_to_feed"], local_file["name"], secret
    )
    # After the upload, Meta holds the media and nothing reads this file again. Deleting
    # a preview must not stop the status check on a post that may be live.
    if not uploaded and draft["status"] not in {
        STATUS_PUBLISH_UNKNOWN,
        STATUS_PUBLISHED,
        STATUS_FAILED,
    }:
        _check_local_file(local_file)
    return errors


class VerifiedReelReader:
    """Stream an opened preview, and record what was read so it can be checked afterward."""

    def __init__(self, file, local_file):
        self._file = file
        self._expected_hash = local_file["sha256"]
        self._size = local_file["size"]
        self._sent = 0
        self._digest = hashlib.sha256()

    def read(self, size=-1):
        remaining = self._size - self._sent
        if size is None or size < 0 or size > remaining:
            size = remaining
        chunk = self._file.read(size)
        self._sent += len(chunk)
        self._digest.update(chunk)
        return chunk

    def matches(self):
        """Return True only if exactly the approved bytes were read."""
        return self._sent == self._size and self._digest.hexdigest() == self._expected_hash


@contextmanager
def open_verified_reel(local_file):
    """Yield a reader for a preview, after checking its record, size and SHA-256.

    The hash is checked on the opened file, so the bytes checked and the bytes read
    come from the same file even if something replaces that path afterward.
    """
    _check_local_file(local_file)
    try:
        file = open(PREVIEWS_DIR / local_file["name"], "rb")
    except OSError:
        raise DraftError("The preview couldn't be read.") from None
    with file:
        try:
            size = os.fstat(file.fileno()).st_size
            digest = hashlib.sha256()
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
            file.seek(0)
        except OSError:
            raise DraftError("The preview couldn't be read.") from None
        if size != local_file["size"] or digest.hexdigest() != local_file["sha256"]:
            raise DraftError("The preview changed after this draft was prepared. Prepare a new draft.")
        yield VerifiedReelReader(file, local_file)


def confirmation_phrase(draft):
    """Return the exact text a person must type to publish this draft."""
    return f"PUBLISH {draft['content_hash'][:8].upper()}"


def upload_test_phrase(draft):
    """Return the text a person must type to send a test upload that can't be published.

    It differs from the publish phrase, so neither one can stand in for the other.
    """
    return f"UPLOAD TEST {draft['content_hash'][:8].upper()}"


def _draft_path(draft_id, suffix=".json"):
    # The strict pattern means a draft ID can't point outside the drafts folder.
    if not isinstance(draft_id, str) or not _DRAFT_ID_PATTERN.fullmatch(draft_id):
        raise DraftError("That is not a valid draft ID.")
    return DRAFTS_DIR / f"{draft_id}{suffix}"


def save_draft(draft):
    """Write a draft atomically, so a crash can't leave a half-written file."""
    path = _draft_path(draft["draft_id"])
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    handle, temporary_path = tempfile.mkstemp(dir=DRAFTS_DIR, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(draft, file, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    except BaseException:
        Path(temporary_path).unlink(missing_ok=True)
        raise


def create_draft(account_id, post_type, media_url, caption, share_to_feed):
    """Create and store a new draft from content that has passed validation."""
    draft = {
        "draft_id": secrets.token_hex(8),
        "account_id": account_id,
        "post_type": post_type,
        "media_url": media_url,
        "caption": caption,
        "share_to_feed": share_to_feed,
        "content_hash": compute_content_hash(
            account_id, post_type, media_url, caption, share_to_feed
        ),
        "status": STATUS_PREPARED,
        "created_at": utc_now(),
        "confirmed_at": None,
        "container_id": None,
        "media_id": None,
        "published_at": None,
        "last_error": None,
    }
    save_draft(draft)
    return draft


def create_local_reel_draft(account_id, file_name, caption="", share_to_feed=True, secret=""):
    """Create and store a Reel draft bound to one preview in reels/previews.

    This only reads local files. Raises DraftError if the file or text can't be used.
    """
    errors, _ = validate_local_content(caption, share_to_feed, file_name, secret)
    if errors:
        raise DraftError(" ".join(errors))
    local_file = inspect_local_reel(file_name)
    draft = {
        "draft_id": secrets.token_hex(8),
        "account_id": account_id,
        "post_type": POST_TYPE_REEL,
        "media_source": MEDIA_SOURCE_LOCAL_FILE,
        "media_url": None,
        "local_file": local_file,
        "upload_complete": False,
        "caption": caption,
        "share_to_feed": share_to_feed,
        "content_hash": compute_local_content_hash(account_id, local_file, caption, share_to_feed),
        "status": STATUS_PREPARED,
        "created_at": utc_now(),
        "confirmed_at": None,
        "container_id": None,
        "media_id": None,
        "published_at": None,
        "last_error": None,
    }
    save_draft(draft)
    return draft


def load_draft(draft_id, expected_hash, account_id, secret=""):
    """Load a draft, and refuse it unless its content still matches expected_hash."""
    path = _draft_path(draft_id)
    try:
        draft = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DraftError("No draft has that ID.") from None
    except (OSError, ValueError):
        raise DraftError("The draft file couldn't be read.") from None
    try:
        is_local = is_local_file_draft(draft)
        if is_local:
            actual_hash = compute_local_content_hash(
                draft["account_id"], draft["local_file"], draft["caption"], draft["share_to_feed"]
            )
        else:
            actual_hash = compute_content_hash(
                draft["account_id"],
                draft["post_type"],
                draft["media_url"],
                draft["caption"],
                draft["share_to_feed"],
            )
        stored_hash = draft["content_hash"]
        status = draft["status"]
        same_id = draft["draft_id"] == draft_id
    except (KeyError, TypeError):
        raise DraftError("The draft file is incomplete.") from None
    if not same_id or actual_hash != stored_hash:
        raise DraftError("The draft file was changed after it was prepared. Prepare a new draft.")
    if actual_hash != expected_hash:
        raise DraftError("content_hash doesn't match this draft. Nothing was published.")
    if draft["account_id"] != account_id:
        raise DraftError("This draft belongs to a different Instagram account.")
    if status not in {
        STATUS_PREPARED,
        STATUS_CONTAINER_CREATED,
        STATUS_PUBLISH_UNKNOWN,
        STATUS_PUBLISHED,
        STATUS_FAILED,
    }:
        raise DraftError("The draft has an unknown status.")
    if not isinstance(draft["caption"], str) or not (is_local or isinstance(draft["media_url"], str)):
        raise DraftError("The draft file is incomplete.")
    # The hash doesn't cover the container ID, and it becomes part of a request path.
    container_id = draft.get("container_id")
    if not (isinstance(container_id, str) and container_id.isdigit()):
        if container_id is not None or status in {STATUS_CONTAINER_CREATED, STATUS_PUBLISH_UNKNOWN}:
            raise DraftError("The draft has an invalid container ID.")
    if is_local:
        try:
            errors = _local_draft_errors(draft, secret)
        except (KeyError, TypeError):
            raise DraftError("The draft file is incomplete.") from None
    else:
        errors, _ = validate_content(
            draft["post_type"], draft["media_url"], draft["caption"], draft["share_to_feed"], secret
        )
    if errors:
        raise DraftError(f"The draft no longer passes validation. {' '.join(errors)}")
    return draft


@contextmanager
def claim_draft(draft_id):
    """Hold a lock file so that only one publish attempt can run for a draft.

    A crash leaves the file behind on purpose. The draft stays blocked until a
    person checks the account and removes the file.
    """
    path = _draft_path(draft_id, ".lock")
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except FileExistsError:
        raise DraftError(
            "Another publish attempt holds this draft, or one was interrupted. Check "
            f"the Instagram account before removing drafts/{draft_id}.lock."
        ) from None
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
