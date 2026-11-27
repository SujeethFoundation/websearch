"""Keyword postings hold chunk numbers at 3 bytes each."""
import numpy as np
import pytest

from services import local_index as li
from test_local_index import conf, filled_store, index_of, make_doc  # noqa: F401  (conf is a fixture)


def test_chunk_numbers_round_trip_through_three_bytes():
    docs = np.array([0, 1, 255, 256, 65535, 65536, 95033, 131071, 16_777_215], dtype=np.int32)
    packed = li.pack_docs(docs)
    assert packed.shape == (len(docs), 3) and packed.dtype == np.uint8
    assert np.array_equal(li.unpack_docs(packed), docs)
    assert li.unpack_docs(packed[2:5]).tolist() == [255, 256, 65535]           # a slice is a list's worth of postings


def test_the_empty_case_and_the_limit():
    assert li.pack_docs(np.zeros(0, dtype=np.int32)).shape == (0, 3)
    assert li.unpack_docs(li.pack_docs(np.zeros(0, dtype=np.int32))).shape == (0,)
    with pytest.raises(ValueError):
        li.pack_docs(np.array([1 << 24]))


def test_an_index_holds_its_postings_at_three_bytes_and_still_finds_chunks(conf):
    docs = [make_doc(i, 0, content=f"alpha word{i % 7} unique{i}") for i in range(40)]
    index = index_of(docs)
    assert index.post_docs.dtype == np.uint8 and index.post_docs.shape[1] == 3
    assert len(index.post_docs) == len(index.post_weights) == int(index.post_ptr[-1])
    hits = index.keyword_scores(np.array([li.term_hash("unique17")], dtype=np.uint64))
    assert int(np.argmax(hits)) == 17 and (hits > 0).sum() == 1
    common = index.keyword_scores(np.array([li.term_hash("alpha")], dtype=np.uint64))
    assert (common > 0).all()                                                    # a word in every chunk reaches every chunk


def test_postings_take_three_quarters_of_what_four_bytes_did(conf):
    index = index_of([make_doc(i, 0, content=f"alpha beta gamma delta {i}") for i in range(30)])
    assert index.post_docs.nbytes == len(index.post_weights) * 3
