"""Guide one video from your computer to a reviewed, optionally published Instagram Reel.

Double-click "Shambhala Reel Creator.bat" to run this. It renders with reel_creator.py
and talks to instagram_mcp_server.py as an ordinary MCP client, so every check stays in
the server. This file has no Instagram network code and can't publish by itself.

Publishing is off unless you choose it in this run. Only then does it ask for the token
at a hidden prompt, and it passes the token and live mode to one server process. Neither
is saved, and neither is taken from your environment. The server's confirmation request
is shown to you exactly as sent, and only what you type goes back to it.

Run with --self-check to test the setup. That sends nothing to Instagram and needs no token.
"""

import asyncio
import getpass
import json
import os
import re
import sys
import warnings
from pathlib import Path

import instagram_drafts as drafts
from instagram_config import ACCOUNT_ENV_VAR, configured_account_id
import reel_creator as reel
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import ElicitResult

BASE_DIR = Path(__file__).resolve().parent
# What runs as the MCP server. Tests replace this with a version that has no route to Meta.
SERVER_ARGS = [str(BASE_DIR / "instagram_mcp_server.py")]
PUBLISH_MODE = "live"
MODE_SETTING = "IG_PUBLISHING_MODE"
TOKEN_SETTING = "IG_ACCESS_TOKEN"

DEFAULT_TOPIC = "Creativity meets technology"
MAX_TOPIC_LENGTH = 200
BASE_HASHTAGS = ("ShambhalaTech", "DigitalInnovation", "WebDevelopment")
MAX_TOPIC_HASHTAGS = 3
_HASHTAG_SKIP_WORDS = {"about", "from", "into", "meets", "that", "this", "with", "your", "their", "where"}


class StopLauncher(Exception):
    """End this run. The message is safe to show."""


def refresh_path():
    """Add the saved PATH, so FFmpeg is found even if Windows hasn't passed on a new install."""
    try:
        import winreg
    except ImportError:
        return
    saved = []
    for root, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(root, key) as handle:
                saved.append(os.path.expandvars(winreg.QueryValueEx(handle, "Path")[0]))
        except OSError:
            continue
    current = os.environ.get("PATH", "")
    known = {item.lower() for item in current.split(os.pathsep)}
    extra = [
        item
        for value in saved
        for item in value.split(os.pathsep)
        if item and item.lower() not in known
    ]
    if extra:
        os.environ["PATH"] = os.pathsep.join([current, *extra])


def ask_yes(prompt):
    """Return True only for an explicit yes. Pressing Enter means no."""
    return input(f"{prompt} [y/N]: ").strip().lower() in {"y", "yes"}


def ask_number(prompt, default, low, high):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip() or str(default)
        if answer.isdecimal() and low <= int(answer) <= high:
            return int(answer)
        print(f"Enter a whole number from {low} to {high}.")


def choose_video():
    """Return the chosen video's path, using a file window if possible."""
    try:
        import tkinter
        from tkinter import filedialog

        window = tkinter.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        patterns = " ".join(f"*{extension}" for extension in sorted(reel.VIDEO_EXTENSIONS))
        chosen = filedialog.askopenfilename(
            parent=window,
            title="Choose a video for your Reel",
            filetypes=[("Video files", patterns)],
        )
        window.destroy()
    except (ImportError, RuntimeError, OSError) as error:
        print(f"The file window couldn't open ({type(error).__name__}).")
        chosen = input("Paste the path to your video: ")
    if not chosen or not str(chosen).strip():
        raise StopLauncher("No video was chosen, so nothing was done.")
    return reel.resolve_video(chosen)


def choose_track(video_duration, ffprobe):
    """Return the file name of a licensed track. Only tracks with complete records are offered."""
    moods = reel.parse_moods(input("Mood for the music, such as relaxed or uplifting (optional): "))
    recommended, not_usable = reel.recommend_tracks(video_duration, moods, ffprobe)
    reel.print_recommendations(recommended, not_usable)
    if not recommended:
        raise StopLauncher("There's no approved music, so nothing was rendered.")
    return recommended[ask_number("Track number", 1, 1, len(recommended)) - 1]["file"]


def open_preview(path):
    """Open the preview in your default video player."""
    try:
        os.startfile(path)  # Windows only. Other systems reach the message below.
    except (AttributeError, OSError):
        print("The preview couldn't be opened automatically, so open the file above yourself.")


def render_and_review(video_path, video_info, ffprobe):
    """Render until you accept a preview, and return its report. Stops if you stop."""
    track = choose_track(video_info["duration"], ffprobe)
    while True:
        music_volume = ask_number("Music volume, 1-100", 30, 1, 100)
        original_volume = 0
        if video_info["has_audio"]:
            original_volume = ask_number("Original video sound, 0-100 (0 mutes it)", 100, 0, 100)
        answer = input("Fit to 9:16 with bars (pad) or by cropping (crop)? [pad]: ").strip().lower()
        print("Rendering. This can take a few minutes.")
        report = reel.render_reel(
            video_path, track, music_volume, original_volume, answer if answer in {"pad", "crop"} else "pad"
        )
        reel.print_report(report)
        if report["problems"]:
            raise StopLauncher("This preview didn't pass its checks, so it can't be used.")
        open_preview(report["preview_path"])
        print("\nWatch the preview with sound.\n  1. It looks and sounds right\n  2. Render again with new settings\n  3. Stop")
        choice = ask_number("Choose", 3, 1, 3)
        if choice == 1:
            return report
        if choice == 3:
            raise StopLauncher(f"Stopped. Your preview is saved at {report['preview_path']}")
        if ask_yes("Choose a different track?"):
            track = choose_track(video_info["duration"], ffprobe)


def _clean_topic(topic):
    """Remove anything from a topic that would change a caption's hashtags or mentions."""
    text = "".join(char if char.isprintable() else " " for char in str(topic or ""))
    text = re.sub(r"[#@]", "", text)
    text = re.sub(r"\s+", " ", text).strip().rstrip(".!?,;: ")
    return text[:MAX_TOPIC_LENGTH].rstrip()


def suggest_hashtags(topic):
    """Return the brand's hashtags plus a few built from the words of the topic."""
    tags = []
    seen = set()
    words = [
        word
        for word in re.findall(r"[^\W_]{4,}", _clean_topic(topic))
        if word.lower() not in _HASHTAG_SKIP_WORDS
    ][:MAX_TOPIC_HASHTAGS]
    for tag in (*BASE_HASHTAGS[:1], *(word.capitalize() for word in words), *BASE_HASHTAGS[1:]):
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(f"#{tag}")
    return " ".join(tags)


def suggest_captions(topic):
    """Return three captions from fixed templates. This doesn't use an AI model or the network."""
    subject = _clean_topic(topic) or DEFAULT_TOPIC
    tags = suggest_hashtags(subject)
    return [
        f"Shambhala Tech | {subject}. Stay tuned for more!\n\n{tags}",
        f"{subject}.\n\nWe build modern websites and digital experiences that help brands stand out. "
        f"DM us to start your project.\n\n{tags}",
        f"From idea to reality: {subject}.\n\nFollow Shambhala Tech for more of our work.\n\n{tags}",
    ]


def choose_caption():
    """Return the caption you chose or wrote, after the same checks the server applies."""
    captions = suggest_captions(input(f"What is this Reel about? [{DEFAULT_TOPIC}]: "))
    print("\nSuggested captions (from templates, so check they fit your video):")
    for number, caption in enumerate(captions, 1):
        print(f"\n--- Option {number} ---\n{caption}")
    print(f"\n--- Option {len(captions) + 1} ---\nWrite your own")
    while True:
        choice = ask_number("\nChoose a caption", 1, 1, len(captions) + 1)
        caption = captions[choice - 1] if choice <= len(captions) else input("Your caption (one line): ").strip()
        errors, warnings_found = drafts.validate_caption(caption)
        if not errors:
            for warning in warnings_found:
                print(warning)
            return caption
        print(" ".join(errors))


def read_access_token():
    """Ask for the token at a hidden prompt. It isn't stored, and is never shown."""
    print("\nEnter your Instagram access token. Nothing appears as you type or paste.")
    print("To paste, right-click in this window. Ctrl+V may not work at this prompt.")
    # If this window can't hide input, getpass only warns and then shows what is typed.
    # Treating the warning as an error means the token is never displayed.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass("Access token: ").strip()
        except getpass.GetPassWarning:
            raise StopLauncher("This window can't hide input, so no token was read.") from None
    if not token:
        raise StopLauncher("No token was entered, so nothing was published.")
    if not all(33 <= ord(char) <= 126 for char in token):
        raise StopLauncher(
            "The token contains invalid characters, so nothing was published. Copy it again, and "
            "right-click to paste."
        )
    return token


def build_server_env(token=None):
    """Return the only Instagram settings the server gets. Your environment isn't used.

    Without a token, publishing is off. The SDK adds basic Windows settings to these.
    """
    env = {"PYTHONUTF8": "1"}
    account_id = configured_account_id()
    if account_id is not None:
        env[ACCOUNT_ENV_VAR] = account_id
    if token:
        env[MODE_SETTING] = PUBLISH_MODE
        env[TOKEN_SETTING] = token
    return env


def _server_transport(env, errors):
    return stdio_client(
        StdioServerParameters(command=sys.executable, args=SERVER_ARGS, env=env, cwd=str(BASE_DIR)),
        errlog=errors,
    )


def _output_of(result):
    """Return (is_error, text) for a tool result."""
    text = result.content[0].text if result.content else ""
    return bool(result.is_error), text


async def _prepare(file_name, caption):
    with open(os.devnull, "w") as errors:
        async with Client(_server_transport(build_server_env(), errors)) as client:
            return _output_of(
                await client.call_tool(
                    "prepare_instagram_local_reel", {"file_name": file_name, "caption": caption}
                )
            )


def prepare_draft(file_name, caption):
    """Ask the server to validate the preview and save a draft. This uses no token or network."""
    is_error, text = asyncio.run(_prepare(file_name, caption))
    if is_error:
        raise StopLauncher(f"The draft couldn't be prepared. {text}")
    body = json.loads(text)
    if not body.get("valid"):
        raise StopLauncher(f"The draft couldn't be prepared. {' '.join(body.get('errors', []))}")
    return body


async def show_confirmation_request(context, params):
    """Show the server's request exactly, and return only what a person types here.

    This never works out the phrase. Pressing Enter declines.
    """
    print("\n" + "=" * 70)
    print(params.message)
    print("=" * 70)
    typed = await asyncio.to_thread(input, "Type the phrase, or press Enter to decline: ")
    if not typed.strip():
        return ElicitResult(action="decline")
    return ElicitResult(action="accept", content={"confirmation": typed})


async def _publish(draft, token):
    with open(os.devnull, "w") as errors:
        async with Client(
            _server_transport(build_server_env(token), errors),
            elicitation_callback=show_confirmation_request,
        ) as client:
            is_error, text = _output_of(
                await client.call_tool(
                    "publish_instagram_draft",
                    {"draft_id": draft["draft_id"], "content_hash": draft["content_hash"]},
                )
            )
            permalink = None
            if not is_error:
                media_id = json.loads(text).get("media_id")
                permalink = await _find_permalink(client, media_id)
            return is_error, text, permalink


async def _find_permalink(client, media_id):
    """Return the new post's link. Failing to find it doesn't change the result."""
    if not media_id:
        return None
    try:
        is_error, text = _output_of(await client.call_tool("get_recent_instagram_posts", {"limit": 10}))
        if not is_error:
            for post in json.loads(text):
                if post.get("id") == media_id:
                    return post.get("permalink")
    except Exception:  # The post is already live, so this must not look like a failure.
        return None
    return None


def publish_draft(draft, token):
    """Make one publish attempt through the server. Returns True if the post is live."""
    print("\nDon't close this window until it finishes. Uploading and processing can take several minutes.")
    try:
        is_error, text, permalink = asyncio.run(_publish(draft, token))
    except Exception as error:
        print(f"\nThe attempt stopped with {type(error).__name__}, so the post's state isn't known.")
        print("Check Instagram before trying again, so you don't post it twice.")
        return False
    text = text.replace(token, "[REDACTED]")
    if is_error:
        print(f"\n{text}")
        print("It won't be retried. Check Instagram before trying again, so you don't post it twice.")
        return False
    body = json.loads(text)
    if body.get("status") != "published":
        print(f"\nNot published. Status: {body.get('status')}")
        return False
    print("\nYour Reel is live.")
    print(f"Post ID: {body.get('media_id')}")
    print(f"Link: {permalink or 'Open the account in Instagram to find it.'}")
    for warning in body.get("warnings", []):
        print(f"Warning: {warning}")
    return True


def offer_publishing(draft):
    """Ask whether to publish. Only a yes leads to the token prompt and live mode."""
    local_file = draft["local_file"]
    print("\nDraft ready. Nothing has been uploaded or published.")
    print(f"Draft ID: {draft['draft_id']}")
    print(f"File: {local_file['name']} ({local_file['size']} bytes)")
    print(f"Caption:\n{draft['caption'] or '(no caption)'}")
    account_id = configured_account_id()
    if account_id is None:
        print(f"\nTo publish, set {ACCOUNT_ENV_VAR} to your own numeric Instagram professional account ID and restart the launcher.")
        return False
    print(f"\nPublishing makes this public on Instagram account ID {account_id}.")
    if not ask_yes("Publish this draft now?"):
        print("Not published. Publishing stayed off, and no token was requested.")
        return False
    token = read_access_token()
    try:
        return publish_draft(draft, token)
    finally:
        del token


async def _list_server_tools():
    with open(os.devnull, "w") as errors:
        async with Client(_server_transport(build_server_env(), errors)) as client:
            is_error, text = _output_of(
                await client.call_tool(
                    "publish_instagram_draft", {"draft_id": "0000000000000000", "content_hash": "0" * 64}
                )
            )
            return sorted(tool.name for tool in (await client.list_tools()).tools), is_error, text


def self_check():
    """Check the setup, with no token, dialog or Instagram request. Returns an exit code."""
    problems = 0
    try:
        ffmpeg, ffprobe = reel.find_tools()
        print(f"FFmpeg: found at {ffmpeg}")
    except reel.ReelError as error:
        print(error)
        return 1
    recommended, not_usable = reel.recommend_tracks(None, [], ffprobe)
    print(f"Approved music tracks: {len(recommended)}")
    for track in not_usable:
        print(f"  Can't be used: {track['file']}: {track['reason']}")
    if not recommended:
        problems += 1
    tools, refused, text = asyncio.run(_list_server_tools())
    print(f"Server tools: {len(tools)}")
    if "publish_instagram_draft" not in tools or "prepare_instagram_local_reel" not in tools:
        print("  The server is missing a tool this launcher needs.")
        problems += 1
    if refused and "Publishing is off" in text:
        print("Publishing by default: off")
    else:
        print("  Publishing wasn't reported as off.")
        problems += 1
    print("Setup looks good." if not problems else f"Problems found: {problems}")
    return 1 if problems else 0


def run():
    """Take one video from selection to review, and publish only if you choose to."""
    _, ffprobe = reel.find_tools()
    print("Choose your video in the window that opens.")
    video_path = choose_video()
    print(f"Video: {video_path}")
    video_info = reel.probe_media(video_path, ffprobe)
    if not video_info["has_video"]:
        raise StopLauncher(f"{video_path.name} has no video stream.")
    report = render_and_review(video_path, video_info, ffprobe)
    caption = choose_caption()
    draft = prepare_draft(Path(report["preview_path"]).name, caption)
    return offer_publishing(draft)


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    refresh_path()
    reel.MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    reel.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if "--self-check" in arguments:
            return self_check()
        run()
        return 0
    except (StopLauncher, reel.ReelError) as error:
        print(f"\n{error}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nStopped. If a publish attempt had started, check Instagram before trying again.")
        return 1
    except Exception as error:
        # Only the type is shown, because an unknown error's text could hold anything.
        print(f"\nStopped because of an unexpected {type(error).__name__}.")
        print("If a publish attempt had started, check Instagram before trying again.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
