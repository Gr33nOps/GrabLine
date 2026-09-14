"""Single-flight guards for one-shot UI actions (double-click protection)."""

from __future__ import annotations

from app.ui import guard


def test_begin_end_blocks_a_second_start():
    registry: set[str] = set()
    assert guard.begin(registry, "ffmpeg") is True  # first click starts it
    assert guard.begin(registry, "ffmpeg") is False  # second click is a no-op
    assert guard.begin(registry, "update") is True  # a different action is free
    guard.end(registry, "ffmpeg")
    assert guard.begin(registry, "ffmpeg") is True  # released - can start again


def test_single_flight_context_releases_even_on_exception():
    registry: set[str] = set()
    ran = 0
    with guard.single_flight(registry, "setup") as go:
        assert go is True
        ran += 1
        # A re-entrant call while held is denied.
        with guard.single_flight(registry, "setup") as inner:
            assert inner is False
    assert "setup" not in registry  # released on normal exit

    try:
        with guard.single_flight(registry, "setup") as go:
            assert go is True
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert "setup" not in registry  # released despite the exception
    assert ran == 1


def test_end_is_safe_when_not_held():
    registry: set[str] = set()
    guard.end(registry, "never-started")  # must not raise
    assert registry == set()
