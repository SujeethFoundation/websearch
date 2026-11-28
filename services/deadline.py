"""A time limit for one search, checked between its stages.

A Python thread cannot be stopped from outside, so a search that has started runs to the end. Checking the clock between
stages (embedding, scoring, rescoring) lets it stop early, before the next stage's CPU, once the caller has waited too long
and has probably given up. The clock starts when the request arrives, so time spent waiting for a free slot counts."""
import contextvars
import time

_until = contextvars.ContextVar("search_deadline", default=None)


class SearchDeadline(RuntimeError):
    """The search ran past SEARCH_DEADLINE_SECONDS and was stopped before its next stage."""

    def __init__(self, stage, elapsed, limit):
        super().__init__(f"the search passed its {limit:g}s limit ({elapsed:.1f}s) before {stage}")
        self.stage, self.elapsed, self.limit = stage, elapsed, limit


def start(seconds, arrived=None):
    """Set this request's deadline: `seconds` after `arrived` (time.monotonic(), default now). 0 means no limit.
    Returns a token for clear()."""
    arrived = time.monotonic() if arrived is None else arrived
    return _until.set((arrived, arrived + seconds, seconds) if seconds and seconds > 0 else None)


def clear(token):
    _until.reset(token)


def check(stage):
    """Raise SearchDeadline when the current request is past its deadline. No deadline set: does nothing."""
    state = _until.get()
    if state is None:
        return
    arrived, until, limit = state
    now = time.monotonic()
    if now > until:
        raise SearchDeadline(stage, now - arrived, limit)
