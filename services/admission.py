"""Admission control for searches: at most N run at once on an instance, and one that has waited too long for a slot is
refused (the caller can try again, on another instance) instead of running late.

A search costs CPU on the instance that runs it. When more arrive than it can serve, they pile up, each one slower than the
last, and the caller has usually given up before its answer is ready. Refusing the ones that waited past the limit keeps the
rest fast and lets the caller retry."""
import threading
import time


class SearchBusy(RuntimeError):
    """No slot became free within the queue timeout."""

    def __init__(self, waited, limit):
        super().__init__(f"this instance is busy: waited {waited:.1f}s for a search slot (limit {limit:g}s)")
        self.waited, self.limit = waited, limit


class AdmissionGate:
    def __init__(self, slots, wait_seconds):
        """slots: searches allowed at once (0 = no limit, the gate lets everything through). wait_seconds: the longest a
        request waits for a slot (0 = refuse at once when none is free)."""
        self.slots = max(0, int(slots))
        self.wait_seconds = max(0.0, float(wait_seconds))
        self._semaphore = threading.BoundedSemaphore(self.slots) if self.slots else None
        self._lock = threading.Lock()
        self._running = self._waiting = self._peak_waiting = self._admitted = self._refused = 0
        self._wait_total = self._wait_max = 0.0

    def acquire(self):
        """Take a slot, waiting up to the queue timeout. Raises SearchBusy when none is free in time. Pair with release()."""
        if self._semaphore is None:
            return
        began = time.monotonic()
        with self._lock:
            self._waiting += 1
            self._peak_waiting = max(self._peak_waiting, self._waiting)
        got = self._semaphore.acquire(timeout=self.wait_seconds) if self.wait_seconds > 0 else self._semaphore.acquire(False)
        waited = time.monotonic() - began
        with self._lock:
            self._waiting -= 1
            self._wait_total += waited
            self._wait_max = max(self._wait_max, waited)
            if got:
                self._running += 1
                self._admitted += 1
            else:
                self._refused += 1
        if not got:
            raise SearchBusy(waited, self.wait_seconds)

    def release(self):
        if self._semaphore is None:
            return
        with self._lock:
            self._running -= 1
        self._semaphore.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False

    def stats(self):
        with self._lock:
            done = self._admitted + self._refused
            return {"slots": self.slots, "queue_timeout_seconds": self.wait_seconds, "running": self._running,
                    "waiting": self._waiting, "peak_waiting": self._peak_waiting, "admitted": self._admitted,
                    "refused": self._refused, "wait_avg_ms": round(1000 * self._wait_total / done) if done else 0,
                    "wait_max_ms": round(1000 * self._wait_max)}
