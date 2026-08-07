"""UI-agnostic progress reporting.

The library never paints a terminal or pushes to a socket itself. Long-running
operations emit structured :class:`ProgressEvent`s through an optional
``progress`` callback that the caller threads in. The caller decides how to
render them:

    * CLI script  -> a callback that drives your own progress bar
    * web server  -> a callback that pushes each event onto a websocket / queue
    * nothing     -> pass ``None`` (the default); events are dropped, no TTY

A callback (rather than an async generator) keeps the synchronous pyVmomi code
synchronous and works for every front-end without forcing async on callers.

Concurrent operations
---------------------
``key`` identifies one operation. Before 0.4.0 it was derived from the step label
alone, so ``task:vmdk-copy`` was byte-identical for every VM in a batch and a
consumer could not tell N concurrent clones apart. Pass ``progress_context=`` to
a workflow (typically the VM name) and every event it emits carries that string
in :attr:`ProgressEvent.context` *and* has it prefixed onto ``key`` — so both a
consumer that groups by ``(context, key)`` and one that only keys on ``key``
stay correct.
"""

import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class Phase(str, Enum):
    """Lifecycle of a single tracked operation."""

    START = "start"
    UPDATE = "update"
    END = "end"
    ERROR = "error"


@dataclass(frozen=True)
class ProgressEvent:
    """One progress sample for a single operation.

    ``key`` is a stable id for the operation (e.g. ``"upload:web.vmx"`` or
    ``"vmdk-copy"``) so a consumer can keep one bar / one websocket channel per
    concurrent operation. ``completed``/``total`` are in ``unit`` (``"%"`` for
    vSphere tasks, ``"B"`` for byte transfers).

    ``context`` is the caller-supplied correlation string (usually the VM name)
    that distinguishes concurrent runs of the same step. ``None`` when the caller
    did not pass one.
    """

    key: str
    label: str
    completed: float
    total: float
    unit: str = "%"
    phase: Phase = Phase.UPDATE
    error: str | None = None
    context: str | None = None

    @property
    def fraction(self) -> float:
        return (self.completed / self.total) if self.total else 0.0

    @property
    def percent(self) -> float:
        return self.fraction * 100.0


#: A progress sink. Receives every :class:`ProgressEvent`. Exceptions it raises
#: are caught and logged rather than propagated — see :meth:`Reporter._emit`.
ProgressCallback = Callable[[ProgressEvent], None]


def scoped_key(key: str, context: str | None) -> str:
    """Prefix ``key`` with ``context`` so concurrent operations do not collide."""
    return f"{context}/{key}" if context else key


class Reporter:
    """Internal helper the library uses to emit events.

    Centralises the ``None``-callback check and absolute/relative bookkeeping so
    call sites stay terse. Callers of the public API never construct one — they
    just pass a :data:`ProgressCallback`.
    """

    def __init__(
        self,
        callback: ProgressCallback | None,
        key: str,
        label: str,
        total: float,
        unit: str = "%",
        context: str | None = None,
    ) -> None:
        self._cb = callback
        self.context = context
        self.key = scoped_key(key, context)
        self.label = label
        self.total = total
        self.unit = unit
        self.completed = 0.0

    def start(self) -> None:
        self._emit(Phase.START)

    def advance(self, by: float) -> None:
        self.completed += by
        self._emit(Phase.UPDATE)

    def to(self, completed: float) -> None:
        if completed > self.completed:
            self.completed = completed
            self._emit(Phase.UPDATE)

    def finish(self) -> None:
        self.completed = self.total
        self._emit(Phase.END)

    def fail(self, error: str) -> None:
        self._emit(Phase.ERROR, error)

    def _emit(self, phase: Phase, error: str | None = None) -> None:
        if self._cb is None:
            return
        event = ProgressEvent(
            key=self.key,
            label=self.label,
            completed=self.completed,
            total=self.total,
            unit=self.unit,
            phase=phase,
            error=error,
            context=self.context,
        )
        try:
            self._cb(event)
        except Exception:
            # A progress sink is a reporting concern; it must never take down the
            # operation it is reporting on. A consumer whose websocket publisher
            # blips mid-clone would otherwise abort the clone.
            log.warning(
                "progress callback raised for key=%s phase=%s; continuing",
                self.key,
                phase.value,
                exc_info=True,
            )


class StepReporter:
    """Overall progress across a workflow's steps ("step 3 of 6").

    Per-step events say how far along one upload is; they do not say how much of
    the whole clone is left. Consumers previously had to hardcode vmkit's internal
    step count and derive the total from their own request flags, so any change to
    the step sequence silently broke their progress bar.

    Emits under the key ``workflow:<name>`` with ``unit="steps"``.
    """

    def __init__(
        self,
        callback: ProgressCallback | None,
        name: str,
        total_steps: int,
        context: str | None = None,
    ) -> None:
        self.name = name
        self.total_steps = total_steps
        self._rep = Reporter(
            callback,
            key=f"workflow:{name}",
            label=name,
            total=total_steps,
            unit="steps",
            context=context,
        )

    def start(self) -> None:
        self._rep.start()

    def step(self, label: str) -> None:
        """Mark one step complete. ``label`` names the step just finished."""
        self._rep.label = f"{self.name}: {label}"
        self._rep.advance(1)

    def finish(self) -> None:
        self._rep.label = self.name
        self._rep.finish()

    def fail(self, error: str) -> None:
        self._rep.fail(error)


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def setup_logging(name: str, verbose: bool) -> str:
    """Configure console + timestamped file logging. Returns the log file path.

    For CLI use only. **Do not call this from a server or worker**: it adds
    handlers unconditionally (so repeated calls duplicate every line) and creates
    a ``logs/`` directory relative to the current working directory. A long-lived
    process should configure the ``vmkit`` logger itself:

        logging.getLogger("vmkit").setLevel(logging.INFO)
    """
    package_log = logging.getLogger("vmkit")
    package_log.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    ch.setFormatter(fmt)
    package_log.addHandler(ch)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = Path("logs") / f"deploy_{name}_{stamp}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    package_log.addHandler(fh)

    package_log.debug("Logging initialised. Log file: %s", logfile)
    return str(logfile)
