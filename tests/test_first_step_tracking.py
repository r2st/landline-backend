"""F2 — drop open-pixel / link tracking on the first touch.

The cold first email is where a tracking pixel and rewrapped links hurt
deliverability most. ``should_track_step`` encodes the gate: step 1 untracked by
default, steps 2+ tracked, ``track_first_step`` overrides, and the global
``sequence_tracking_enabled`` switch still wins.
"""

import pytest

from app.config import Settings
from app.services.email_tracking import apply_tracking, should_track_step


class _Cfg:
    def __init__(self, tracking_enabled=True, track_first=False):
        self.sequence_tracking_enabled = tracking_enabled
        self.track_first_step = track_first


def test_config_default_track_first_step_is_false():
    assert Settings.model_fields["track_first_step"].default is False


def test_first_step_is_untracked_by_default():
    assert should_track_step(1, _Cfg()) is False
    # step 0 / negative (defensive) also treated as first touch
    assert should_track_step(0, _Cfg()) is False


def test_later_steps_are_tracked():
    assert should_track_step(2, _Cfg()) is True
    assert should_track_step(4, _Cfg()) is True


def test_track_first_step_override_re_enables_step_one():
    assert should_track_step(1, _Cfg(track_first=True)) is True


def test_global_switch_off_disables_all_steps():
    off = _Cfg(tracking_enabled=False)
    assert should_track_step(1, off) is False
    assert should_track_step(3, off) is False
    # even with the first-step override, the global switch wins
    assert should_track_step(1, _Cfg(tracking_enabled=False, track_first=True)) is False


def test_apply_tracking_disabled_returns_body_unchanged():
    """An untracked first step keeps the exact human body — no pixel, no wrap."""
    body = '<p>Hi</p><p><a href="https://example.com/demo">demo</a></p>'
    out = apply_tracking(body, "enr-1", 1, enabled=should_track_step(1, _Cfg()))
    assert out == body
    assert "/track/open" not in out and "/track/click" not in out


def test_apply_tracking_enabled_on_later_step_injects_pixel_and_wraps():
    body = '<p>Hi</p><p><a href="https://example.com/demo">demo</a></p>'
    out = apply_tracking(body, "enr-1", 2, enabled=should_track_step(2, _Cfg()))
    assert out != body
    # A pixel was appended and the link was rewrapped through the redirect.
    assert "track/open" in out
    assert "track/click" in out
