"""Rescoring: the 1-bit codes pick a shortlist, smaller copies of the stored vectors rank it."""
import numpy as np
import pytest

from services import local_index as li
from services.config import settings
from test_local_index import conf, filled_store, index_of, make_doc, make_service  # noqa: F401  (conf is a fixture)

NO_TERMS = np.zeros(0, dtype=np.uint64)


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def trap_vectors(dims=64):
    """A question q, a chunk A that is really close to it, and a chunk B whose signs match q better but whose size does
    not: B differs from q in 4 signs (large values), A in 6 signs (tiny values). Hamming distance prefers B, cosine A."""
    q = np.ones(dims)
    a = np.ones(dims)
    a[:6] = -0.01
    b = np.full(dims, 0.01)
    b[:4] = -5.0
    return q, a, b


def trap_index(**kw):
    q, a, b = trap_vectors()
    docs = [make_doc(1, 0, vector=a, content="alpha"), make_doc(2, 0, vector=b, content="beta")]
    return q, index_of(docs, **kw)


def ids(index, hits):
    return [index.ids[row] for row, _ in hits]


def test_quantizing_to_8_bits_keeps_the_direction_and_ignores_scale():
    rng = np.random.default_rng(1)
    v = rng.standard_normal(1536)
    q = np.frombuffer(li.quantize_int8(v), dtype=np.int8).astype(np.float64)
    assert q.shape == (1536,) and abs(q).max() == 127
    assert float(unit(q) @ unit(v)) > 0.9995
    assert li.quantize_int8(v) == li.quantize_int8(v * 37.5)
    assert li.quantize_int8(np.zeros(8)) == bytes(8)


def test_rows_carry_the_8_bit_vector_and_read_it_back(conf):
    doc = make_doc(1, 0)
    entity = li.entity_from_search_doc(doc)
    assert len(entity["v8"]) == len(doc["contentVector"]) and len(entity["vec"]) == len(doc["contentVector"]) // 8
    assert li.row_from_entity(entity)["v8"] == entity["v8"]


def test_the_codes_alone_rank_the_trap_wrongly_and_rescoring_gets_it_right(conf):
    q, index = trap_index()
    qcode, qvec = li.binary_code(q), li.quantize_int8(q)
    by_codes, _ = index.search(NO_TERMS, qcode, ["sujeeth.org"], ["circle"], candidates=2, limit=2)
    assert ids(index, by_codes) == ["page2-0", "page1-0"]                      # Hamming 4 beats 6
    rescored, _ = index.search(NO_TERMS, qcode, ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=qvec)
    assert ids(index, rescored) == ["page1-0", "page2-0"]                      # cosine puts the real match first


def test_rescoring_is_all_or_nothing(conf):
    q, a, b = trap_vectors()
    entities = [li.entity_from_search_doc(make_doc(1, 0, vector=a, content="alpha")),
                li.entity_from_search_doc(make_doc(2, 0, vector=b, content="beta"))]
    entities[1]["v8"] = b""                                                    # a row written before vectors were kept
    index = li.LocalIndex(li.row_from_entity(e) for e in entities)
    assert index.rvec is None and index.rows_without_vectors == 1
    hits, _ = index.search(NO_TERMS, li.binary_code(q), ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=li.quantize_int8(q))
    assert ids(index, hits) == ["page2-0", "page1-0"]                          # quietly the codes alone


def test_rescoring_can_be_turned_off(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore", False)
    q, index = trap_index()
    assert index.rvec is None
    hits, _ = index.search(NO_TERMS, li.binary_code(q), ["sujeeth.org"], ["circle"], candidates=2, limit=2, qvec=li.quantize_int8(q))
    assert ids(index, hits) == ["page2-0", "page1-0"]


def test_only_the_shortlist_the_codes_pick_is_rescored(conf, monkeypatch):
    q, index = trap_index()
    qcode, qvec = li.binary_code(q), li.quantize_int8(q)
    monkeypatch.setattr(settings, "local_rescore_pool", 1)
    rows = np.array([0, 1], dtype=np.int64)
    assert index.vector_ranking(rows, qcode, qvec, 1, pool=1) == [1]            # the code shortlist is B alone; A is never seen
    assert index.vector_ranking(rows, qcode, qvec, 1, pool=2) == [0]            # a wider shortlist reaches A, the better match
    assert index.vector_ranking(rows, qcode, qvec, 2, pool=1) == [0, 1]         # the shortlist is never smaller than what is asked for


def clustered(n=2500, dims=256, clusters=60, seed=5):
    rng = np.random.default_rng(seed)
    mean = rng.normal(0, 0.15, dims)
    scale = np.exp(rng.normal(0, 0.5, dims))
    centers = rng.normal(0, 1, (clusters, dims)) * scale
    make = lambda c: unit(centers[c] * 0.8 + rng.normal(0, 1, dims) * scale * 0.6 + mean)
    vectors = np.array([make(rng.integers(0, clusters)) for _ in range(n)])
    queries = np.array([make(rng.integers(0, clusters)) for _ in range(120)])
    return vectors, queries


def test_rescoring_agrees_with_exact_cosine_far_more_often_than_the_codes_do(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_bits", 8)        # exact enough to demand 90%; the 4-bit shape has its own test
    monkeypatch.setattr(settings, "local_rescore_dims", 0)
    vectors, queries = clustered()
    docs = [make_doc(i, 0, vector=v, content=f"chunk {i}") for i, v in enumerate(vectors)]
    index = index_of(docs)
    rows = np.arange(index.n, dtype=np.int64)
    agree_codes = agree_rescored = 0
    for q in queries:
        exact = int(np.argmax(vectors @ q))
        qcode, qvec = li.binary_code(q), li.quantize_int8(q)
        agree_codes += index.vector_ranking(rows, qcode, None, 1)[0] == exact
        agree_rescored += index.vector_ranking(rows, qcode, qvec, 1, pool=100)[0] == exact
    assert agree_rescored / len(queries) >= 0.9
    assert agree_rescored - agree_codes >= 0.3 * len(queries)


def test_the_index_counts_the_vectors_in_its_memory(conf):
    docs = [make_doc(i, 0) for i in range(10)]
    with_vectors = index_of(docs)
    rows = [li.row_from_entity(li.entity_from_search_doc(d)) for d in docs]
    for r in rows:
        r["v8"] = b""
    without = li.LocalIndex(rows)
    width = li.rescore_width(*li.rescore_params(64))
    assert with_vectors.memory_bytes() - without.memory_bytes() == 10 * width + 10 * 4


class VectorEmbedder:
    """Like EmbeddingService.embed_query_vectors, with one chosen vector for every question."""
    def __init__(self, vector):
        self.vector = vector
        self.calls = []

    def embed_query_vectors(self, text):
        self.calls.append(text)
        return li.binary_code(self.vector), li.quantize_int8(self.vector)


def test_a_local_search_rescores_when_the_index_and_the_question_have_vectors(conf):
    q, a, b = trap_vectors()
    docs = [make_doc(1, 0, vector=a, content="alpha"), make_doc(2, 0, vector=b, content="beta")]
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    answer = svc.search("zzzz", concepts=["circle"], domains=["sujeeth.org"], limit=2)
    assert [r["chunk_id"] for r in answer["results"]] == ["page1-0", "page2-0"]
    d = svc.local.describe()
    assert d["rescoring"]["active"] is True and d["rescoring"]["rescored_searches"] == 1 and d["rescoring"]["rows_without_vectors"] == 0


def test_status_says_rescoring_is_inactive_when_rows_lack_vectors(conf):
    docs = [make_doc(i, 0) for i in range(4)]
    store = filled_store(docs)
    for key, row in store.chunks.rows.items():
        row.pop("v8", None)
    svc = make_service(store=store)
    svc.search("alpha", concepts=["circle"], domains=["sujeeth.org"], limit=2)
    r = svc.local.describe()["rescoring"]
    assert r["enabled"] is True and r["active"] is False and r["rows_without_vectors"] == 4 and r["rescored_searches"] == 0


def test_running_the_local_backfill_writes_the_8_bit_vectors_so_the_index_can_rescore(conf, monkeypatch):
    from test_local_index import backfill_service
    svc, store, names = backfill_service()
    monkeypatch.setattr(settings, "embedding_configured", True, raising=False)
    # rows written before vectors were kept: take them out, then run the backfill again
    svc.index_blobs(names)
    for row in store.chunks.rows.values():
        row.pop("v8", None)
    assert li.LocalIndexManager(store=store).current().rvec is None
    svc.index_blobs(names)
    index = li.LocalIndexManager(store=store).current()
    assert index.rvec is not None and index.rows_without_vectors == 0 and index.rvec.shape == (index.n, li.rescore_width(*li.rescore_params(64)))


def test_a_question_made_of_common_words_still_finds_chunks_unless_they_are_turned_off(conf, monkeypatch):
    docs = [make_doc(1, 0, content="what is the plan for the week", title="Week plan"),
            make_doc(2, 0, content="mentor guide and resume tips", title="Guide")]
    index = index_of(docs)
    hits, matching = index.search(li.query_terms("what is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert [index.ids[r] for r, _ in hits] == ["page1-0"]                      # indexed words match
    monkeypatch.setattr(settings, "local_index_stopwords", "english")
    hits, _ = index.search(li.query_terms("what is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert [index.ids[r] for r, _ in hits] == ["page1-0"]                      # "what" is not on the list, so it still matches
    hits, _ = index.search(li.query_terms("is the"), None, ["sujeeth.org"], ["circle"], candidates=5, limit=5)
    assert hits == []                                                          # a question of only listed words searches for nothing


# ---------------------------------------------------------------------------------------------------------------
# The smaller copies: 4 bits and a cut number of dimensions
# ---------------------------------------------------------------------------------------------------------------

def test_the_defaults_hold_four_bits_over_1024_of_1536_dimensions(monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_bits", 4)
    monkeypatch.setattr(settings, "local_rescore_dims", 1024)
    assert li.rescore_params(1536) == (1024, 4) and li.rescore_width(1024, 4) == 512
    assert li.rescore_params(64) == (64, 4)                      # a vector shorter than the cut is kept whole
    monkeypatch.setattr(settings, "local_rescore_dims", 0)
    assert li.rescore_params(1536) == (1536, 4)
    monkeypatch.setattr(settings, "local_rescore_bits", 8)
    assert li.rescore_params(1536) == (1536, 8) and li.rescore_width(1536, 8) == 1536


@pytest.mark.parametrize("dims", [64, 63])
def test_four_bit_packing_round_trips_to_a_close_direction(dims):
    rng = np.random.default_rng(3)
    v = rng.standard_normal((20, 200))
    stored = np.array([np.frombuffer(li.quantize_int8(r), dtype=np.int8) for r in v])
    packed, norms = li.pack_rescore(stored, dims, 4)
    assert packed.shape == (20, (dims + 1) // 2) and packed.dtype == np.uint8 and norms.shape == (20,)
    back = li.unpack_rescore(packed, dims, 4)
    assert back.shape == (20, dims) and abs(back).max() <= 7
    assert np.allclose(np.sqrt((back * back).sum(axis=1)), norms, rtol=1e-5)
    cos = (back * v[:, :dims]).sum(axis=1) / (np.linalg.norm(back, axis=1) * np.linalg.norm(v[:, :dims], axis=1))
    assert cos.min() > 0.97


def test_eight_bit_packing_with_every_dimension_is_the_stored_vector():
    rng = np.random.default_rng(4)
    stored = np.array([np.frombuffer(li.quantize_int8(r), dtype=np.int8) for r in rng.standard_normal((5, 32))])
    packed, norms = li.pack_rescore(stored, 32, 8)
    assert np.array_equal(packed, stored)
    assert np.allclose(norms, np.sqrt((stored.astype(np.float32) ** 2).sum(axis=1)))
    assert np.array_equal(li.unpack_rescore(packed, 32, 8), stored.astype(np.float32))


def test_a_zero_vector_packs_to_zeros_with_a_norm_of_one():
    for bits in (4, 8):
        packed, norms = li.pack_rescore(np.zeros((2, 16), dtype=np.int8), 16, bits)
        assert norms.tolist() == [1.0, 1.0]
        assert not li.unpack_rescore(packed, 16, bits).any()


def test_the_builder_packs_in_blocks_and_gives_up_when_a_row_has_no_vector(monkeypatch):
    monkeypatch.setattr(li, "RESCORE_BLOCK", 3)
    rng = np.random.default_rng(5)
    stored = [np.frombuffer(li.quantize_int8(r), dtype=np.int8).tobytes() for r in rng.standard_normal((8, 40))]
    b = li.RescoreBuilder(8, 40)
    for raw in stored:
        b.add(raw)
    data, norms = b.finish()
    whole, whole_norms = li.pack_rescore(np.array([np.frombuffer(x, dtype=np.int8) for x in stored]), b.dims, b.bits)
    assert np.array_equal(data, whole) and np.array_equal(norms, whole_norms)
    broken = li.RescoreBuilder(8, 40)
    for raw in stored[:4] + [b""] + stored[5:]:
        broken.add(raw)
    assert broken.finish() == (None, None)


def test_four_bit_rescoring_still_ranks_a_close_neighbour_first(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_bits", 4)
    monkeypatch.setattr(settings, "local_rescore_dims", 0)
    q, index = trap_index()
    assert index.rescore_bits == 4 and index.rvec.shape == (2, 32)
    rows = np.array([0, 1], dtype=np.int64)
    assert index.vector_ranking(rows, li.binary_code(q), li.quantize_int8(q), 1, pool=2) == [0]


def test_a_cut_number_of_dimensions_uses_the_cut_query_vector(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_bits", 8)
    monkeypatch.setattr(settings, "local_rescore_dims", 32)
    q, index = trap_index()
    assert index.rescore_dims == 32 and index.vec_dims == 64 and index.rvec.shape == (2, 32)
    rows = np.array([0, 1], dtype=np.int64)
    assert index.vector_ranking(rows, li.binary_code(q), li.quantize_int8(q), 1, pool=2) == [0]
    assert index.vector_ranking(rows, li.binary_code(q), bytes(10), 1, pool=2)      # a question vector of the wrong size falls back to the codes


def test_the_default_shape_agrees_closely_with_the_exact_ranking_on_clustered_vectors(conf, monkeypatch):
    """4 bits over every dimension (the 1,024 cut needs real Matryoshka embeddings to be meaningful, so it is measured
    with tools/compare_rescoring.py on the real index). Synthetic vectors with many near-ties are a hard case."""
    monkeypatch.setattr(settings, "local_rescore_bits", 4)
    monkeypatch.setattr(settings, "local_rescore_dims", 0)
    vectors, queries = clustered()
    index = index_of([make_doc(i, 0, vector=v, content=f"chunk {i}") for i, v in enumerate(vectors)])
    rows = np.arange(index.n, dtype=np.int64)
    kept = 0.0
    for q in queries:
        exact = set(np.argsort(-(vectors @ q))[:10].tolist())
        got = set(index.vector_ranking(rows, li.binary_code(q), li.quantize_int8(q), 10, pool=300))
        kept += len(exact & got) / 10
    assert kept / len(queries) >= 0.75


def test_compare_tool_reports_the_agreement_and_the_memory_saved(conf, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import compare_rescoring
    monkeypatch.setattr(settings, "local_rescore_bits", 8)
    monkeypatch.setattr(settings, "local_rescore_dims", 0)
    vectors, _ = clustered(n=400)
    index = index_of([make_doc(i, 0, vector=v, content=f"chunk {i}") for i, v in enumerate(vectors)])
    same = compare_rescoring.compare(index, 8, 0, queries=40, pool=100)
    assert same["top1"] == 1.0 and same["recall"] == 1.0
    small = compare_rescoring.compare(index, 4, 128, queries=40, pool=100)
    assert small["bits"] == 4 and small["dims"] == 128 and 0.0 <= small["recall"] <= 1.0
    assert small["megabytes"] < same["megabytes"] / 3


def test_the_scaling_clip_can_be_chosen_and_changes_only_four_bit_rows():
    rng = np.random.default_rng(6)
    stored = np.array([np.frombuffer(li.quantize_int8(r), dtype=np.int8) for r in rng.standard_normal((6, 64))])
    a, _ = li.pack_rescore(stored, 64, 4)
    b, _ = li.pack_rescore(stored, 64, 4, clip=li.RESCORE_CLIP)
    c, _ = li.pack_rescore(stored, 64, 4, clip=2.0)
    assert np.array_equal(a, b) and not np.array_equal(a, c)
    assert np.array_equal(li.pack_rescore(stored, 64, 8, clip=2.0)[0], stored)
