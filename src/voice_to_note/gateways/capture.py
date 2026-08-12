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


def _built() -> None:
    """Refuses anything needing the native helper before setup has compiled it,
    naming the command that would. Shared by every entry point into the helper,
    so none of them can fail as a bare missing-file traceback instead."""
    if not config.CAPTURE_BIN.exists():
        raise GatewayError(BUILD_HINT)


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


def start(
    system: Path,
    mic: Path,
    output_uid: str | None = None,
    input_uid: str | None = None,
) -> Recording:
    """Starts taping this Mac: what it is playing into one file, what the
    microphone hears into the other. Returns only once both streams are
    actually live, so a caller may tell the user the meeting is being recorded
    and be telling the truth. The helper keeps its own stderr, which is where
    its permission advice appears as it happens.

    A side left unnamed is recorded from whatever the Mac itself is set to use,
    which is what most people mean and what happens when nobody has chosen.

    Waiting for that deliberately has no deadline: the first ever run stops at
    the macOS permission prompts, and a person may take minutes to answer them.
    A helper that failed closes stdout, and that silence — not a clock — is
    what says it failed."""
    _built()
    system.parent.mkdir(parents=True, exist_ok=True)
    mic.parent.mkdir(parents=True, exist_ok=True)
    argv = [str(config.CAPTURE_BIN), str(system), str(mic)]
    if output_uid:
        argv += ["--output-uid", output_uid]
    if input_uid:
        argv += ["--input-uid", input_uid]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    if _readline(proc) != "recording":
        raise _failure(proc)
    return Recording(proc)


def devices() -> str:
    """Every audio device a recording could be pointed at, as the helper lists
    them: one tab-separated `direction UID name` line each, in and out counted
    separately. Only the helper can see Core Audio, but asking it what exists
    records nothing and prompts for nothing, so a picker can be filled in
    before anyone has granted permission to record."""
    _built()
    result = subprocess.run(
        [str(config.CAPTURE_BIN), "--list-devices"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise GatewayError(
            result.stderr.strip() or f"vtn-capture exited with code {result.returncode}"
        )
    return result.stdout
