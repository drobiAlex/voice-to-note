import json
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import cast

from .. import config
from ..transforms.youtube import VideoInfo
from . import GatewayError

INSTALL_HINT = "install yt-dlp: brew install yt-dlp"


def available() -> bool:
    """Whether the fetcher is on this machine at all, answered without ever
    raising: a probe run ahead of an import has no error worth stopping for —
    the import itself will say what is missing."""
    return shutil.which("yt-dlp") is not None


def video_info(url: str) -> VideoInfo:
    """What YouTube says about one video — title, duration, and every caption
    track on offer — without downloading a byte of media. yt-dlp is asked as
    a subprocess rather than imported so the user can keep it current with
    brew as YouTube changes things underneath it, on its own schedule instead
    of this app's. A refusal here is also where a private, deleted or
    age-gated video surfaces, as yt-dlp's own words."""
    try:
        proc = subprocess.run(
            [
                "yt-dlp", "--skip-download", "--no-playlist",
                "--dump-single-json", url,
            ],
            capture_output=True, text=True, timeout=config.YOUTUBE_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise GatewayError(f"yt-dlp not found — {INSTALL_HINT}") from e
    except subprocess.TimeoutExpired as e:
        raise GatewayError(
            f"yt-dlp timed out after {config.YOUTUBE_TIMEOUT_S}s reading {url}"
        ) from e
    if proc.returncode != 0:
        # a stale yt-dlp is the usual cause: YouTube changes its site faster
        # than any release cycle, and the fix is an upgrade, not a bug report
        raise GatewayError(
            f"yt-dlp failed reading {url}:\n{proc.stderr[-2000:]}\n"
            "if the video plays in a browser, try: brew upgrade yt-dlp"
        )
    try:
        return cast(VideoInfo, json.loads(proc.stdout))
    except ValueError as e:
        raise GatewayError(f"yt-dlp returned unreadable JSON for {url}") from e


def captions(track_url: str) -> str:
    """One caption track's text, fetched in json3 — the one format whose
    events arrive already deduplicated, where the VTT of an auto-generated
    track repeats every line as it rolls in word by word. The format is
    forced onto the URL rather than trusted to be there, since yt-dlp lists
    whatever formats YouTube offered and json3 is not always among them."""
    url = track_url
    if "fmt=json3" not in url:
        url += ("&" if "?" in url else "?") + "fmt=json3"
    try:
        with urllib.request.urlopen(url, timeout=config.YOUTUBE_TIMEOUT_S) as resp:
            return cast(bytes, resp.read()).decode("utf-8")
    except (urllib.error.URLError, OSError) as e:
        # OSError also covers a socket timing out mid-read
        raise GatewayError(
            f"caption download failed: {e}\n"
            "HTTP 429 means YouTube is rate-limiting this address — retry later"
        ) from e
