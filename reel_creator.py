"""Make a local Instagram Reel preview from your own video and licensed music.

This tool only works with local files. It has no network code, doesn't read your
Instagram token, doesn't create drafts and can't publish. It runs FFmpeg, which you
install yourself. It never installs or downloads anything.

A track can only be used if reels/music/licenses.json has an entry for it that
records verified business-use permission and where the proof is. The tool checks
that record exists and is complete. It can't check that the licence is genuine, so
that part stays your responsibility.

Usage:

    .\\.venv\\Scripts\\python.exe reel_creator.py check
    .\\.venv\\Scripts\\python.exe reel_creator.py create
    .\\.venv\\Scripts\\python.exe reel_creator.py music --video C:\\clips\\a.mp4 --mood calm
    .\\.venv\\Scripts\\python.exe reel_creator.py render --video C:\\clips\\a.mp4 \\
        --track piano.mp3 --music-volume 30
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MUSIC_DIR = BASE_DIR / "reels" / "music"
PREVIEW_DIR = BASE_DIR / "reels" / "previews"
LICENSES_FILE_NAME = "licenses.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}

# Types of permission that can cover business use. Non-commercial licences such as
# CC BY-NC are left out on purpose, so they can't be selected.
LICENSE_TYPES = {
    "owned",
    "purchased_license",
    "subscription_license",
    "royalty_free_license",
    "written_permission",
    "cc0",
    "cc_by",
}

# Reel limits from Meta's documentation. The README lists the same requirements.
MIN_DURATION_SECONDS = 3
MAX_DURATION_SECONDS = 15 * 60
MAX_FILE_BYTES = 300 * 1000 * 1000
MAX_BITRATE = 25_000_000
MAX_WIDTH = 1920
MIN_FPS = 23
MAX_FPS = 60
MAX_AUDIO_SAMPLE_RATE = 48_000

OUTPUT_WIDTH = 1080
OUTPUT_HEIGHT = 1920
DEFAULT_FPS = 30
MUSIC_FADE_SECONDS = 1.5
LOW_RESOLUTION_SHORT_SIDE = 720

PROBE_TIMEOUT_SECONDS = 60
RENDER_TIMEOUT_SECONDS = 2 * 60 * 60

STATUS_AWAITING_REVIEW = "awaiting_review"
STATUS_NEEDS_ATTENTION = "needs_attention"

FFMPEG_INSTALL_HELP = """\
FFmpeg isn't installed, or isn't on your PATH. This tool won't install it for you.

To install it on Windows, run this in PowerShell:

    winget install --id Gyan.FFmpeg --exact

Or download a build from https://www.gyan.dev/ffmpeg/builds/ and add its bin folder
to your PATH.

Then close and reopen PowerShell, and run:

    ffmpeg -version
    .\\.venv\\Scripts\\python.exe reel_creator.py check"""

_INTERLACED_FIELD_ORDERS = {"tt", "bb", "tb", "bt"}
_HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


class ReelError(Exception):
    """The reel can't be made. The message is safe to show to the person."""


def find_tools():
    """Return (ffmpeg, ffprobe) paths, or raise with the installation steps."""
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise ReelError(FFMPEG_INSTALL_HELP)
    return ffmpeg, ffprobe


def child_environment():
    """Return this environment without Instagram settings. FFmpeg never needs them."""
    return {name: value for name, value in os.environ.items() if not name.upper().startswith("IG_")}


def run_tool(arguments, timeout):
    """Run FFmpeg or ffprobe without a shell, and return the finished process."""
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_environment(),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        raise ReelError(f"{Path(arguments[0]).stem} took too long and was stopped.") from None
    except OSError as error:
        raise ReelError(f"{Path(arguments[0]).stem} couldn't be started: {error}") from None


def parse_fps(rate):
    """Turn a rate such as 30000/1001 into a number, or None if it's unknown."""
    try:
        top, _, bottom = str(rate).partition("/")
        value = float(top) / float(bottom or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return value if value > 0 else None


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def probe_media(path, ffprobe):
    """Return the facts about a media file that this tool needs."""
    result = run_tool(
        [
            ffprobe, "-v", "error", "-protocol_whitelist", "file",
            "-print_format", "json", "-show_format", "-show_streams", str(path),
        ],
        PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ReelError(f"FFmpeg couldn't read {Path(path).name}. Check that it's a working media file.")
    try:
        body = json.loads(result.stdout)
        streams = body["streams"]
        file_format = body["format"]
    except (ValueError, KeyError, TypeError):
        raise ReelError(f"FFmpeg's report on {Path(path).name} had an unexpected format.") from None

    videos = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
        and not stream.get("disposition", {}).get("attached_pic")
    ]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = videos[0] if videos else None
    audio = audios[0] if audios else None
    duration = _number(file_format.get("duration"))
    if duration is None and video:
        duration = _number(video.get("duration"))
    if duration is None and audio:
        duration = _number(audio.get("duration"))
    return {
        "duration": duration,
        "format_name": file_format.get("format_name", ""),
        "bit_rate": _number(file_format.get("bit_rate")),
        "has_video": video is not None,
        "has_audio": audio is not None,
        "video_index": video.get("index") if video else None,
        "video_codec": video.get("codec_name") if video else None,
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "color_range": video.get("color_range") if video else None,
        "fps": parse_fps(video.get("avg_frame_rate")) if video else None,
        "field_order": video.get("field_order") if video else None,
        "color_transfer": video.get("color_transfer") if video else None,
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_sample_rate": _number(audio.get("sample_rate")) if audio else None,
        "audio_channels": audio.get("channels") if audio else None,
    }


def resolve_video(path_text):
    """Return the path of a local video, accepting a path pasted with quotes."""
    path = Path(str(path_text).strip().strip('"').strip("'")).expanduser()
    if not path.is_file():
        raise ReelError(f"No video file was found at {path}.")
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ReelError(
            f"{path.name} isn't a supported video. Use one of: {', '.join(sorted(VIDEO_EXTENSIONS))}."
        )
    return path.resolve()


def _license_problem(entry, music_dir, today):
    """Return why this licence record can't be used, or None if it's complete."""
    name = entry.get("file")
    if not isinstance(name, str) or not name or Path(name).name != name:
        return "The entry's file must be a plain file name inside the music folder."
    if Path(name).suffix.lower() not in MUSIC_EXTENSIONS:
        return "The file type isn't supported."
    if not (music_dir / name).is_file():
        return "The file isn't in the music folder."
    if entry.get("business_use_verified") is not True:
        return "business_use_verified isn't true."
    if entry.get("license_type") not in LICENSE_TYPES:
        return "license_type isn't one of the types that can cover business use."
    proof = entry.get("proof")
    if not isinstance(proof, str) or not proof.strip():
        return "proof is empty. Record where your licence or permission is kept."
    if entry.get("license_type") == "cc_by":
        attribution = entry.get("attribution")
        if not isinstance(attribution, str) or not attribution.strip():
            return "A cc_by track needs its attribution text."
    expires = entry.get("expires")
    if expires is not None:
        try:
            expiry = date.fromisoformat(expires)
        except (TypeError, ValueError):
            return "expires isn't a date in YYYY-MM-DD format."
        if expiry < today:
            return f"The licence expired on {expires}."
    return None


def load_tracks(music_dir=None, today=None):
    """Return every track, marked eligible or not, including files with no entry."""
    music_dir = Path(music_dir or MUSIC_DIR)
    today = today or date.today()
    entries = []
    licenses_path = music_dir / LICENSES_FILE_NAME
    if licenses_path.is_file():
        try:
            body = json.loads(licenses_path.read_text(encoding="utf-8-sig"))
            entries = body["tracks"]
            if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                raise TypeError
        except (OSError, ValueError, KeyError, TypeError):
            raise ReelError(
                f"{licenses_path} couldn't be read. It must be JSON with a \"tracks\" list. "
                "See music_licenses.example.json."
            ) from None

    tracks = []
    seen = set()
    for entry in entries:
        problem = _license_problem(entry, music_dir, today)
        name = str(entry.get("file"))
        seen.add(name.lower())
        moods = entry.get("moods")
        tracks.append(
            {
                "file": name,
                "title": str(entry.get("title") or name),
                "artist": str(entry.get("artist") or ""),
                "moods": [str(mood).lower() for mood in moods] if isinstance(moods, list) else [],
                "license_type": entry.get("license_type"),
                "proof": entry.get("proof"),
                "expires": entry.get("expires"),
                "attribution": entry.get("attribution"),
                "eligible": problem is None,
                "reason": problem,
            }
        )
    if music_dir.is_dir():
        for path in sorted(music_dir.iterdir()):
            if path.suffix.lower() in MUSIC_EXTENSIONS and path.name.lower() not in seen:
                tracks.append(
                    {
                        "file": path.name,
                        "title": path.name,
                        "artist": "",
                        "moods": [],
                        "license_type": None,
                        "proof": None,
                        "expires": None,
                        "attribution": None,
                        "eligible": False,
                        "reason": f"It has no entry in {LICENSES_FILE_NAME}.",
                    }
                )
    return tracks


def parse_moods(text):
    """Turn "calm, Uplifting" into ["calm", "uplifting"]."""
    return [mood.strip().lower() for mood in str(text or "").split(",") if mood.strip()]


def recommend_tracks(video_duration, moods, ffprobe, music_dir=None, today=None):
    """Return (recommended, not_usable). Only licensed tracks can be recommended.

    This ranks with simple rules: the moods you tagged, then whether the track is long
    enough. It doesn't listen to the audio or use an AI model.
    """
    music_dir = Path(music_dir or MUSIC_DIR)
    recommended = []
    not_usable = []
    for track in load_tracks(music_dir, today):
        if not track["eligible"]:
            not_usable.append(track)
            continue
        try:
            info = probe_media(music_dir / track["file"], ffprobe)
        except ReelError as error:
            not_usable.append({**track, "eligible": False, "reason": str(error)})
            continue
        if not info["has_audio"]:
            not_usable.append({**track, "eligible": False, "reason": "The file has no audio."})
            continue
        matched = [mood for mood in moods if mood in track["moods"]]
        score = len(matched) * 2
        reasons = [f"Matches your mood: {', '.join(matched)}."] if matched else []
        duration = info["duration"]
        if video_duration and duration:
            if duration >= video_duration:
                score += 1
                reasons.append("It's long enough to cover the whole video.")
            else:
                reasons.append(f"It's shorter than the video ({duration:.0f}s), so it would loop.")
        recommended.append({**track, "duration": duration, "score": score, "reasons": reasons})
    recommended.sort(key=lambda item: (-item["score"], item["title"].lower()))
    return recommended, not_usable


def output_fps(video_info):
    """Keep the source frame rate when Meta allows it, otherwise use the nearest."""
    fps = video_info.get("fps")
    if fps is None:
        return DEFAULT_FPS
    return round(min(max(fps, MIN_FPS), MAX_FPS), 3)


def _video_filter(video_info, fit, fps):
    size = f"{OUTPUT_WIDTH}:{OUTPUT_HEIGHT}"
    steps = []
    if video_info.get("field_order") in _INTERLACED_FIELD_ORDERS:
        steps.append("yadif")
    # Phones can record full-range video. format=yuv420p alone doesn't convert it, so
    # the scaler has to, or the output stays full range.
    if fit == "crop":
        steps += [f"scale={size}:force_original_aspect_ratio=increase:out_range=tv", f"crop={size}"]
    else:
        steps += [
            f"scale={size}:force_original_aspect_ratio=decrease:out_range=tv",
            f"pad={size}:(ow-iw)/2:(oh-ih)/2:black",
        ]
    steps += ["setsar=1", f"fps={fps}", "format=yuv420p"]
    return f"[0:{video_info['video_index']}]{','.join(steps)}[v]"


def _audio_filter(duration, music_volume, original_volume, use_original):
    audio_format = "aformat=sample_rates=48000:channel_layouts=stereo"
    limiter = "alimiter=limit=0.95:level=disabled"
    fade_start = max(duration - MUSIC_FADE_SECONDS, 0)
    music = (
        f"[1:a:0]{audio_format},volume={music_volume / 100:.3f},atrim=0:{duration:.3f},"
        f"afade=t=out:st={fade_start:.3f}:d={MUSIC_FADE_SECONDS}"
    )
    if not use_original:
        return f"{music},{limiter}[a]"
    return (
        f"{music}[music];"
        f"[0:a:0]{audio_format},volume={original_volume / 100:.3f}[original];"
        f"[original][music]amix=inputs=2:duration=longest:normalize=0,{limiter}[a]"
    )


def build_render_command(
    ffmpeg, video_path, music_path, output_path, video_info, music_info,
    music_volume, original_volume, fit,
):
    """Return the FFmpeg arguments. They follow the Reel requirements in the README."""
    duration = video_info["duration"]
    fps = output_fps(video_info)
    use_original = video_info["has_audio"] and original_volume > 0
    graph = ";".join(
        [
            _video_filter(video_info, fit, fps),
            _audio_filter(duration, music_volume, original_volume, use_original),
        ]
    )
    music_input = ["-protocol_whitelist", "file"]
    if (music_info.get("duration") or 0) < duration:
        music_input += ["-stream_loop", "-1"]
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-n",
        "-protocol_whitelist", "file", "-i", str(video_path),
        *music_input, "-i", str(music_path),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-t", f"{duration:.3f}",
        # Phone videos can hold GPS and device details, so none of it is copied over.
        "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-c:v", "libx264", "-profile:v", "high", "-level:v", "4.2",
        "-preset", "medium", "-crf", "20", "-maxrate", "20M", "-bufsize", "40M",
        "-pix_fmt", "yuv420p", "-color_range", "tv",
        "-bf", "0", "-g", str(round(fps * 2)), "-flags", "+cgop",
        "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-use_editlist", "0",
        str(output_path),
    ]


def _boxes(file, start, end):
    """Yield (name, body_start, box_end) for each MP4 box between start and end."""
    position = start
    while position + 8 <= end:
        file.seek(position)
        size, name = struct.unpack(">I4s", file.read(8))
        header = 8
        if size == 1:
            size = struct.unpack(">Q", file.read(8))[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield name.decode("latin-1"), position + header, position + size
        position += size


def read_mp4_layout(path):
    """Return the top-level MP4 box order, and whether any track has an edit list."""
    end = Path(path).stat().st_size
    top_level = []
    has_edit_list = False
    with open(path, "rb") as file:
        for name, body, box_end in list(_boxes(file, 0, end)):
            top_level.append(name)
            if name != "moov":
                continue
            for track, track_body, track_end in list(_boxes(file, body, box_end)):
                if track != "trak":
                    continue
                for edits, edits_body, edits_end in list(_boxes(file, track_body, track_end)):
                    if edits != "edts":
                        continue
                    if any(item == "elst" for item, _, _ in _boxes(file, edits_body, edits_end)):
                        has_edit_list = True
    return {"top_level": top_level, "has_edit_list": has_edit_list}


def check_reel_file(path, info):
    """Return (problems, warnings) from checking a finished file against Meta's limits."""
    problems = []
    warnings = []
    size = Path(path).stat().st_size
    if size > MAX_FILE_BYTES:
        problems.append(f"The file is {size / 1_000_000:.0f} MB. The limit is 300 MB.")
    if "mp4" not in info["format_name"] and "mov" not in info["format_name"]:
        problems.append("The container isn't MP4 or MOV.")
    duration = info["duration"]
    if duration is None or not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        problems.append("The duration must be 3 seconds to 15 minutes.")
    if info["video_codec"] != "h264":
        problems.append("The video isn't H.264.")
    if info["pix_fmt"] != "yuv420p":
        problems.append(f"The video is {info['pix_fmt']}, not limited-range 4:2:0 (yuv420p).")
    elif info.get("color_range") == "pc":
        problems.append("The video is labelled full range, not limited range.")
    if info["field_order"] in _INTERLACED_FIELD_ORDERS:
        problems.append("The video isn't progressive.")
    if info["fps"] is None or not MIN_FPS <= info["fps"] <= MAX_FPS:
        problems.append("The frame rate must be 23-60 FPS.")
    if not info["width"] or info["width"] > MAX_WIDTH:
        problems.append("The width must be at most 1920 px.")
    if info["bit_rate"] and info["bit_rate"] > MAX_BITRATE:
        problems.append("The bitrate is over 25 Mbps.")
    if info["audio_codec"] != "aac":
        problems.append("The file has no AAC audio.")
    if info["audio_sample_rate"] and info["audio_sample_rate"] > MAX_AUDIO_SAMPLE_RATE:
        problems.append("The audio sample rate is over 48 kHz.")
    if info["audio_channels"] not in {1, 2}:
        problems.append("The audio must be mono or stereo.")
    try:
        layout = read_mp4_layout(path)
    except (OSError, struct.error):
        problems.append("The MP4 structure couldn't be read.")
    else:
        names = layout["top_level"]
        if "moov" not in names or "mdat" not in names or names.index("moov") > names.index("mdat"):
            problems.append("The moov atom isn't at the front of the file.")
        if layout["has_edit_list"]:
            problems.append("The file contains an edit list.")
    if info["width"] and info["height"] and info["width"] * 16 != info["height"] * 9:
        warnings.append("The video isn't 9:16, which Meta recommends.")
    return problems, warnings


def _source_warnings(video_info, music_info):
    warnings = []
    short_side = min(video_info.get("width") or 0, video_info.get("height") or 0)
    if short_side and short_side < LOW_RESOLUTION_SHORT_SIDE:
        warnings.append(f"The source video is low resolution ({short_side}p), so it may look soft.")
    if video_info.get("color_transfer") in _HDR_TRANSFERS:
        warnings.append("The source video is HDR, and its colours may look washed out. Check the preview.")
    if (music_info.get("duration") or 0) < video_info["duration"]:
        warnings.append("The track is shorter than the video, so it loops. Listen for the join.")
    return warnings


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _volume(value, low, name):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= 100:
        raise ReelError(f"{name} must be a whole number from {low} to 100.")
    return value


def _output_path(video_path, output_dir):
    stem = _SAFE_NAME.sub("-", video_path.stem).strip("-")[:40] or "reel"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return Path(output_dir) / f"{stem}-{stamp}-{secrets.token_hex(2)}.mp4"


def render_reel(
    video, track, music_volume=30, original_volume=100, fit="pad",
    music_dir=None, output_dir=None, today=None,
):
    """Render a preview and write its review record. Returns that record.

    A track that isn't eligible stops this before FFmpeg runs. The source files are
    only read, and an existing file is never overwritten.
    """
    music_dir = Path(music_dir or MUSIC_DIR)
    output_dir = Path(output_dir or PREVIEW_DIR)
    music_volume = _volume(music_volume, 1, "Music volume")
    original_volume = _volume(original_volume, 0, "Original audio volume")
    if fit not in {"pad", "crop"}:
        raise ReelError("fit must be pad or crop.")
    ffmpeg, ffprobe = find_tools()
    video_path = resolve_video(video)

    selected = next(
        (item for item in load_tracks(music_dir, today) if item["file"].lower() == str(track).lower()),
        None,
    )
    if selected is None:
        raise ReelError(f"{track} isn't in the music folder or {LICENSES_FILE_NAME}.")
    if not selected["eligible"]:
        raise ReelError(f"{selected['file']} can't be used. {selected['reason']}")
    music_path = music_dir / selected["file"]

    video_info = probe_media(video_path, ffprobe)
    if not video_info["has_video"]:
        raise ReelError(f"{video_path.name} has no video stream.")
    duration = video_info["duration"]
    if duration is None or not MIN_DURATION_SECONDS <= duration <= MAX_DURATION_SECONDS:
        raise ReelError("The video must be 3 seconds to 15 minutes long. Trim it, then try again.")
    music_info = probe_media(music_path, ffprobe)
    if not music_info["has_audio"]:
        raise ReelError(f"{selected['file']} has no audio.")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = _output_path(video_path, output_dir)
    command = build_render_command(
        ffmpeg, video_path, music_path, output_path, video_info, music_info,
        music_volume, original_volume, fit,
    )
    try:
        result = run_tool(command, RENDER_TIMEOUT_SECONDS)
    except ReelError:
        output_path.unlink(missing_ok=True)
        raise
    if result.returncode != 0 or not output_path.is_file():
        output_path.unlink(missing_ok=True)
        details = "\n".join((result.stderr or "").strip().splitlines()[-8:])
        raise ReelError(f"FFmpeg couldn't render the reel.\n{details}".rstrip())

    problems, warnings = check_reel_file(output_path, probe_media(output_path, ffprobe))
    report = {
        "status": STATUS_NEEDS_ATTENTION if problems else STATUS_AWAITING_REVIEW,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "preview_file": output_path.name,
        "preview_sha256": _sha256(output_path),
        "source_video": str(video_path),
        "duration_seconds": duration,
        "music": {
            "file": selected["file"],
            "title": selected["title"],
            "artist": selected["artist"],
            "license_type": selected["license_type"],
            "proof": selected["proof"],
            "expires": selected["expires"],
            "attribution": selected["attribution"],
            "sha256": _sha256(music_path),
        },
        "settings": {
            "music_volume": music_volume,
            "original_volume": original_volume if video_info["has_audio"] else None,
            "fit": fit,
        },
        "problems": problems,
        "warnings": _source_warnings(video_info, music_info) + warnings,
    }
    record_path = output_path.with_suffix(".json")
    record_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["preview_path"] = str(output_path)
    report["record_path"] = str(record_path)
    return report


def print_recommendations(recommended, not_usable):
    if recommended:
        print("Recommended tracks (based on your licence records):")
        for number, track in enumerate(recommended, 1):
            artist = f" by {track['artist']}" if track["artist"] else ""
            print(f"  {number}. {track['title']}{artist} [{track['file']}] ({track['license_type']})")
            for reason in track["reasons"]:
                print(f"     {reason}")
    else:
        print(
            f"No usable tracks. Put music you have permission to use in {MUSIC_DIR}, and record it "
            f"in {LICENSES_FILE_NAME}. See music_licenses.example.json."
        )
    if not_usable:
        print("Can't be used:")
        for track in not_usable:
            print(f"  - {track['file']}: {track['reason']}")


def print_report(report):
    print(f"\nPreview: {report['preview_path']}")
    print(f"Record:  {report['record_path']}")
    print(f"Status:  {report['status']}")
    for problem in report["problems"]:
        print(f"  Problem: {problem}")
    for warning in report["warnings"]:
        print(f"  Warning: {warning}")
    if report["problems"]:
        print("This file doesn't meet the Reel requirements.")
    else:
        print("Watch the preview with sound. Nothing has been published or sent anywhere.")


def _ask_number(prompt, default, low, high):
    while True:
        answer = input(f"{prompt} [{default}]: ").strip() or str(default)
        if answer.isdecimal() and low <= int(answer) <= high:
            return int(answer)
        print(f"Enter a whole number from {low} to {high}.")


def run_interactive():
    """Ask for the video, track and volume, then render a preview."""
    _, ffprobe = find_tools()
    video_path = resolve_video(input("Path to your video: "))
    video_info = probe_media(video_path, ffprobe)
    if not video_info["has_video"]:
        raise ReelError(f"{video_path.name} has no video stream.")
    moods = parse_moods(input("Mood for the music, such as calm or uplifting (optional): "))
    recommended, not_usable = recommend_tracks(video_info["duration"], moods, ffprobe)
    print_recommendations(recommended, not_usable)
    if not recommended:
        return None
    choice = _ask_number("Track number", 1, 1, len(recommended))
    music_volume = _ask_number("Music volume, 1-100", 30, 1, 100)
    original_volume = 0
    if video_info["has_audio"]:
        original_volume = _ask_number("Original video sound, 0-100 (0 mutes it)", 100, 0, 100)
    answer = input("Fit to 9:16 with bars (pad) or by cropping (crop)? [pad]: ").strip().lower()
    print("Rendering. This can take a few minutes.")
    return render_reel(
        video_path, recommended[choice - 1]["file"], music_volume, original_volume,
        answer if answer in {"pad", "crop"} else "pad",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Make a local Reel preview. This never publishes or uploads anything."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Check whether FFmpeg is installed. Installs nothing.")
    commands.add_parser("create", help="Answer questions, then render a preview.")
    music = commands.add_parser("music", help="Recommend licensed tracks.")
    music.add_argument("--video", help="The video, to check that tracks are long enough.")
    music.add_argument("--mood", default="", help="Moods separated by commas, such as calm,uplifting.")
    render = commands.add_parser("render", help="Render a preview with a chosen track.")
    render.add_argument("--video", required=True)
    render.add_argument("--track", required=True, help="File name in reels/music.")
    render.add_argument("--music-volume", type=int, default=30, help="1-100. The default is 30.")
    render.add_argument("--original-volume", type=int, default=100, help="0-100. 0 mutes the video's own sound.")
    render.add_argument("--fit", choices=["pad", "crop"], default="pad", help="How to reach 9:16.")
    return parser


def main(arguments=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    options = build_parser().parse_args(arguments)
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if options.command == "check":
            ffmpeg, ffprobe = find_tools()
            print(f"FFmpeg found: {ffmpeg}\nffprobe found: {ffprobe}")
            return 0
        if options.command == "music":
            _, ffprobe = find_tools()
            video_duration = None
            if options.video:
                video_duration = probe_media(resolve_video(options.video), ffprobe)["duration"]
            print_recommendations(*recommend_tracks(video_duration, parse_moods(options.mood), ffprobe))
            return 0
        if options.command == "create":
            report = run_interactive()
            if report is None:
                return 1
        else:
            report = render_reel(
                options.video, options.track, options.music_volume,
                options.original_volume, options.fit,
            )
        print_report(report)
        return 1 if report["problems"] else 0
    except ReelError as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
