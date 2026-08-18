import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from .. import config
from . import GatewayError

STOP_TIMEOUT_S = 10
BUILD_HINT = "meeting capture helper not built — run: vtn setup"
# what the helper prefixes a reading with, ten times a second while it records:
# `level<TAB>system dBFS<TAB>mic dBFS`
LEVEL = "level\t"

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

    def __init__(
        self,
        proc: "subprocess.Popen[str]",
        levels: Callable[[float, float], None] | None = None,
    ) -> None:
        self._proc = proc
        self._levels = levels
        self._over = threading.Event()
        # somebody has to keep reading the helper for as long as it talks, and
        # it talks ten times a second once it is measuring: unread, the pipe
        # fills within minutes and the helper blocks forever inside a print,
        # taking the recording with it. A daemon thread so a caller that never
        # stops the recording can still exit.
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self) -> None:
        """Everything the helper says while it records, read as it says it.
        Runs until the helper closes stdout, which is the only thing that can
        be relied on: `stopped` may or may not be the last line, but EOF always
        is. Callbacks run on this thread — whoever is handed a level is being
        handed it here, not on the thread that started the recording."""
        if self._proc.stdout is not None:
            for line in self._proc.stdout:
                self._heard(line.strip())
        self._over.set()

    def _heard(self, line: str) -> None:
        """One line from the helper. Anything unrecognised is dropped rather
        than reported: a newer helper saying more than this one knows about
        must not become an error in the middle of a meeting."""
        if line == "stopped":
            self._over.set()
        elif line.startswith(LEVEL) and self._levels is not None:
            system, tab, mic = line.removeprefix(LEVEL).partition("\t")
            if not tab:
                return
            try:
                reading = (float(system), float(mic))
            except ValueError:
                return
            try:
                self._levels(*reading)
            except Exception:
                # a meter that raises must not take this thread down with it:
                # nobody reading the helper is what blocks the recording, and
                # the levels are the decoration while the meeting is the point
                pass

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
        # the last word comes to the thread that has been reading all along,
        # after however many levels were still in the pipe ahead of it. Bounded
        # so that a helper which says nothing at all still reaches the kill
        # below rather than holding a person here for the rest of the evening
        self._over.wait(STOP_TIMEOUT_S)
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
    levels: Callable[[float, float], None] | None = None,
) -> Recording:
    """Starts taping this Mac: what it is playing into one file, what the
    microphone hears into the other. Returns only once both streams are
    actually live, so a caller may tell the user the meeting is being recorded
    and be telling the truth. The helper keeps its own stderr, which is where
    its permission advice appears as it happens.

    A side left unnamed is recorded from whatever the Mac itself is set to use,
    which is what most people mean and what happens when nobody has chosen.

    Given somewhere to send them, the helper is asked to measure both sides and
    reports how loud each is ten times a second, in dBFS, system first. Only
    then: measuring is a pass over every buffer of the meeting, and a recording
    nobody is watching should not pay for a meter nobody will see. Each reading
    arrives on the thread that reads the helper rather than on this one.

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
    if levels is not None:
        argv += ["--levels"]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
    # read here rather than by the thread below, which only starts once the
    # handshake is done: two readers on one pipe would race for the first word
    if _readline(proc) != "recording":
        raise _failure(proc)
    return Recording(proc, levels)


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
