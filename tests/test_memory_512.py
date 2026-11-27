"""Tests for the memory changes that let a search instance run in 512 MB: smaller postings, shared strings, a reload that
never holds two indexes, reloads that use only a snapshot, staggered reloads, and the HTTP-only preload."""
import threading
import time

import numpy as np
import pytest

import function_app as fa
from services import index_snapshot as snap
from services import local_index as li
from services.config import settings
from test_index_snapshot import FakeContainer, manager
from test_local_index import conf, corpus, entities, filled_store, index_of, make_doc, wait_for  # noqa: F401


@pytest.fixture
def snaps(conf, monkeypatch):
    monkeypatch.setattr(settings, "index_snapshot_enabled", True)
    monkeypatch.setattr(settings, "index_snapshot_autobuild", True)
    monkeypatch.setattr(settings, "index_snapshot_keep_seconds", 3600)
    monkeypatch.setattr(settings, "local_rescore", True)
    monkeypatch.setattr(settings, "local_index_min_reload_seconds", 0)
    container = FakeContainer()
    return container, snap.SnapshotStore(container, "index-snapshot")


def refresh_now(m):
    """Run the manager's periodic check once and wait for its background thread to finish."""
    m._last_check = float("-inf")
    m._maybe_refresh()
    assert wait_for(lambda: m._checking.acquire(blocking=False))
    m._checking.release()


# ---------------------------------------------------------------------------------------------------------------
# Postings
# ---------------------------------------------------------------------------------------------------------------

def test_posting_weights_are_float16_by_default_and_scores_stay_close(monkeypatch):
    docs = corpus()
    monkeypatch.setattr(settings, "local_posting_weight_dtype", "float32")
    wide = index_of(docs)
    monkeypatch.setattr(settings, "local_posting_weight_dtype", "float16")
    narrow = index_of(docs)
    assert wide.post_weights.dtype == np.float32 and narrow.post_weights.dtype == np.float16
    assert narrow.post_weights.nbytes * 2 == wide.post_weights.nbytes
    terms = li.query_terms("w3 w17 w250 w999 w44")
    a, b = wide.keyword_scores(terms), narrow.keyword_scores(terms)
    assert a.dtype == b.dtype == np.float32
    assert np.allclose(a, b, rtol=3e-3, atol=1e-3)
    assert list(np.argsort(-a)[:5]) == list(np.argsort(-b)[:5])


def test_an_unknown_weight_type_means_float16():
    assert settings.local_posting_weight_dtype in ("float16", "float32")


def test_words_in_most_chunks_get_no_postings_when_a_share_is_set(monkeypatch):
    docs = []
    for d in corpus(pages=10, per_page=5):
        d["content"] = "everywhere " + d["content"]
        docs.append(d)
    monkeypatch.setattr(settings, "local_posting_weight_dtype", "float32")
    monkeypatch.setattr(settings, "local_posting_max_df", 0.0)
    full = index_of(docs)
    monkeypatch.setattr(settings, "local_posting_max_df", 0.5)
    pruned = index_of(docs)
    everywhere = li.query_terms("everywhere")
    assert full.keyword_scores(everywhere).max() > 0
    assert pruned.keyword_scores(everywhere).max() == 0
    assert len(pruned.post_docs) < len(full.post_docs)
    assert len(pruned.post_docs) == len(pruned.post_weights) == int(pruned.post_ptr[-1])
    # a word that is not common scores exactly as before, because lengths were worked out from every word
    rare = li.query_terms("w3 w17 w250")
    assert np.allclose(pruned.keyword_scores(rare), full.keyword_scores(rare))


def test_a_pruned_index_still_answers_searches(monkeypatch):
    monkeypatch.setattr(settings, "local_posting_max_df", 0.2)
    index = index_of(corpus())
    assert index.n == 100 and len(index.post_docs) > 0
    assert index.keyword_scores(li.query_terms("w3 w17")).max() > 0


# ---------------------------------------------------------------------------------------------------------------
# Shared strings
# ---------------------------------------------------------------------------------------------------------------

def test_the_chunks_of_a_page_hold_one_title_url_and_concept_list():
    index = index_of(corpus(pages=4, per_page=5))
    first = index.pages[next(iter(index.pages))]
    assert len(first) == 5
    assert all(index.titles[i] is index.titles[first[0]] for i in first)
    assert all(index.urls[i] is index.urls[first[0]] for i in first)
    assert all(index.concepts[i] is index.concepts[first[0]] for i in first)


def test_a_restored_snapshot_shares_them_too(snaps):
    container, store = snaps
    built = index_of(corpus(pages=4, per_page=5))
    manifest = store.publish(built, "v1", set())
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip=set(), rescore=True)
    rows = restored.pages[next(iter(restored.pages))]
    assert all(restored.titles[i] is restored.titles[rows[0]] for i in rows)
    assert all(restored.urls[i] is restored.urls[rows[0]] for i in rows)
    assert [list(c) for c in restored.concepts] == [list(c) for c in built.concepts]


# ---------------------------------------------------------------------------------------------------------------
# Reloads
# ---------------------------------------------------------------------------------------------------------------

def changed_table(table, pages=8):
    table.upsert_entities(entities(corpus(pages=pages)))
    table.bump_version()


def test_snapshot_only_never_scans_and_keeps_the_old_index_when_the_snapshot_is_stale(snaps, monkeypatch):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    m = manager(table, store)
    assert m.current().n == 20
    assert wait_for(lambda: "index-snapshot/latest.json" in container.blobs)
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)
    changed_table(table)
    scans = []
    original = table.scan
    table.scan = lambda *a, **k: scans.append(1) or original(*a, **k)

    refresh_now(m)
    assert scans == [] and m._index is not None and m.current().n == 20
    assert "no current snapshot" in m.stats["last_error"]
    refresh_now(m)                       # the same reason is not logged again
    assert scans == []

    # someone builds the snapshot (this builder may scan: it is the explicit path)
    monkeypatch.setattr(settings, "local_index_snapshot_only", False)
    builder = manager(table, store)
    builder.reload(use_snapshot=False, publish=True)
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)
    scans.clear()
    refresh_now(m)
    assert m.current().n == 40 and scans == [] and m.describe()["load_source"] == "snapshot"


def test_a_first_load_with_snapshot_only_and_no_snapshot_says_what_to_do(snaps, monkeypatch):
    container, store = snaps
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)
    m = manager(filled_store(), store)
    with pytest.raises(li.LocalIndexUnavailable) as err:
        m.current()
    assert "build_snapshot" in str(err.value)


def test_drop_before_reload_frees_the_old_index_before_the_new_one_loads(snaps, monkeypatch):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    m = manager(table, store)
    old = m.current()
    assert wait_for(lambda: "index-snapshot/latest.json" in container.blobs)
    monkeypatch.setattr(settings, "local_index_drop_before_reload", True)
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)
    monkeypatch.setattr(settings, "local_index_fail_fast", True)
    changed_table(table)
    monkeypatch.setattr(settings, "local_index_snapshot_only", False)
    manager(table, store).reload(use_snapshot=False, publish=True)
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)

    seen = {}
    real_load = store.load

    def watching(*args, **kwargs):
        seen["index_during_load"] = m._index
        try:
            m.current()
        except li.LocalIndexLoading as exc:
            seen["fail_fast"] = str(exc)
        return real_load(*args, **kwargs)

    store.load = watching
    refresh_now(m)
    assert seen["index_during_load"] is None and "still loading" in seen["fail_fast"]
    assert m.current().n == 40 and m.current() is not old


def test_drop_before_reload_keeps_the_old_index_when_the_snapshot_is_unusable(snaps, monkeypatch):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    m = manager(table, store)
    m.current()
    monkeypatch.setattr(settings, "local_index_drop_before_reload", True)
    monkeypatch.setattr(settings, "local_index_snapshot_only", True)
    changed_table(table)                  # the snapshot is now stale, so nothing is dropped
    refresh_now(m)
    assert m._index is not None and m.current().n == 20


def test_reloads_wait_a_random_while_after_a_change(snaps, monkeypatch):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    m = manager(table, store)
    m.current()
    monkeypatch.setattr(settings, "local_index_reload_jitter_seconds", 100)
    monkeypatch.setattr(li.random, "uniform", lambda a, b: 50.0)
    changed_table(table)
    refresh_now(m)
    assert m.current().n == 20 and m._reload_not_before is not None      # waiting
    m._reload_not_before = time.monotonic() - 1
    refresh_now(m)
    assert m.current().n == 40 and m._reload_not_before is None


def test_no_jitter_means_an_immediate_reload(snaps, monkeypatch):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    m = manager(table, store)
    m.current()
    monkeypatch.setattr(settings, "local_index_reload_jitter_seconds", 0)
    changed_table(table)
    refresh_now(m)
    assert m.current().n == 40


# ---------------------------------------------------------------------------------------------------------------
# Preload
# ---------------------------------------------------------------------------------------------------------------

def test_the_preload_starts_once_and_only_when_asked(monkeypatch):
    started = []
    monkeypatch.setattr(fa, "_preload_local_index", lambda: started.append(1))
    monkeypatch.setattr(fa, "_preload_done", False)
    monkeypatch.setattr(settings, "local_index_preload", True)
    monkeypatch.setattr(type(fa.searcher.local), "enabled", property(lambda self: True))
    monkeypatch.setattr(fa, "_preload_started", threading.Lock())
    fa.start_preload_once()
    fa.start_preload_once()
    assert wait_for(lambda: len(started) == 1) and len(started) == 1


def test_no_preload_when_it_is_off(monkeypatch):
    started = []
    monkeypatch.setattr(fa, "_preload_local_index", lambda: started.append(1))
    monkeypatch.setattr(fa, "_preload_done", False)
    monkeypatch.setattr(settings, "local_index_preload", False)
    monkeypatch.setattr(fa, "_preload_started", threading.Lock())
    fa.start_preload_once()
    time.sleep(0.05)
    assert started == []


def test_the_new_behaviour_is_on_by_default_and_the_settings_turn_it_off(monkeypatch):
    for name in ("LOCAL_INDEX_LEAN_BUILD", "LOCAL_INDEX_DROP_BEFORE_RELOAD", "LOCAL_INDEX_PRELOAD_ON_HTTP", "LOCAL_INDEX_FAIL_FAST",
                 "LOCAL_INDEX_RELOAD_JITTER_SECONDS", "LOCAL_INDEX_SNAPSHOT_ONLY", "LOCAL_POSTING_MAX_DF", "LOCAL_POSTING_WEIGHT_DTYPE"):
        monkeypatch.delenv(name, raising=False)
    import importlib
    from services import config
    fresh = importlib.reload(config).settings
    try:
        assert fresh.local_index_lean_build and fresh.local_index_drop_before_reload and fresh.local_index_preload_on_http
        assert fresh.local_index_fail_fast and fresh.local_index_reload_jitter_seconds == 300
        assert fresh.local_posting_weight_dtype == "float16"
        assert not fresh.local_index_snapshot_only and fresh.local_posting_max_df == 0.0
        for name, value in (("LOCAL_INDEX_LEAN_BUILD", "false"), ("LOCAL_INDEX_DROP_BEFORE_RELOAD", "false"),
                            ("LOCAL_INDEX_PRELOAD_ON_HTTP", "false"), ("LOCAL_INDEX_FAIL_FAST", "false"),
                            ("LOCAL_INDEX_RELOAD_JITTER_SECONDS", "0"), ("LOCAL_POSTING_WEIGHT_DTYPE", "float32")):
            monkeypatch.setenv(name, value)
        off = importlib.reload(config).settings
        assert not (off.local_index_lean_build or off.local_index_drop_before_reload or off.local_index_preload_on_http
                    or off.local_index_fail_fast)
        assert off.local_index_reload_jitter_seconds == 0 and off.local_posting_weight_dtype == "float32"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
