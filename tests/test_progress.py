"""Tests for progress events, the Reporter, and the callback guard."""

import logging

import pytest

from vmkit.progress import (
    Phase,
    ProgressEvent,
    Reporter,
    StepReporter,
    human_bytes,
    scoped_key,
)


def collector():
    events: list[ProgressEvent] = []
    return events, events.append


# --------------------------------------------------------------------------- #
# ProgressEvent                                                               #
# --------------------------------------------------------------------------- #
def test_fraction_and_percent():
    event = ProgressEvent(key="k", label="l", completed=25, total=100)
    assert event.fraction == 0.25
    assert event.percent == 25.0


def test_zero_total_does_not_divide_by_zero():
    """An empty upload is a real case — a zero-byte file has total=0."""
    event = ProgressEvent(key="k", label="l", completed=0, total=0)
    assert event.fraction == 0.0
    assert event.percent == 0.0


def test_event_is_immutable():
    """Events may be fanned out to several consumers; one must not be able to
    rewrite another's copy."""
    event = ProgressEvent(key="k", label="l", completed=1, total=2)
    with pytest.raises(Exception):
        event.completed = 5


def test_context_defaults_to_none():
    assert ProgressEvent(key="k", label="l", completed=0, total=1).context is None


def test_phase_is_a_string_enum():
    """Consumers serialise events to JSON, so the phase must survive as a string."""
    assert Phase.START == "start"
    assert Phase.END.value == "end"


# --------------------------------------------------------------------------- #
# Reporter                                                                    #
# --------------------------------------------------------------------------- #
def test_none_callback_is_a_no_op():
    rep = Reporter(None, key="k", label="l", total=100)
    rep.start()
    rep.to(50)
    rep.finish()  # must not raise


def test_lifecycle_phases():
    events, cb = collector()
    rep = Reporter(cb, key="k", label="l", total=100)
    rep.start()
    rep.to(50)
    rep.finish()
    assert [e.phase for e in events] == [Phase.START, Phase.UPDATE, Phase.END]
    assert events[-1].completed == 100


def test_to_ignores_backward_movement():
    """vSphere reports task progress that can jump around; a bar must not rewind."""
    events, cb = collector()
    rep = Reporter(cb, key="k", label="l", total=100)
    rep.to(50)
    rep.to(30)
    rep.to(60)
    assert [e.completed for e in events] == [50, 60]


def test_advance_accumulates():
    events, cb = collector()
    rep = Reporter(cb, key="k", label="l", total=100, unit="B")
    rep.advance(10)
    rep.advance(15)
    assert [e.completed for e in events] == [10, 25]


def test_fail_emits_error_phase_and_message():
    events, cb = collector()
    rep = Reporter(cb, key="k", label="l", total=100)
    rep.fail("disk full")
    assert events[0].phase == Phase.ERROR
    assert events[0].error == "disk full"


def test_unit_is_carried_through():
    events, cb = collector()
    Reporter(cb, key="k", label="l", total=100, unit="B").start()
    assert events[0].unit == "B"


# --------------------------------------------------------------------------- #
# Context / key scoping (V5)                                                  #
# --------------------------------------------------------------------------- #
def test_scoped_key_without_context_is_unchanged():
    assert scoped_key("task:vmdk-copy", None) == "task:vmdk-copy"


def test_scoped_key_prefixes_context():
    assert scoped_key("task:vmdk-copy", "web01") == "web01/task:vmdk-copy"


def test_context_reaches_every_event():
    events, cb = collector()
    rep = Reporter(cb, key="task:copy", label="copy", total=100, context="web01")
    rep.start()
    rep.to(10)
    rep.fail("x")
    assert all(e.context == "web01" for e in events)


def test_concurrent_operations_get_distinct_keys():
    """The point of the context field: before it, `task:vmdk-copy` was
    byte-identical for every VM in a batch and consumers could not demultiplex."""
    events, cb = collector()
    for vm in ("web01", "web02"):
        Reporter(cb, key="task:vmdk-copy", label="VMDK copy", total=100, context=vm).start()
    assert len({e.key for e in events}) == 2
    assert {e.context for e in events} == {"web01", "web02"}


# --------------------------------------------------------------------------- #
# Callback guard (V6)                                                         #
# --------------------------------------------------------------------------- #
def test_raising_callback_does_not_propagate():
    """A blip in a consumer's publisher used to abort the clone it was reporting on."""
    def explode(_event):
        raise RuntimeError("websocket closed")

    rep = Reporter(explode, key="k", label="l", total=100)
    rep.start()
    rep.to(50)
    rep.finish()
    rep.fail("x")  # none of these may raise


def test_raising_callback_is_logged(caplog):
    def explode(_event):
        raise RuntimeError("websocket closed")

    with caplog.at_level(logging.WARNING, logger="vmkit.progress"):
        Reporter(explode, key="k", label="l", total=100).start()

    assert any("progress callback raised" in r.message for r in caplog.records)


def test_reporter_keeps_working_after_a_callback_raises():
    """A transient failure must not silently disable progress for the rest of the run."""
    seen = []
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        seen.append(event)

    rep = Reporter(flaky, key="k", label="l", total=100)
    rep.start()
    rep.to(50)
    rep.finish()
    assert len(seen) == 2


def test_a_raising_callback_does_not_lose_the_underlying_error():
    """Reporter.fail is often called while handling a real exception; swallowing
    the callback's error must not swallow the caller's."""
    def explode(_event):
        raise RuntimeError("publisher down")

    rep = Reporter(explode, key="k", label="l", total=100)
    with pytest.raises(ValueError, match="real failure"):
        try:
            raise ValueError("real failure")
        except ValueError:
            rep.fail("real failure")
            raise


# --------------------------------------------------------------------------- #
# StepReporter (V16)                                                          #
# --------------------------------------------------------------------------- #
def test_step_reporter_counts_steps():
    events, cb = collector()
    steps = StepReporter(cb, "clone", 3)
    steps.start()
    steps.step("VMDK copy")
    steps.step("VMX upload")
    steps.step("register")
    steps.finish()

    assert events[0].phase == Phase.START
    assert events[0].total == 3
    assert events[0].unit == "steps"
    assert [e.completed for e in events[1:4]] == [1, 2, 3]
    assert events[-1].phase == Phase.END


def test_step_reporter_labels_name_the_step():
    events, cb = collector()
    steps = StepReporter(cb, "clone", 2)
    steps.start()
    steps.step("VMDK copy")
    assert events[1].label == "clone: VMDK copy"


def test_step_reporter_key_is_scoped_by_context():
    events, cb = collector()
    StepReporter(cb, "clone", 1, context="web01").start()
    assert events[0].key == "web01/workflow:clone"


def test_step_reporter_without_context():
    events, cb = collector()
    StepReporter(cb, "clone", 1).start()
    assert events[0].key == "workflow:clone"


def test_step_reporter_fail():
    events, cb = collector()
    steps = StepReporter(cb, "clone", 3)
    steps.start()
    steps.step("VMDK copy")
    steps.fail("register failed")
    assert events[-1].phase == Phase.ERROR
    assert events[-1].error == "register failed"
    # The partial count survives, so a UI can show where it stopped.
    assert events[-1].completed == 1


# --------------------------------------------------------------------------- #
# human_bytes                                                                 #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0.0 B"),
        (512, "512.0 B"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024**2, "1.0 MiB"),
        (1024**3, "1.0 GiB"),
        (1024**4, "1.0 TiB"),
        (1024**5, "1.0 PiB"),
        (1024**6, "1024.0 PiB"),
    ],
)
def test_human_bytes(value, expected):
    assert human_bytes(value) == expected


def test_human_bytes_negative():
    assert human_bytes(-1024) == "-1.0 KiB"
