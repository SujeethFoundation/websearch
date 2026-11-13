"""Tests for the chunk text kept as raw arrays in a snapshot (LazyTexts), the ordered download, and the fail-fast load."""
import threading
import types

import numpy as np
import pytest

from services import index_snapshot as snap
from services import local_index as li
from services.config import settings
from test_index_snapshot import FakeContainer, build, snaps, same_index, text_term  # noqa: F401  (snaps is a fixture)
from test_local_index import conf, corpus, entities, filled_store  # noqa: F401


def test_pack_texts_round_trips_unicode_empty_and_none():
    texts = ["plain", "", None, "caf\u00e9 \u2014 \u65e5\u672c\u8a9e", "emoji \U0001F600", None, "last"]
    lazy = snap.LazyTexts(*snap.pack_texts(texts))
    assert len(lazy) == len(texts)
    assert list(lazy) == texts
    assert lazy[-1] == "last" and lazy[2] is None and lazy[1] == ""


def test_pack_texts_of_nothing():
    lazy = snap.LazyTexts(*snap.pack_texts([]))
    assert len(lazy) == 0 and list(lazy) == []


def test_an_unencodable_character_is_replaced_not_fatal():
    lazy = snap.LazyTexts(*snap.pack_texts(["bad \ud800 surrogate"]))
    assert "bad" in lazy[0]


def test_lazy_texts_can_hide_rows_but_not_change_them():
    lazy = snap.LazyTexts(*snap.pack_texts(["a", "b", "c"]))
    lazy[1] = None
    lazy.hide_rows([2])
    assert list(lazy) == ["a", None, None]
    with pytest.raises(TypeError):
        lazy[0] = "x"
    with pytest.raises(IndexError):
        lazy[3]


def test_lazy_texts_refuses_offsets_that_do_not_fit():
    blob, off, none = snap.pack_texts(["abc", "de"])
    with pytest.raises(snap.SnapshotError):
        snap.LazyTexts(blob[:-1], off, none)
    with pytest.raises(snap.SnapshotError):
        snap.LazyTexts(blob, off[:-1], none)


def test_nbytes_counts_without_decoding():
    texts = ["x" * 1000] * 50
    lazy = snap.LazyTexts(*snap.pack_texts(texts))
    assert lazy.nbytes() >= 50_000


def test_a_restored_index_holds_lazy_text_and_meta_no_longer_has_it(snaps):
    container, store = snaps
    original = build()
    manifest = store.publish(original, "v1", {"themuse.com"})
    assert {"text_blob", "text_off", "text_none"} <= set(manifest["arrays"])
    import gzip, json
    meta = json.loads(gzip.decompress(container.blobs[f"index-snapshot/{manifest['build']}/meta.json.gz"][0]))
    assert "texts" not in meta
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    assert isinstance(restored.texts, snap.LazyTexts)
    same_index(original, restored)
    assert restored.memory_bytes() > 0


def test_extra_text_skip_domains_are_hidden_on_restore(snaps):
    container, store = snaps
    original = build(skip=())
    manifest = store.publish(original, "v1", set())
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    muse_rows = [i for i, c in enumerate(restored.domain_codes) if restored.domain_names[c] == "themuse.com"]
    assert muse_rows and all(restored.texts[i] is None for i in muse_rows)
    others = [i for i in range(restored.n) if i not in set(muse_rows)]
    assert all(restored.texts[i] for i in others[:20])


def test_text_blobs_are_not_downloaded_when_text_is_off(snaps):
    container, store = snaps
    manifest = store.publish(build(), "v1", {"themuse.com"})
    container.downloads.clear()
    restored = store.load(manifest, li.LocalIndex, keep_text=False, text_skip=set(), rescore=True)
    assert restored.texts is None
    assert not [d for d in container.downloads if "text_" in d]


def test_the_biggest_blobs_are_requested_first(snaps):
    container, store = snaps
    manifest = store.publish(build(), "v1", {"themuse.com"})
    container.downloads.clear()
    store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    base = f"index-snapshot/{manifest['build']}/"
    sizes = [manifest["arrays"][d[len(base):-4]]["bytes"] for d in container.downloads if d.endswith(".bin")]
    # pool.map submits in order; with a fake that answers instantly the log order is the submit order
    assert sizes == sorted(sizes, reverse=True)


def test_a_version_1_snapshot_is_not_used(snaps):
    container, store = snaps
    manifest = dict(store.publish(build(), "v1", {"themuse.com"}))
    manifest["format"] = 1
    ok, why = snap.compatible(manifest, version="v1", keep_text=True, text_skip={"themuse.com"}, rescore=True)
    assert not ok and "format" in why


def test_a_snapshot_missing_its_text_arrays_is_refused(snaps):
    container, store = snaps
    manifest = store.publish(build(), "v1", {"themuse.com"})
    broken = dict(manifest)
    broken["arrays"] = {k: v for k, v in manifest["arrays"].items() if k != "text_off"}
    with pytest.raises(snap.SnapshotError):
        store.load(broken, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)


# ---------------------------------------------------------------------------------------------------------------
# Fail fast while the first load runs
# ---------------------------------------------------------------------------------------------------------------

def _manager_that_is_loading(monkeypatch, fail_fast):
    monkeypatch.setattr(settings, "local_index_fail_fast", fail_fast)
    manager = li.LocalIndexManager(store=object(), snapshot_store=None, text_skip_provider=lambda: set())
    gate, started = threading.Event(), threading.Event()

    def slow_reload(*args, **kwargs):
        started.set()
        gate.wait(5)
        raise RuntimeError("stop here")        # the load is not the point of the test

    manager._reload_locked = slow_reload
    t = threading.Thread(target=lambda: _swallow(manager.current), daemon=True)
    t.start()
    assert started.wait(5)
    return manager, gate, t


def _swallow(fn):
    try:
        fn()
    except Exception:
        pass


def test_a_search_during_the_first_load_fails_at_once_when_fail_fast_is_on(monkeypatch):
    manager, gate, t = _manager_that_is_loading(monkeypatch, True)
    try:
        with pytest.raises(li.LocalIndexLoading):
            manager.current()
    finally:
        gate.set()
        t.join(5)


def test_a_search_during_the_first_load_waits_when_fail_fast_is_off(monkeypatch):
    manager, gate, t = _manager_that_is_loading(monkeypatch, False)
    result = {}

    def ask():
        try:
            manager.current()
        except Exception as exc:
            result["exc"] = exc

    waiter = threading.Thread(target=ask, daemon=True)
    waiter.start()
    waiter.join(0.3)
    assert waiter.is_alive()                   # still waiting for the load, as before
    gate.set()
    waiter.join(5)
    t.join(5)
    assert not isinstance(result.get("exc"), li.LocalIndexLoading)


def test_loading_is_not_swallowed_as_unavailable():
    assert not issubclass(li.LocalIndexLoading, li.LocalIndexUnavailable)
