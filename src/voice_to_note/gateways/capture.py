import signal
import subprocess
from pathlib import Path

from .. import config
from . import GatewayError

STOP_TIMEOUT_S = 10
BUILD_HINT = "meeting capture helper not built — run: vtn setup"

# what the helper's own exit codes mean, said in terms of the thing the user has
# to go and click. It has already printed the detail on stderr, which goes
# straight to the terminal, so one line each is enough here
DENIED = {
    3: "microphone access denied — allow vtn in"
    " System Settings → Privacy & Security → Microphone",
    4: "system audio capture failed — allow vtn in"
    " System Settings → Privacy & Security → Screen & System Audio Recording",
}


def _readline(proc: "subprocess.Popen[str]") -> str:
    """One word the helper said, or nothing at all once it has closed stdout —
    which is how a helper that gave up announces itself."""
    return proc.stdout.readline().strip() if proc.stdout else ""


def _failure(proc: "subprocess.Popen[str]") -> GatewayError:
    """Why the helper quit before the recording ever started, as advice rather
    than as a number."""
    code = proc.wait()
    return GatewayError(DENIED.get(code, f"vtn-capture exited with code {code}"))


class Recording:
    """A meeting being taped right now. Owning the helper process is all this
    is for: whoever started the recording can sit through it and then end it
    without knowing how the helper is signalled or what it says on the way
    out."""

    def __init__(self, proc: "subprocess.Popen[str]") -> None:
        self._proc = proc

    def wait(self) -> int:
        """Sits through the meeting. Returning at all means the helper quit by
        itself and the recording is over early, so the code it died with is
        worth reporting — a meeting normally ends with a Ctrl+C, which reaches
        this process as a KeyboardInterrupt instead."""
        return self._proc.wait()

    def stop(self) -> None:
        """Ends the recording and waits for the two wav files to be closed,
        which is what makes them playable. Safe to call on a helper that is
        already gone: a Ctrl+C in a terminal signals the whole foreground
        group, so the helper has usually stopped itself before this is
        reached. A helper that will not go is killed rather than waited on
        forever, since by then both files are as complete as they will get."""
        if self._proc.poll() is None:
            self._proc.send_signal(signal.SIGINT)
        while True:
            line = _readline(self._proc)
            if not line or line == "stopped":
                break
        try:
            self._proc.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


def start(system: Path, mic: Path) -> Recording:
    """Starts taping this Mac: what it is playing into one file, what the
    microphone hears into the other. Returns only once both streams are
    actually live, so a caller may tell the user the meeting is being recorded
    and be telling the truth. The helper keeps its own stderr, which is where
    its permission advice appears as it happens.

    Waiting for that deliberately has no deadline: the first ever run stops at
    the macOS permission prompts, and a person may take minutes to answer them.
    A helper that failed closes stdout, and that silence — not a clock — is
    what says it failed."""
    if not config.CAPTURE_BIN.exists():
        raise GatewayError(BUILD_HINT)
    system.parent.mkdir(parents=True, exist_ok=True)
    mic.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [str(config.CAPTURE_BIN), str(system), str(mic)],
        stdout=subprocess.PIPE,
        text=True,
    )
    if _readline(proc) != "recording":
        raise _failure(proc)
    return Recording(proc)
