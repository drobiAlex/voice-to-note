import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import GatewayError

# every command this app has to run to build itself from source, and where a
# person without it goes to get one
TOOL_HINTS = {
    "git": "xcode-select --install",
    "cmake": "https://cmake.org/download/ or brew install cmake",
    "ffmpeg": "https://evermeet.cx/ffmpeg/ or brew install ffmpeg",
}

_CHUNK = 1 << 20  # 1 MB: small enough to report progress without slowing a download


def require_tools() -> None:
    """Refuses to start setup on a machine missing something it has to shell
    out to, naming the tool and how to get it rather than failing mid-build."""
    for tool, hint in TOOL_HINTS.items():
        if shutil.which(tool) is None:
            raise GatewayError(f"missing: {tool} — install: {hint}")


def _run(cmd: list[str], what: str) -> None:
    """Runs one setup step, turning a nonzero exit into something the user can
    read: which step failed, and the tail of what the tool said about it."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise GatewayError(f"{what} failed: {e}") from e
    if proc.returncode != 0:
        raise GatewayError(f"{what} failed:\n{proc.stderr[-2000:]}")


DEFAULT_WHISPER_REPO_URL = "https://github.com/ggml-org/whisper.cpp"


def clone_whisper(vendor: Path, repo_url: str = DEFAULT_WHISPER_REPO_URL) -> None:
    """Fetches whisper.cpp's source, shallow since only a build comes from it.
    The caller names the repo to clone rather than this gateway reaching for
    config itself — a fork or a mirror is a setup-time choice, not this
    module's to know about."""
    _run(
        ["git", "clone", "--depth", "1", repo_url, str(vendor)],
        "cloning whisper.cpp",
    )


def build_whisper(vendor: Path) -> None:
    """Builds whisper.cpp in release mode, once its source is on disk."""
    build = vendor / "build"
    _run(
        ["cmake", "-S", str(vendor), "-B", str(build), "-DCMAKE_BUILD_TYPE=Release"],
        "configuring whisper.cpp",
    )
    _run(["cmake", "--build", str(build), "-j", "--config", "Release"], "building whisper.cpp")


def build_capture(source: Path, plist: Path, dst: Path) -> None:
    """Compiles the meeting-capture helper this app records through, which ships
    as Swift source rather than as a binary so that nothing unsigned and
    prebuilt ever lands on a user's machine."""
    if shutil.which("swiftc") is None:
        raise GatewayError("missing: swiftc — install: xcode-select --install")
    dst.parent.mkdir(parents=True, exist_ok=True)
    # the Info.plist is linked into the binary itself: a bundle-less CLI has
    # nowhere else to keep one, and without it macOS asks for microphone and
    # audio access on behalf of an unnamed program the user cannot recognise
    _run(
        [
            "swiftc",
            str(source),
            "-O",
            "-o",
            str(dst),
            "-Xlinker",
            "-sectcreate",
            "-Xlinker",
            "__TEXT",
            "-Xlinker",
            "__info_plist",
            "-Xlinker",
            str(plist),
        ],
        "building vtn-capture",
    )
    # signed under a fixed identifier rather than the ad-hoc default, whose hash
    # changes with every build: macOS attaches the granted permissions to that
    # identifier, so a rebuild under a new one would ask for them all again
    _run(
        ["codesign", "--force", "--sign", "-", "--identifier", "app.vtn.capture", str(dst)],
        "signing vtn-capture",
    )


def build_menubar(source: Path, plist: Path, app: Path) -> None:
    """Compiles the menu bar recorder, which starts a meeting from the menu bar
    instead of from a terminal that has to stay open for the length of a call.
    It is assembled into an app bundle rather than left as a binary because
    macOS attributes recording permission to the bundle a capture is launched
    from — and because only something in a bundle can be opened from the Finder
    or set to log in."""
    if shutil.which("swiftc") is None:
        raise GatewayError("missing: swiftc — install: xcode-select --install")
    binary = app / "Contents" / "MacOS" / "vtn-menubar"
    binary.parent.mkdir(parents=True, exist_ok=True)
    _run(["swiftc", str(source), "-O", "-o", str(binary)], "building vtn-menubar")
    shutil.copyfile(plist, app / "Contents" / "Info.plist")
    # signed under a fixed identifier for the same reason the capture helper is:
    # macOS files the permissions a person grants under it, and the ad-hoc
    # default's hash changes with every build
    _run(
        ["codesign", "--force", "--sign", "-", "--identifier", "app.vtn.menubar", str(app)],
        "signing VTN Recorder.app",
    )


def download_model_script(vendor: Path, script: str, args: list[str]) -> None:
    """Runs one of whisper.cpp's own model-download scripts, which ship inside
    its clone rather than in this project. Left to print straight to the
    terminal rather than captured, since curl's own progress bar is the only
    feedback a multi-gigabyte model download otherwise gives."""
    proc = subprocess.run(["bash", str(vendor / "models" / script), *args])
    if proc.returncode != 0:
        raise GatewayError(f"{script} failed — see output above")


def _no_tick(read: int, total: int) -> None:
    """Default for callers that don't want a download's progress reported."""


def _stream(r: Any, dst: Path, tick: Callable[[int, int], None]) -> None:
    """Copies an open response's body to dst in 1 MB chunks, calling tick after
    each one with the bytes read so far and the total the server advertised
    (0 when it did not say)."""
    total = int(r.headers.get("Content-Length") or 0)
    read = 0
    with dst.open("wb") as f:
        while chunk := r.read(_CHUNK):
            f.write(chunk)
            read += len(chunk)
            tick(read, total)


def fetch(url: str, dst: Path, tick: Callable[[int, int], None] = _no_tick) -> None:
    """Downloads one file under a `.part` name and only then renames it into
    place, so a run killed mid-download never leaves something at `dst` that a
    later run's exists-check would mistake for a finished one."""
    part = dst.with_suffix(dst.suffix + ".part")
    try:
        with urllib.request.urlopen(url) as r:
            _stream(r, part, tick)
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError(f"download failed: {url}: {e}") from e
    part.rename(dst)


def fetch_tar_bz2(
    url: str, dst_dir: Path, tick: Callable[[int, int], None] = _no_tick
) -> None:
    """Downloads a `.tar.bz2` release archive and unpacks it in place, the way
    sherpa-onnx ships its models: as an archive containing its own directory."""
    tmp = dst_dir / f"{Path(url).name}.part"
    try:
        with urllib.request.urlopen(url) as r:
            _stream(r, tmp, tick)
        with tarfile.open(tmp, "r:bz2") as tar:
            tar.extractall(dst_dir, filter="data")
    except (urllib.error.URLError, OSError, tarfile.TarError) as e:
        raise GatewayError(f"download failed: {url}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
