import os
import shutil
import subprocess
import sys

# a nice value this high asks the scheduler to run the work only when nothing
# more interactive wants the CPU, without starving it outright
BACKGROUND_NICENESS = 19

# a machine that will not answer this in a second is not going to answer it
SYSCTL_TIMEOUT_S = 1


def background(cmd: list[str]) -> list[str]:
    """Wraps a command so heavy audio work never makes the Mac feel stuck
    while a memo processes. macOS's `taskpolicy -c utility` is preferred over
    its own "background" class: background QoS pins work to the efficiency
    cores and can multiply transcription time several-fold, while utility
    still yields to the interactive UI but stays eligible for the performance
    cores. `nice` is the portable fallback where taskpolicy is unavailable,
    and an unmodified command is returned when neither tool exists rather
    than fail the work outright."""
    if shutil.which("taskpolicy"):
        return ["taskpolicy", "-c", "utility", *cmd]
    if shutil.which("nice"):
        return ["nice", "-n", str(BACKGROUND_NICENESS), *cmd]
    return list(cmd)


def lower_priority() -> None:
    """Drops the calling process's own scheduling priority, for work that
    runs in-process rather than as a subprocess — a ProcessPoolExecutor
    initializer, for instance. A platform that refuses or lacks the call is
    left at its default priority rather than treated as a failure: a child
    that cannot lower itself must still do the work it was started for."""
    try:
        os.setpriority(os.PRIO_PROCESS, 0, BACKGROUND_NICENESS)
    except (OSError, AttributeError):
        pass


def performance_cores() -> int:
    """How many cores heavy model work should be spread over. Apple silicon
    runs this kind of work on its efficiency cores at roughly half the speed of
    its performance ones, so a thread per core counts in cores that finish
    late: the same work is spread thinner rather than finished sooner.
    `hw.perflevel0.logicalcpu` is the count of the fast ones. Anywhere else —
    an Intel Mac, a Linux box, a sysctl that answers something unexpected —
    every core counts, which is the honest answer where there is no split."""
    if sys.platform == "darwin":
        try:
            found = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.logicalcpu"],
                capture_output=True, text=True, timeout=SYSCTL_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            found = None
        if found is not None and found.returncode == 0 and found.stdout.strip().isdigit():
            return max(1, int(found.stdout.strip()))
    return os.cpu_count() or 1
