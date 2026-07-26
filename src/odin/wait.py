"""Sleeping until a provider limit lifts.

Odin never waits by default — a process that silently idles for hours is a bad
default, and a stated reset time cannot always be parsed. But when the provider
*does* say when the window reopens and the user opts in, sleeping through it
turns an interrupted overnight queue into one that finishes by itself.

Posture matches the rest of the best-effort layers (`term.py`, `style.py`):
stdlib only, TTY-aware output, and no way for this to sink a run. Recovery
always happens *before* the sleep, so a laptop that dies mid-wait still leaves
the work committed and the queue runnable.

The clock and sleep function are injectable so tests never actually wait.
"""

from __future__ import annotations

import sys
import time as _time
from datetime import datetime, timedelta
from typing import Callable, TextIO

#: Slept past the stated reset by this much — providers are not to-the-second,
#: and waking up 10 seconds early just burns another attempt.
BUFFER_SECONDS = 120

#: Refuse to sleep longer than this unless told otherwise (minutes).
DEFAULT_MAX_WAIT_MINUTES = 360

#: How often the countdown redraws.
TICK_SECONDS = 30


def human_delta(seconds: float) -> str:
    """`2h13m` / `13m` / `45s` — for offering and counting down a wait."""
    secs = max(0, int(seconds))
    if secs >= 3600:
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"
    if secs >= 60:
        return f"{secs // 60}m"
    return f"{secs}s"


def seconds_until(target: datetime, *, now: datetime | None = None) -> float:
    """Seconds from now until `target` (plus the buffer); never negative."""
    now = now or datetime.now().astimezone()
    if target.tzinfo is None:
        target = target.replace(tzinfo=now.tzinfo)
    return max(0.0, (target - now).total_seconds() + BUFFER_SECONDS)


def exceeds_cap(target: datetime, max_minutes: int, *, now: datetime | None = None) -> bool:
    return seconds_until(target, now=now) > max_minutes * 60


def format_target(target: datetime) -> str:
    """`15:00 America/New_York (in 2h13m)` — what the user is offered."""
    zone = getattr(target.tzinfo, "key", None) or target.strftime("%Z") or "local"
    return f"{target.strftime('%H:%M')} {zone} (in {human_delta(seconds_until(target))})"


def sleep_until(
    target: datetime,
    *,
    out: TextIO | None = None,
    max_minutes: int = DEFAULT_MAX_WAIT_MINUTES,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    """Sleep until `target` (+ buffer), showing a countdown. True if it waited.

    Returns False when interrupted with Ctrl-C or when the wait would exceed
    `max_minutes` — in both cases the caller simply stops, with the task already
    recovered and sitting in `pending/`.
    """
    out = out if out is not None else sys.stdout
    now_fn = now_fn or (lambda: datetime.now().astimezone())
    sleep_fn = sleep_fn or _time.sleep

    remaining = seconds_until(target, now=now_fn())
    if remaining <= 0:
        return True
    if remaining > max_minutes * 60:
        _write(
            out,
            f"odin: reset is {human_delta(remaining)} away, beyond the "
            f"{max_minutes}-minute cap — not waiting.\n",
        )
        return False

    wake = now_fn() + timedelta(seconds=remaining)
    _write(out, f"odin: sleeping until {wake.strftime('%H:%M')} (Ctrl-C to stop)\n")
    try:
        while True:
            left = seconds_until(target, now=now_fn())
            if left <= 0:
                break
            _countdown(out, left)
            sleep_fn(min(TICK_SECONDS, left))
    except KeyboardInterrupt:
        _write(out, "\nodin: wait cancelled.\n")
        return False
    _write(out, "\nodin: reset window reached, continuing.\n")
    return True


def _countdown(out: TextIO, left: float) -> None:
    line = f"   waiting · {human_delta(left)} remaining"
    # Redraw in place on a terminal; on a pipe or log, stay quiet rather than
    # emitting thousands of lines.
    try:
        if out.isatty():
            _write(out, "\r" + line.ljust(46))
    except Exception:
        pass


def _write(out: TextIO, text: str) -> None:
    try:
        out.write(text)
        out.flush()
    except Exception:
        pass
