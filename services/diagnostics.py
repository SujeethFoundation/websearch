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
        self.searches = self.slow = self.loading_refused = self.queue_refused = self.errors = 0

    def bump(self, name, n=1):
        with self._lock:
            setattr(self, name, getattr(self, name) + n)

    def take(self):
        with self._lock:
            out = {"searches": self.searches, "slow": self.slow, "loading_503": self.loading_refused,
                   "queue_503": self.queue_refused, "errors": self.errors}
            self.reset()
            return out


counters = Counters()


def emit(event, **fields):
    """One line: `event key=value ...` with strings left bare and anything else as JSON."""
    parts = [f"{k}={v if isinstance(v, str) else json.dumps(v, default=str, separators=(',', ':'))}" for k, v in fields.items()]
    log.warning("%s %s", event, " ".join(parts))


def start(info, interval, snapshot):
    """Log process_start, register process_exit, and start the heartbeat thread (interval 0 turns the heartbeat off).
    info() -> dict of process facts; snapshot() -> dict with `running`, `waiting`, `index` (loaded|loading|failed)."""
    emit("process_start", **info())
    atexit.register(lambda: emit("process_exit", **info()))
    if interval <= 0:
        return None
    thread = threading.Thread(target=_beat_forever, args=(info, interval, snapshot), name="heartbeat", daemon=True)
    thread.start()
    return thread


def _beat_forever(info, interval, snapshot):
    last_beat = time.monotonic()
    while True:
        time.sleep(interval)
        try:
            if beat(info, snapshot, time.monotonic() - last_beat >= IDLE_BEAT_SECONDS):
                last_beat = time.monotonic()
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
