"""Tests for the prebuilt index snapshot (services/index_snapshot.py) and how the manager uses it."""
import json
import types
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError

from services import index_snapshot as snap
from services import local_index as li
from services.config import settings
from test_local_index import DIMS, conf, corpus, entities, filled_store, wait_for  # noqa: F401  (conf is a fixture)


# ---------------------------------------------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------------------------------------------

class FakeContainer:
    """Just enough of a ContainerClient: named blobs with a last-modified time, no overwrite unless asked."""

    def __init__(self):
        self.blobs = {}
        self.uploads, self.downloads = [], []
        self.fail_uploads_matching = None

    def upload_blob(self, name, data, overwrite=False, **kwargs):
        if self.fail_uploads_matching and self.fail_uploads_matching in name:
            raise RuntimeError("the upload failed")
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("the blob exists")
        self.blobs[name] = (bytes(data), datetime.now(timezone.utc))
        self.uploads.append(name)

    def download_blob(self, name, **kwargs):
        if name not in self.blobs:
            raise ResourceNotFoundError("no such blob")
        self.downloads.append(name)
        data = self.blobs[name][0]
        return types.SimpleNamespace(readall=lambda: data)

    def list_blobs(self, name_starts_with=""):
        for name in sorted(self.blobs):
            if name.startswith(name_starts_with):
                yield types.SimpleNamespace(name=name, last_modified=self.blobs[name][1])

    def delete_blob(self, name):
        del self.blobs[name]

    def age(self, name, seconds):
        self.blobs[name] = (self.blobs[name][0], datetime.now(timezone.utc) - timedelta(seconds=seconds))


@pytest.fixture
def snaps(conf, monkeypatch):
    monkeypatch.setattr(settings, "index_snapshot_enabled", True)
    monkeypatch.setattr(settings, "index_snapshot_autobuild", True)
    monkeypatch.setattr(settings, "index_snapshot_keep_seconds", 3600)
    monkeypatch.setattr(settings, "local_rescore", True)
    container = FakeContainer()
    return container, snap.SnapshotStore(container, "index-snapshot")


def manager(store, container_store, skip=()):
    return li.LocalIndexManager(store=store, snapshot_store=container_store, text_skip_provider=lambda: set(skip))


def count_scans(store):
    calls = []
    original = store.scan

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    store.scan = counting
    return calls


def published(container):
    return wait_for(lambda: "index-snapshot/latest.json" in container.blobs)


def ask(index, text="w5 w60 w120", concepts=("circle", "mentor"), domains=("sujeeth.org", "themuse.com")):
    vec = np.random.default_rng(9).standard_normal(DIMS)
    hits, matching = index.search(
        li.query_terms(text), li.binary_code(vec), domains, concepts, qvec=li.quantize_int8(vec), question=text,
        phrase_boost=1.0, concept_weights={"circle": 1.0, "mentor": 0.5}, concept_boost=1.0, limit=5, unique_pages=True)
    return [(index.ids[row], round(float(score), 9)) for row, score in hits], matching


ARRAYS = ("domain_codes", "page_codes", "chunk_numbers", "concept_bits", "codes", "vocab", "post_docs", "post_weights",
          "post_ptr", "vec8", "norm8")
PLAIN = ("n", "skipped", "ids", "titles", "urls", "concepts", "texts", "domain_names", "concept_names", "code_bytes",
         "concept_words", "distinct_pages", "vec_dims", "rows_without_vectors", "text_skip_domains", "pages", "by_url", "by_id")


def same_index(a, b):
    for name in PLAIN:
        assert getattr(a, name) == getattr(b, name), name
    for name in ARRAYS:
        x, y = getattr(a, name), getattr(b, name)
        assert (x is None) == (y is None), name
        if x is not None:
            assert x.dtype == y.dtype and x.shape == y.shape and np.array_equal(x, y), name
    assert a.memory_bytes() == b.memory_bytes()


def build(skip=("themuse.com",), keep_text=True):
    rows = (li.row_from_entity(e) for e in entities(corpus()))
    return li.LocalIndex(rows, keep_text=keep_text, text_skip_domains=skip)


# ---------------------------------------------------------------------------------------------------------------
# A restored index is the same index
# ---------------------------------------------------------------------------------------------------------------

def test_a_restored_index_holds_and_answers_exactly_like_the_original(snaps):
    container, store = snaps
    original = build()
    manifest = store.publish(original, "v1", {"themuse.com"})
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    same_index(original, restored)
    for text in ("w5 w60 w120", "w1 w2", "nothing matches zzz"):
        assert ask(original, text) == ask(restored, text)
    assert ask(original, concepts=("resume",)) == ask(restored, concepts=("resume",))
    assert restored.texts[original.ids.index("page1-0")] is None      # a text-cold domain stays cold
    assert restored.texts[original.ids.index("page0-0")]              # the others keep their text


def test_the_manifest_goes_last_and_lists_what_it_holds(snaps):
    container, store = snaps
    manifest = store.publish(build(), "v1", {"themuse.com"})
    assert container.uploads[-1] == "index-snapshot/latest.json"
    assert manifest["version"] == "v1" and manifest["scalars"]["n"] == 100 and manifest["has_vec8"] is True
    assert manifest["text_skip"] == ["themuse.com"] and manifest["keep_text"] is True
    for name, spec in manifest["arrays"].items():
        assert len(container.blobs[f"index-snapshot/{manifest['build']}/{name}.bin"][0]) == spec["bytes"]


def test_restoring_for_a_reader_that_drops_more_text_or_the_vectors(snaps):
    container, store = snaps
    manifest = store.publish(build(skip=()), "v1", set())
    more = store.load(manifest, li.LocalIndex, keep_text=True, text_skip={"themuse.com"}, rescore=True)
    same_index(build(skip=("themuse.com",)), more)
    container.downloads.clear()
    plain = store.load(manifest, li.LocalIndex, keep_text=False, text_skip=set(), rescore=False)
    assert plain.texts is None and plain.vec8 is None and plain.norm8 is None and plain.vec_dims == 0
    assert plain.rows_without_vectors == plain.n                       # as a scan leaves it with rescoring off
    assert not any(name.endswith(("vec8.bin", "norm8.bin")) for name in container.downloads)   # the biggest blob is not fetched


def test_compatibility_says_why_not(snaps):
    _, store = snaps
    manifest = store.publish(build(), "v1", {"themuse.com"})
    ok = dict(version="v1", keep_text=True, text_skip={"themuse.com"}, rescore=True)
    assert snap.compatible(manifest, **ok) == (True, "")
    assert "changed" in snap.compatible(manifest, **{**ok, "version": "v2"})[1]
    assert "version row" in snap.compatible(manifest, **{**ok, "version": None})[1]
    assert "no text for themuse.com" in snap.compatible(manifest, **{**ok, "text_skip": set()})[1]
    assert snap.compatible({**manifest, "keep_text": False}, **ok)[0] is False
    assert snap.compatible({**manifest, "has_vec8": False}, **ok)[0] is False
    assert "format" in snap.compatible({**manifest, "format": 99}, **ok)[1]
    assert snap.compatible(None, **ok)[0] is False


# ---------------------------------------------------------------------------------------------------------------
# The manager: snapshot first, table as the fallback
# ---------------------------------------------------------------------------------------------------------------

def test_the_second_instance_loads_the_snapshot_and_never_scans_the_table(snaps):
    container, store = snaps
    table = filled_store()
    first = manager(table, store, skip=["themuse.com"])
    original = first.current()
    assert first.describe()["load_source"] == "table" and published(container)

    scans = count_scans(table)
    second = manager(table, store, skip=["themuse.com"])
    loaded = second.current()
    assert scans == [] and second.describe()["load_source"] == "snapshot"
    assert second.describe()["snapshot"]["load_seconds"] is not None and second.describe()["build_seconds"] is None
    same_index(original, loaded)
    assert ask(original) == ask(loaded)


def test_a_changed_table_means_a_scan_and_a_new_snapshot(snaps):
    container, store = snaps
    table = filled_store(corpus(pages=4))
    manager(table, store).current()
    assert published(container)
    table.upsert_entities(entities(corpus(pages=8)))
    table.bump_version()

    scans = count_scans(table)
    fresh = manager(table, store)
    assert fresh.current().n == 40 and len(scans) == 1
    assert "changed" in fresh.describe()["snapshot"]["not_used_because"]
    assert wait_for(lambda: json.loads(container.blobs["index-snapshot/latest.json"][0])["scalars"]["n"] == 40)
    scans.clear()
    assert manager(table, store).current().n == 40 and scans == []


def test_an_instance_that_wants_text_the_snapshot_lacks_scans_instead(snaps):
    container, store = snaps
    table = filled_store()
    manager(table, store, skip=["themuse.com"]).current()
    assert published(container)
    scans = count_scans(table)
    reader = manager(table, store, skip=[])
    assert reader.current().texts[reader.current().ids.index("page1-0")]
    assert len(scans) == 1 and "no text for themuse.com" in reader.describe()["snapshot"]["not_used_because"]


def test_a_reader_without_rescoring_skips_the_vectors_and_one_with_it_needs_them(snaps, monkeypatch):
    container, store = snaps
    table = filled_store()
    monkeypatch.setattr(settings, "local_rescore", False)
    manager(table, store).current()                                    # built without 8-bit vectors
    assert published(container) and json.loads(container.blobs["index-snapshot/latest.json"][0])["has_vec8"] is False
    scans = count_scans(table)
    plain = manager(table, store)
    assert plain.current().vec8 is None and scans == []
    monkeypatch.setattr(settings, "local_rescore", True)
    rescoring = manager(table, store)
    assert rescoring.current().vec8 is not None and len(scans) == 1    # the snapshot could not serve it


def test_a_damaged_snapshot_falls_back_to_the_table(snaps):
    container, store = snaps
    table = filled_store()
    manager(table, store).current()
    assert published(container)
    build_name = json.loads(container.blobs["index-snapshot/latest.json"][0])["build"]
    name = f"index-snapshot/{build_name}/codes.bin"
    container.blobs[name] = (container.blobs[name][0][:-8], container.blobs[name][1])
    scans = count_scans(table)
    reader = manager(table, store)
    assert reader.current().n == 100 and len(scans) == 1               # searches still work
    assert "codes is" in reader.describe()["snapshot"]["error"]


def test_a_missing_blob_or_container_error_falls_back_too(snaps):
    container, store = snaps
    table = filled_store()
    manager(table, store).current()
    assert published(container)
    del container.blobs["index-snapshot/latest.json"]                  # no manifest: nothing published yet
    reader = manager(table, store)
    assert reader.current().n == 100 and "no snapshot" in reader.describe()["snapshot"]["not_used_because"]

    class Broken(FakeContainer):
        def download_blob(self, name, **kwargs):
            raise RuntimeError("storage is down")

    broken = manager(table, snap.SnapshotStore(Broken(), "index-snapshot"))
    assert broken.current().n == 100 and "storage is down" in broken.describe()["snapshot"]["error"]


def test_when_the_ontology_cannot_be_read_the_snapshots_text_set_is_kept(snaps):
    container, store = snaps
    table = filled_store()
    manager(table, store, skip=["themuse.com"]).current()
    assert published(container)

    def down():
        raise RuntimeError("the ontology is throttled")

    reader = li.LocalIndexManager(store=table, snapshot_store=store, text_skip_provider=down)
    reader.current()
    assert reader.describe()["load_source"] == "snapshot"
    assert reader.describe()["text_not_in_memory_domains"] == ["themuse.com"]   # not a load with every text in memory


def test_a_failed_publish_never_breaks_loading_and_frees_the_claim(snaps):
    container, store = snaps
    container.fail_uploads_matching = "meta.json.gz"
    table = filled_store()
    first = manager(table, store)
    assert first.current().n == 100
    assert wait_for(lambda: first.describe()["snapshot"]["error"] is not None)
    assert "index-snapshot/latest.json" not in container.blobs
    assert not [name for name in container.blobs if name.endswith("claim.json")]   # the next instance may try at once
    container.fail_uploads_matching = None
    assert first._publish_snapshot(first.current(), first._version, first._text_skip) is not None


def test_autobuild_off_publishes_nothing_and_snapshots_off_touch_nothing(snaps, monkeypatch):
    container, store = snaps
    monkeypatch.setattr(settings, "index_snapshot_autobuild", False)
    m = manager(filled_store(), store)
    m.current()
    assert wait_for(lambda: False, 0.3) is False and container.blobs == {}
    monkeypatch.setattr(settings, "index_snapshot_autobuild", True)
    monkeypatch.setattr(settings, "index_snapshot_enabled", False)
    off = manager(filled_store(), store)
    assert off.current().n == 100
    assert container.blobs == {} and container.downloads == [] and off.describe()["snapshot"]["enabled"] is False


def test_the_snapshot_route_path_rebuilds_from_the_table_and_publishes_now(snaps):
    container, store = snaps
    table = filled_store()
    m = manager(table, store)
    m.current()
    assert published(container)
    uploads = len(container.uploads)
    m.reload(use_snapshot=False, publish=True)                         # what POST /api/index/snapshot runs
    assert len(container.uploads) > uploads and m.describe()["load_source"] == "table"
    status = m.snapshot_status()
    assert status["current"]["matches_table"] is True and status["current"]["chunks"] == 100
    assert status["current"]["megabytes"] > 0 and status["published"]["version"] == m._version


# ---------------------------------------------------------------------------------------------------------------
# Claims and cleanup
# ---------------------------------------------------------------------------------------------------------------

def test_only_one_instance_builds_a_given_snapshot(snaps):
    container, store = snaps
    index = build()
    assert store.publish(index, "v1", {"themuse.com"}) is not None
    uploads = len(container.uploads)
    assert store.publish(index, "v1", {"themuse.com"}) is None          # already claimed
    assert len(container.uploads) == uploads
    assert store.publish(index, "v1", {"themuse.com"}, force=True) is not None
    assert store.publish(index, "v1", set()) is not None                # a different text set is a different build


def test_a_dead_builders_claim_is_taken_over(snaps):
    container, store = snaps
    index = build()
    store.publish(index, "v1", {"themuse.com"})
    build_name = json.loads(container.blobs["index-snapshot/latest.json"][0])["build"]
    claim = f"index-snapshot/{build_name}/claim.json"
    container.age(claim, snap.CLAIM_STALE_SECONDS - 60)
    assert store.publish(index, "v1", {"themuse.com"}) is None
    container.age(claim, snap.CLAIM_STALE_SECONDS + 60)
    assert store.publish(index, "v1", {"themuse.com"}) is not None


def test_cleanup_keeps_the_current_build_and_recent_ones(snaps):
    container, store = snaps
    index = build()
    store.publish(index, "v1", {"themuse.com"})
    old_build = json.loads(container.blobs["index-snapshot/latest.json"][0])["build"]
    for name in [n for n in container.blobs if old_build in n]:
        container.age(name, 7200)
    store.publish(index, "v2", {"themuse.com"})
    assert not [n for n in container.blobs if old_build in n]           # the old build is gone
    current = json.loads(container.blobs["index-snapshot/latest.json"][0])["build"]
    assert len([n for n in container.blobs if current in n]) > 5
    store.publish(index, "v3", {"themuse.com"})
    assert [n for n in container.blobs if current in n]                 # a recent build stays: a reader may be downloading it
    assert "index-snapshot/latest.json" in container.blobs


def test_the_settings_default_to_on_with_a_prefix_nothing_else_lists():
    assert settings.index_snapshot_enabled and settings.index_snapshot_autobuild
    assert settings.index_snapshot_prefix == "index-snapshot"
    assert not "index-snapshot/".startswith(("documents/", "vectors/"))
