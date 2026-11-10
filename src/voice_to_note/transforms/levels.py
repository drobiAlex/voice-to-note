from dataclasses import dataclass, field

# how loud the quieter of the two meters may read and still count as nothing.
# The helper floors its readings at -60 dBFS, and a working microphone in a
# quiet room sits well above that on room tone alone, so a side pinned at the
# floor is a side delivering no audio at all rather than a quiet one
SILENT_DB = -58.0
# how loud the other side has to be for a silent one to be suspicious: a mic
# reading nothing while nobody anywhere is talking is a pause, while a mic
# reading nothing while the call is talking is the failure worth knowing about
TALKING_DB = -45.0
# how long a side has to stay silent while the other talks before it is said to
# have dropped. Long enough that somebody listening without speaking is not
# reported, short enough that a dropped mic is noticed within the same topic
DROPPED_S = 20.0


@dataclass
class Side:
    """One side of a recording, as the meter has been hearing it."""

    name: str
    # how long, in seconds of readings, this side has been silent while the
    # other one talked — reset the moment it makes a sound
    silent_s: float = 0.0
    # whether it is currently reported as dropped
    dropped: bool = False
    # over the whole recording: how long it was silent at all, the longest it
    # stayed dropped, and how many times it dropped
    silent_total_s: float = 0.0
    longest_drop_s: float = 0.0
    drops: int = 0


@dataclass
class LevelWatch:
    """Watches both meters of a recording for a side that has stopped
    delivering audio while the other carries on — the microphone going quiet
    because a headset switched profiles, the system tap going quiet because the
    output device changed underneath it. Pure: it is handed readings, and hands
    back what changed, for whoever owns it to say.

    Readings are taken to arrive evenly spaced, `interval_s` apart, which is
    how the helper sends them. Nothing here keeps the readings themselves, so a
    two hour meeting costs exactly what a two minute one does."""

    interval_s: float = 0.25
    sides: tuple[Side, Side] = field(default_factory=lambda: (Side("system"), Side("mic")))
    readings: int = 0

    def hear(self, system_db: float, mic_db: float) -> list[tuple[str, str, float]]:
        """Takes one reading of both meters. Hands back each side that just
        dropped ("dropped") or just came back ("back") — what happened, which
        side, and how long it had been silent — and usually nothing."""
        self.readings += 1
        changed: list[tuple[str, str, float]] = []
        system, mic = self.sides
        pairs: tuple[tuple[Side, float, float], ...] = (
            (system, system_db, mic_db),
            (mic, mic_db, system_db),
        )
        for side, own, other in pairs:
            if own > SILENT_DB:
                if side.dropped:
                    changed.append(("back", side.name, side.silent_s))
                    side.longest_drop_s = max(side.longest_drop_s, side.silent_s)
                side.silent_s, side.dropped = 0.0, False
                continue
            side.silent_total_s += self.interval_s
            if other > TALKING_DB or side.dropped:
                side.silent_s += self.interval_s
            if not side.dropped and side.silent_s >= DROPPED_S:
                side.dropped = True
                side.drops += 1
                changed.append(("dropped", side.name, side.silent_s))
        return changed

    def summary(self) -> dict[str, float | int]:
        """The whole recording as the meters heard it, for one line at the end:
        how long it ran, and for each side how much of it was silent, how often
        it dropped and the longest drop — still-open drops included."""
        out: dict[str, float | int] = {"metered_s": round(self.readings * self.interval_s, 2)}
        for side in self.sides:
            longest = max(side.longest_drop_s, side.silent_s if side.dropped else 0.0)
            out[f"{side.name}_silent_s"] = round(side.silent_total_s, 2)
            out[f"{side.name}_drops"] = side.drops
            out[f"{side.name}_longest_drop_s"] = round(longest, 2)
        return out
