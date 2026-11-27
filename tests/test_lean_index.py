"""Tests for the two-pass index build (services/lean_index.py): it must be the same index as LocalIndex(rows), build
from a table that changes underneath it, and take much less memory."""
import gc
import tracemalloc

import numpy as np
import pytest

from services import lean_index as lean
from services import local_index as li
from services.config import settings
from test_index_snapshot import ask, same_index
from test_local_index import DIMS, conf, corpus, entities, filled_store, make_doc  # noqa: F401


def normal(store, keep_text=True, skip=()):
    return li.LocalIndex((li.row_from_entity(e) for e in store.scan(keep_text, skip)), keep_text=keep_text,
                         text_skip_domains=skip)


@pytest.mark.parametrize("keep_text,skip", [(True, ()), (True, ("themuse.com",)), (False, ()), (False, ("themuse.com",))])
def test_the_lean_build_is_the_same_index(conf, monkeypatch, keep_text, skip):
    monkeypatch.setattr(settings, "local_rescore", True)
    store = filled_store()
    a, b = normal(store, keep_text, skip), lean.build_lean(store, keep_text, skip)
    same_index(a, b)
    assert ask(a) == ask(b)


@pytest.mark.parametrize("dtype,max_df", [("float32", 0.0), ("float16", 0.0), ("float16", 0.3)])
def test_the_same_postings_with_every_posting_setting(conf, monkeypatch, dtype, max_df):
    monkeypatch.setattr(settings, "local_posting_weight_dtype", dtype)
    monkeypatch.setattr(settings, "local_posting_max_df", max_df)
    docs = [dict(d, content="everywhere " + d["content"]) for d in corpus()]
    store = filled_store(docs)
    monkeypatch.setattr(lean, "BLOCK_ROWS", 7)          # many blocks, so the block merging is exercised
    a, b = normal(store, True, ("themuse.com",)), lean.build_lean(store, True, ("themuse.com",))
    same_index(a, b)
    assert ask(a, text="everywhere w5 w60") == ask(b, text="everywhere w5 w60")


def test_without_rescoring_there_are_no_8bit_vectors(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore", False)
    store = filled_store()
    index = lean.build_lean(store, True, ())
    assert index.vec8 is None and index.norm8 is None
    same_index(normal(store, True, ()), index)


def test_a_row_added_between_the_passes_is_left_for_the_next_reload(conf):
    store = filled_store(corpus(pages=6))
    original = store.scan_columns
    state = {"added": False}

    def scan_columns(select, with_text, text_skip=(), split=False):
        if "title" in select and not state["added"]:          # the second pass
            state["added"] = True
            store.upsert_entities(entities([make_doc(99, 0, title="New page", domain="sujeeth.org")]))
        yield from original(select, with_text, text_skip, split)

    store.scan_columns = scan_columns
    index = lean.build_lean(store, True, ())
    assert index.n == 30 and "page99-0" not in index.by_id


def test_a_row_deleted_between_the_passes_restarts_the_build(conf):
    store = filled_store(corpus(pages=6))
    original = store.scan_columns
    state = {"deleted": False}

    def scan_columns(select, with_text, text_skip=(), split=False):
        if "title" in select and not state["deleted"]:
            state["deleted"] = True
            del store.chunks.rows[sorted(store.chunks.rows)[3]]
        yield from original(select, with_text, text_skip, split)

    store.scan_columns = scan_columns
    index = lean.build_lean(store, True, ())
    assert index.n == 29


def test_a_table_that_never_settles_gives_up_with_a_message(conf):
    store = filled_store(corpus(pages=6))
    original = store.scan_columns

    def scan_columns(select, with_text, text_skip=(), split=False):
        if "title" in select and store.chunks.rows:
            del store.chunks.rows[sorted(store.chunks.rows)[0]]
        yield from original(select, with_text, text_skip, split)

    store.scan_columns = scan_columns
    with pytest.raises(li.LocalIndexUnavailable) as err:
        lean.build_lean(store, True, (), attempts=2)
    assert "kept changing" in str(err.value)


def test_an_empty_table_is_unavailable(conf):
    with pytest.raises(li.LocalIndexUnavailable):
        lean.build_lean(li.LocalIndexStore(chunks=filled_store().chunks.__class__(), meta=filled_store().meta), True, ())


def peak(build):
    gc.collect()
    tracemalloc.start()
    built = build()
    current, high = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return high - current, built          # the memory the build needed beyond the index it left behind


def test_the_lean_build_needs_far_less_memory_than_the_normal_build(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore", True)
    docs = [dict(make_doc(p, n, title=f"Page {p}", domain="sujeeth.org" if p % 5 else "themuse.com",
                          content=" ".join(f"w{(p * 31 + n * 7 + k * 13) % 3000}" for k in range(250)),
                          vector=np.random.default_rng(p * 10 + n).standard_normal(DIMS)))
            for p in range(400) for n in range(5)]
    store = filled_store(docs)
    monkeypatch.setattr(lean, "BLOCK_ROWS", 200)         # the production block (2,048 rows) is a small share of a real index
    normal_extra, a = peak(lambda: normal(store, True, ()))
    lean_extra, b = peak(lambda: lean.build_lean(store, True, ()))
    assert a.n == b.n == 2000
    print(f"memory beyond the finished index: normal {normal_extra / 1e6:.1f} MB, lean {lean_extra / 1e6:.1f} MB")
    assert lean_extra < normal_extra * 0.5


# ---------------------------------------------------------------------------------------------------------------
# In the manager
# ---------------------------------------------------------------------------------------------------------------

def test_the_manager_builds_lean_when_asked_and_drops_the_old_index_first(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_lean_build", True)
    monkeypatch.setattr(settings, "index_snapshot_enabled", False)
    store = filled_store(corpus(pages=4))
    m = li.LocalIndexManager(store=store, text_skip_provider=lambda: set())
    assert m.current().n == 20 and m.describe()["load_source"] == "table"
    store.upsert_entities(entities(corpus(pages=8)))
    store.bump_version()
    seen = {}
    original = store.scan_columns

    def watching(*args, **kwargs):
        seen.setdefault("index_during_build", m._index)
        return original(*args, **kwargs)

    store.scan_columns = watching
    monkeypatch.setattr(settings, "local_index_drop_before_reload", True)
    with m._load_lock:
        m._reload_locked(drop_old=True)
    assert seen["index_during_build"] is None and m.current().n == 40


def test_a_lean_index_publishes_and_restores_as_itself(conf, monkeypatch):
    from services import index_snapshot as snap
    from test_index_snapshot import FakeContainer
    monkeypatch.setattr(settings, "local_rescore", True)
    container = FakeContainer()
    store = snap.SnapshotStore(container, "index-snapshot")
    table = filled_store()
    built = lean.build_lean(table, True, ("themuse.com",))
    manifest = store.publish(built, "v1", {"themuse.com"})
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    same_index(built, restored)
    assert ask(built) == ask(restored)
