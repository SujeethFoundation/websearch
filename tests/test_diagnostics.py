import logging

from services import diagnostics


def facts():
    return {"pid": 7, "instance": "i1", "uptime_seconds": 12, "rss_mb": 400.0, "mem_limit_mb": 784,
            "cpu_seconds": 3.2, "cpu_throttled_seconds": 1.1}


def test_quiet_when_idle_and_loaded(caplog):
    diagnostics.counters.take()
    with caplog.at_level(logging.WARNING):
        assert diagnostics.beat(facts, lambda: {"running": 0, "waiting": 0, "index": "loaded"}) is False
    assert caplog.records == []


def test_beats_while_loading_and_reports_refusals(caplog):
    diagnostics.counters.take()
    diagnostics.counters.bump("loading_refused", 3)
    with caplog.at_level(logging.WARNING):
        assert diagnostics.beat(facts, lambda: {"running": 0, "waiting": 0, "index": "loading"}) is True
    line = caplog.records[-1].getMessage()
    assert line.startswith("heartbeat ") and "pid=7" in line and "rss_mb=400.0" in line and "loading_503=3" in line
    assert "index=loading" in line
    assert diagnostics.counters.take()["loading_503"] == 0   # a beat resets the counts


def test_idle_sign_of_life(caplog):
    diagnostics.counters.take()
    with caplog.at_level(logging.WARNING):
        assert diagnostics.beat(facts, lambda: {"running": 0, "waiting": 0, "index": "loaded"}, idle_due=True) is True


def test_start_logs_process_start(caplog):
    with caplog.at_level(logging.WARNING):
        assert diagnostics.start(facts, 0, lambda: {}) is None
    assert any(r.getMessage().startswith("process_start pid=7") for r in caplog.records)
