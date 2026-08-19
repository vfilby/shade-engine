"""Tests for the pure actuator decision logic."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "custom_components" / "shade_engine"))

from core import (  # noqa: E402
    REASON_COMMAND,
    REASON_DISABLED,
    REASON_HOLD,
    REASON_IN_SYNC,
    REASON_RATE_LIMITED,
    Decision,
    ModeTarget,
    MotionConfig,
    ZoneCore,
)

MODES = {
    "night": ModeTarget(fixed=37),
    "open": ModeTarget(fixed=100),
    "track": ModeTarget(dynamic=True),
    "shield": ModeTarget(dynamic=True, max=50),
}


def make_zone(mode="open", **motion):
    return ZoneCore(
        covers=["cover.a", "cover.b"],
        modes=MODES,
        motion=MotionConfig(**motion),
        mode=mode,
    )


def test_mode_targets_resolve():
    zone = make_zone("night")
    assert zone.target(glare_position=40) == 37
    zone.set_mode("open")
    assert zone.target(40) == 100
    zone.set_mode("track")
    assert zone.target(40) == 40
    zone.set_mode("shield")
    assert zone.target(40) == 40
    assert zone.target(90) == 50  # clamped


def test_command_when_out_of_sync():
    zone = make_zone("open")
    d = zone.evaluate(now=1000, glare_position=100,
                      current={"cover.a": 37, "cover.b": 100})
    assert d.reason == REASON_COMMAND
    assert d.target == 100
    assert d.covers == ["cover.a"]  # b already within deadband
    assert zone.last_commanded["cover.a"] == 100


def test_in_sync_never_commands():
    zone = make_zone("open")
    d = zone.evaluate(1000, 100, {"cover.a": 99, "cover.b": 100})
    assert d.reason == REASON_IN_SYNC


def test_rate_limit_defers_then_converges():
    zone = make_zone("track", min_interval=300)
    d1 = zone.evaluate(1000, 50, {"cover.a": 100, "cover.b": 100})
    assert d1.reason == REASON_COMMAND

    # Sun moved: new target differs, but only 60s elapsed.
    d2 = zone.evaluate(1060, 45, {"cover.a": 50, "cover.b": 50})
    assert d2.reason == REASON_RATE_LIMITED

    # Next tick after the interval: deferred move goes out (not dropped).
    d3 = zone.evaluate(1301, 45, {"cover.a": 50, "cover.b": 50})
    assert d3.reason == REASON_COMMAND
    assert d3.target == 45


def test_forced_bypasses_rate_limit_only():
    zone = make_zone("track", min_interval=300)
    zone.evaluate(1000, 50, {"cover.a": 100, "cover.b": 100})
    d = zone.evaluate(1010, 40, {"cover.a": 50, "cover.b": 50}, forced=True)
    assert d.reason == REASON_COMMAND

    zone.start_hold(1020)
    d = zone.evaluate(1030, 30, {"cover.a": 40, "cover.b": 40}, forced=True)
    assert d.reason == REASON_HOLD  # forced never beats a human


def test_manual_move_starts_hold_and_adopts_position():
    zone = make_zone("open", hold_duration=3600, settle=90)
    zone.evaluate(1000, 100, {"cover.a": 37, "cover.b": 37})

    # Our own command settling: not manual.
    assert not zone.report_position("cover.a", 80, now=1050)

    # Well after settle, position far from commanded: manual.
    assert zone.report_position("cover.a", 20, now=2000)
    assert zone.hold_active(2001)
    assert zone.last_commanded["cover.a"] == 20

    # Hold expires; evaluation resumes and reconciles.
    assert not zone.hold_active(2000 + 3601)
    d = zone.evaluate(2000 + 3601, 100, {"cover.a": 20, "cover.b": 100})
    assert d.reason == REASON_COMMAND
    assert d.covers == ["cover.a"]


def test_report_matching_command_is_not_manual():
    zone = make_zone("open")
    zone.evaluate(1000, 100, {"cover.a": 37, "cover.b": 37})
    assert not zone.report_position("cover.a", 100, now=5000)
    assert not zone.hold_active(5001)


def test_first_report_adopts_baseline():
    zone = make_zone("open")
    assert not zone.report_position("cover.a", 25, now=100)
    assert zone.last_commanded["cover.a"] == 25


def test_evaluate_seeds_baseline_so_first_manual_move_holds():
    # Regression: without seeding, the first manual move after startup was
    # adopted as the baseline (no hold) and the next tick reverted it.
    zone = make_zone("open", hold_duration=3600, settle=90)
    d = zone.evaluate(1000, 100, {"cover.a": 100, "cover.b": 100})
    assert d.reason == REASON_IN_SYNC
    assert zone.last_commanded == {"cover.a": 100, "cover.b": 100}

    # Hours later a human closes a cover: manual, held, not reverted.
    assert zone.report_position("cover.a", 25, now=20000)
    assert zone.hold_active(20001)
    d = zone.evaluate(20060, 100, {"cover.a": 25, "cover.b": 100})
    assert d.reason == REASON_HOLD


def test_evaluate_does_not_seed_unavailable_covers():
    zone = make_zone("open")
    zone.evaluate(1000, 100, {"cover.a": None, "cover.b": 100})
    assert "cover.a" not in zone.last_commanded


def test_release_hold():
    zone = make_zone("open")
    zone.start_hold(1000)
    assert zone.hold_active(1001)
    zone.release_hold()
    assert not zone.hold_active(1002)


def test_unavailable_cover_is_skipped():
    zone = make_zone("open")
    d = zone.evaluate(1000, 100, {"cover.a": None, "cover.b": 37})
    assert d.reason == REASON_COMMAND
    assert d.covers == ["cover.b"]


def test_disabled_never_commands_even_forced():
    zone = make_zone("open")
    zone.enabled = False
    d = zone.evaluate(1000, 100, {"cover.a": 37, "cover.b": 37}, forced=True)
    assert d.reason == REASON_DISABLED
    assert d.covers == []
    assert zone.last_command_ts is None


def test_disabled_takes_precedence_over_hold():
    zone = make_zone("open")
    zone.start_hold(1000)
    zone.enabled = False
    d = zone.evaluate(1001, 100, {"cover.a": 37, "cover.b": 37})
    assert d.reason == REASON_DISABLED


def test_reenable_converges():
    zone = make_zone("open")
    zone.enabled = False
    zone.evaluate(1000, 100, {"cover.a": 37, "cover.b": 37})
    zone.enabled = True
    d = zone.evaluate(1002, 100, {"cover.a": 37, "cover.b": 37}, forced=True)
    assert d.reason == REASON_COMMAND
    assert d.covers == ["cover.a", "cover.b"]


def test_manual_move_while_disabled_adopts_without_hold():
    zone = make_zone("open")
    d = zone.evaluate(1000, 100, {"cover.a": 100, "cover.b": 100})
    assert d.reason == REASON_IN_SYNC
    zone.enabled = False
    # Human moves a cover while control is off: adopt, but no hold.
    assert not zone.report_position("cover.a", 25, now=20000)
    assert not zone.hold_active(20001)
    assert zone.last_commanded["cover.a"] == 25
    # Re-enabling still converges to the mode target (no hold in the way).
    zone.enabled = True
    d = zone.evaluate(20060, 100, {"cover.a": 25, "cover.b": 100}, forced=True)
    assert d.reason == REASON_COMMAND
    assert d.covers == ["cover.a"]


def test_disabled_still_seeds_baselines():
    zone = make_zone("open")
    zone.enabled = False
    zone.evaluate(1000, 100, {"cover.a": 42, "cover.b": None})
    assert zone.last_commanded == {"cover.a": 42}
