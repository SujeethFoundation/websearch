"""The few log lines that explain a restart or a stall, in place of a line per request.

process_start   once, when the worker starts: pid, instance, memory and CPU limits.
heartbeat       every HEARTBEAT_SECONDS while the instance is busy or loading (and every 5 minutes when idle):
                memory, CPU, throttling, searches running and waiting, refusals since the last beat, index state.
slow_search     one warning for a search or document read that took longer than SLOW_SEARCH_SECONDS.
process_exit    when the worker exits normally. A worker killed for memory writes nothing; that shows as a heartbeat
                that stops, followed by a new process_start with a different pid.
"""
import atexit
import json
import logging
import threading
import time

log = logging.getLogger("sujeeth-web-search.diag")

IDLE_BEAT_SECONDS = 300


class Counters:
    """What happened since the last heartbeat. Cheap to bump from request threads."""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        self.searches = self.slow = self.loading_refused = self.queue_refused = self.deadline_refused = self.errors = 0

    def bump(self, name, n=1):
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def take(self):
        with self._lock:
            out = {"searches": self.searches, "slow": self.slow, "loading_503": self.loading_refused,
                   "queue_503": self.queue_refused, "deadline_503": self.deadline_refused, "errors": self.errors}
            self.reset()
            return out


counters = Counters()


def emit(event, **fields):
    """One line: `event key=value ...` with strings left bare and anything else as JSON."""
    parts = [f"{k}={v if isinstance(v, str) else json.dumps(v, default=str, separators=(',', ':'))}" for k, v in fields.items()]
    log.warning("%s %s", event, " ".join(parts))


_state = {"announced": False, "last": 0.0}
_state_lock = threading.Lock()


def start(info, interval, snapshot):
    """Log process_start now and process_exit at exit. The Functions host forwards a worker's log lines only when they come
    from a request thread, so the heartbeat is not a thread of its own: tick() sends it from whichever request comes in."""
    emit("process_start", **info())
    atexit.register(lambda: emit("process_exit", **info()))
    _state.update(announced=True, last=time.monotonic(), interval=interval, info=info, snapshot=snapshot)


def tick():
    """Call at the start of every request. At most once per interval it logs a heartbeat (if anything happened since the
    last one), and the first call after a start also repeats process_start, so both reach the host's log."""
    interval = _state.get("interval") or 0
    if interval <= 0:
        return
    now = time.monotonic()
    with _state_lock:
        first = _state.pop("announced", False)
        if not first and now - _state["last"] < interval:
            return
        idle_due = now - _state.get("last_logged", 0.0) >= IDLE_BEAT_SECONDS
        _state["last"] = now
    try:
        if first:
            emit("process_start", **_state["info"]())
        if beat(_state["info"], _state["snapshot"], idle_due):
            _state["last_logged"] = now
    except Exception:
        log.debug("heartbeat failed", exc_info=True)


def beat(info, snapshot, idle_due=False):
    """Emit one heartbeat if anything happened since the last one, the index is not loaded, a search is running or
    waiting, or idle_due says it has been quiet long enough for a sign of life."""
    state = snapshot()
    since = counters.take()
    active = any(since.values()) or state.get("running") or state.get("waiting") or state.get("index") != "loaded"
    if not (active or idle_due):
        return False
    facts = info()
    emit("heartbeat", pid=facts.get("pid"), instance=facts.get("instance"), up_s=facts.get("uptime_seconds"),
         rss_mb=facts.get("rss_mb"), mem_limit_mb=facts.get("mem_limit_mb"), cpu_s=facts.get("cpu_seconds"),
         throttled_s=facts.get("cpu_throttled_seconds"), **state, **since)
    return True
