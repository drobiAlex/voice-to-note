import os
import shutil

# a nice value this high asks the scheduler to run the work only when nothing
# more interactive wants the CPU, without starving it outright
BACKGROUND_NICENESS = 19


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
