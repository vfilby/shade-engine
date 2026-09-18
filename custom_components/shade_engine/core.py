"""Pure actuator decision logic for Shade Engine.

No Home Assistant imports — this module is importable and testable anywhere.

A zone owns one mode, a mode resolves to a target position (a constant or a
clamped passthrough of the calculator's glare position), and ``evaluate``
decides whether to command the covers right now. Suppressed commands are
always deferred, never dropped: the engine re-evaluates on the next tick and
converges once the suppressing condition clears.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Reasons an evaluation may decline to command. Exposed on the target sensor
# so "why didn't it move?" is answerable from the UI.
REASON_COMMAND = "command"
REASON_DISABLED = "disabled"
REASON_HOLD = "hold_active"
REASON_IN_SYNC = "in_sync"
REASON_RATE_LIMITED = "rate_limited"
REASON_NO_TARGET = "no_target"


@dataclass(frozen=True)
class ModeTarget:
    """How one mode resolves to a position.

    Either ``fixed`` is set (constant position), or ``dynamic`` is True and
    the glare position passes through, clamped to [min, max].
    """

    fixed: int | None = None
    dynamic: bool = False
    min: int = 0
    max: int = 100

    def resolve(self, glare_position: int) -> int:
        if not self.dynamic:
            return int(self.fixed)
        return max(self.min, min(self.max, glare_position))


@dataclass(frozen=True)
class MotionConfig:
    """Actuation tuning for one zone. Durations in seconds."""

    deadband: int = 3
    min_interval: float = 300.0
    hold_duration: float = 3600.0
    settle: float = 90.0


@dataclass
class Decision:
    """Result of one zone evaluation."""

    reason: str
    target: int | None = None
    covers: list[str] = field(default_factory=list)


@dataclass
class ZoneCore:
    """Runtime state and decisions for one zone."""

    covers: list[str]
    modes: dict[str, ModeTarget]
    motion: MotionConfig
    mode: str
    enabled: bool = True
    last_commanded: dict[str, int] = field(default_factory=dict)
    last_command_ts: float | None = None
    hold_until: float | None = None
    # Most recent position report per cover. Inside the settle window this
    # is what "converging on the target" is measured against.
    last_reported: dict[str, int] = field(default_factory=dict)

    # -- mode ---------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        if mode not in self.modes:
            raise ValueError(f"unknown mode: {mode}")
        self.mode = mode

    def target(self, glare_position: int) -> int:
        return self.modes[self.mode].resolve(glare_position)

    # -- hold (manual override) --------------------------------------------

    def hold_active(self, now: float) -> bool:
        return self.hold_until is not None and now < self.hold_until

    def start_hold(self, now: float, duration: float | None = None) -> None:
        self.hold_until = now + (
            duration if duration is not None else self.motion.hold_duration
        )

    def release_hold(self) -> None:
        self.hold_until = None

    def report_position(self, cover: str, position: int, now: float) -> bool:
        """Record a cover's reported position; start a hold on a manual move.

        A report is manual when it differs from what we last commanded by
        more than the deadband, unless it arrives inside the settle window
        after our own command *and* is converging on that command: no
        farther from the commanded position than the previous report was
        (plus the deadband for jitter). Intermediate positions of our own
        move converge; a report that moves away from the target inside the
        window is a human and starts a hold. Returns True when a hold was
        started or refreshed.
        """
        previous = self.last_reported.get(cover)
        self.last_reported[cover] = position
        commanded = self.last_commanded.get(cover)
        if commanded is None:
            # Never commanded this cover (e.g. just started): adopt as baseline.
            self.last_commanded[cover] = position
            return False
        if abs(position - commanded) <= self.motion.deadband:
            return False
        if (
            self.last_command_ts is not None
            and now - self.last_command_ts < self.motion.settle
            and previous is not None
            and abs(position - commanded)
            <= abs(previous - commanded) + self.motion.deadband
        ):
            # Our own move still settling / reporting intermediates.
            return False
        # A human moved this cover: adopt their position and stand down. The
        # hold starts even while control is off, so re-enabling inside the
        # hold window stays held instead of snapping back to the target.
        self.last_commanded[cover] = position
        self.start_hold(now)
        return True

    # -- evaluation ---------------------------------------------------------

    def evaluate(
        self,
        now: float,
        glare_position: int,
        current: dict[str, int | None],
        forced: bool = False,
    ) -> Decision:
        """Decide whether to command the zone's covers toward the target.

        ``current`` maps cover -> reported position (None when unavailable).
        ``forced`` bypasses rate limiting (mode changes, explicit services)
        but never bypasses an active hold or a disabled zone.
        """
        # Adopt baselines for covers we have never commanded. Without this,
        # the first manual move after startup is mistaken for the baseline in
        # report_position (no hold), and the next tick reverts the human's
        # move. Seeding here means that by the first tick every reachable
        # cover has a baseline, so a later differing report reads as manual.
        for cover, position in current.items():
            if position is not None:
                self.last_commanded.setdefault(cover, position)

        if not self.enabled:
            return Decision(REASON_DISABLED)

        if self.hold_active(now):
            return Decision(REASON_HOLD)

        target = self.target(glare_position)

        movers = [
            cover
            for cover in self.covers
            if current.get(cover) is not None
            and abs(current[cover] - target) > self.motion.deadband
        ]
        if not movers:
            return Decision(REASON_IN_SYNC, target)

        if (
            not forced
            and self.last_command_ts is not None
            and now - self.last_command_ts < self.motion.min_interval
        ):
            return Decision(REASON_RATE_LIMITED, target)

        for cover in movers:
            self.last_commanded[cover] = target
            # Where the cover starts from: the settle rule measures each
            # report's convergence against the one before it.
            self.last_reported[cover] = current[cover]
        self.last_command_ts = now
        return Decision(REASON_COMMAND, target, movers)
