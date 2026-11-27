"""Chunk ids held as page ids plus chunk numbers, read like the list of strings they replace."""
import numpy as np
import pytest

from services import local_index as li
from test_local_index import conf, corpus, entities, index_of, make_doc  # noqa: F401  (conf is a fixture)


def ids_of(*names):
    return li.ChunkIds.compact(list(names))


def test_ids_read_back_exactly_like_the_list_they_replace():
    names = ["abc-0", "abc-1", "x-y-z-0", "abc-12", "q-3"]
    ids = ids_of(*names)
    assert isinstance(ids, li.ChunkIds) and len(ids) == 5
    assert list(ids) == names and [ids[i] for i in range(5)] == names and ids[-1] == "q-3"
    assert ids[1:4] == names[1:4] and ids == names and names == list(ids)
    assert ids.index("x-y-z-0") == 2 and "abc-12" in ids and "abc-2" not in ids and "nope" not in ids
    with pytest.raises(IndexError):
        ids[5]
    with pytest.raises(ValueError):
        ids.index("abc-2")
    assert ids.total_chars() == sum(len(n) for n in names)
    assert ids.page_ids == ["abc", "x-y-z", "q"]                   # a page id is held once, however many chunks it has


@pytest.mark.parametrize("bad", ["abc-007", "nodash", "-1", "abc-", "abc-x", "abc-1234567890"])
def test_an_id_that_would_not_read_back_keeps_the_plain_list(bad):
    assert li.ChunkIds.compact(["ok-1", bad]) is None


def test_the_index_holds_ids_compactly_and_looks_chunks_up_the_same_way(conf):
    docs = [make_doc(p, n) for p in range(6) for n in range(4)]
    compact = index_of(docs)
    assert isinstance(compact.ids, li.ChunkIds) and compact.chunk_numbers is compact.ids.numbers
    plain = li.LocalIndex([li.row_from_entity(li.entity_from_search_doc(d)) for d in docs])
    plain.ids = list(compact.ids)
    plain._finish_lookups()                                         # the same index built the old way
    assert compact.pages == plain.pages and compact.by_url == plain.by_url
    assert compact.by_id == plain.by_id and dict(compact.by_id.items()) == plain.by_id
    for cid in ("page2-3", "page5-0"):
        assert compact.by_id[cid] == plain.by_id[cid] and cid in compact.by_id
    assert "page2-9" not in compact.by_id and compact.by_id.get("page2-9") is None and compact.by_id.get("junk") is None
    with pytest.raises(KeyError):
        compact.by_id["page9-0"]
    assert len(compact.by_id) == len(plain.by_id) == 24


def test_a_page_whose_chunks_arrive_out_of_order_still_lists_them_by_number(conf):
    docs = [make_doc(1, n) for n in (3, 0, 2, 1)] + [make_doc(2, 0)]
    index = index_of(docs)
    assert [index.ids[r] for r in index.pages["page1"]] == ["page1-0", "page1-1", "page1-2", "page1-3"]


def test_an_odd_id_falls_back_to_the_plain_list_and_everything_still_works(conf):
    docs = [make_doc(1, 0), make_doc(2, 0)]
    docs[1]["id"] = "page2-007"
    index = index_of(docs)
    assert isinstance(index.ids, list) and index.ids == ["page1-0", "page2-007"]
    assert index.by_id["page2-007"] == 1


def test_compact_ids_are_far_smaller_than_the_strings(conf):
    docs = [make_doc(p, n) for p in range(200) for n in range(5)]
    index = index_of(docs)
    strings = sum(__import__("sys").getsizeof(s) for s in list(index.ids))
    assert index.ids.memory_bytes() < strings / 3
    assert index.memory_breakdown()["python_objects_mb"]["ids"] < index.memory_breakdown()["python_objects_mb"]["urls"] + 1


def test_the_breakdown_counts_each_shared_concept_list_once(conf):
    docs = [make_doc(p, n, concepts=("circle", "week_4")) for p in range(50) for n in range(4)]
    index = index_of(docs)
    one_list = __import__("sys").getsizeof(index.concepts[0])
    assert index.memory_breakdown()["python_objects_mb"]["concepts"] * 1e6 < 200 * 8 + 50 * one_list
