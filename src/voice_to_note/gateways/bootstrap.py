import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

from . import GatewayError

# every command this app has to run to build itself from source, and where a
# person without it goes to get one
TOOL_HINTS = {
    "git": "xcode-select --install",
    "cmake": "https://cmake.org/download/ or brew install cmake",
    "ffmpeg": "https://evermeet.cx/ffmpeg/ or brew install ffmpeg",
}


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


def clone_whisper(vendor: Path) -> None:
    """Fetches whisper.cpp's source, shallow since only a build comes from it."""
    _run(
        ["git", "clone", "--depth", "1", "https://github.com/ggml-org/whisper.cpp", str(vendor)],
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


def download_model_script(vendor: Path, script: str, args: list[str]) -> None:
    """Runs one of whisper.cpp's own model-download scripts, which ship inside
    its clone rather than in this project."""
    _run(["bash", str(vendor / "models" / script), *args], script)


def fetch(url: str, dst: Path) -> None:
    """Downloads one file under a `.part` name and only then renames it into
    place, so a run killed mid-download never leaves something at `dst` that a
    later run's exists-check would mistake for a finished one."""
    part = dst.with_suffix(dst.suffix + ".part")
    try:
        with urllib.request.urlopen(url) as r:
            part.write_bytes(r.read())
    except (urllib.error.URLError, OSError) as e:
        raise GatewayError(f"download failed: {url}: {e}") from e
    part.rename(dst)


def fetch_tar_bz2(url: str, dst_dir: Path) -> None:
    """Downloads a `.tar.bz2` release archive and unpacks it in place, the way
    sherpa-onnx ships its models: as an archive containing its own directory."""
    tmp = dst_dir / f"{Path(url).name}.part"
    try:
        with urllib.request.urlopen(url) as r:
            tmp.write_bytes(r.read())
        with tarfile.open(tmp, "r:bz2") as tar:
            tar.extractall(dst_dir, filter="data")
    except (urllib.error.URLError, OSError, tarfile.TarError) as e:
        raise GatewayError(f"download failed: {url}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
