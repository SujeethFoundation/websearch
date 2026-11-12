"""Keyword fusion depth: a chunk strong on vectors gets its keyword credit even when its keyword rank is deep."""
import numpy as np

from services import local_index as li
from services.config import settings
from test_local_index import conf, filled_store, index_of, make_doc, make_service  # noqa: F401
from test_local_rescoring import VectorEmbedder

DIMS = 64


def depth_corpus():
    """Y: the only chunk with 'alpha' (keyword rank 1), the worst vector. Z: the best vector, but the weakest keyword
    score of the 82 chunks that mention 'common' (keyword rank 82). 80 chunks (A) match only by keyword and sit far from
    the question's vector; 49 chunks (B) sit near it and have no keyword match, so no chunk is strong in both lists
    except Z once the keyword side is read deep enough."""
    rng = np.random.default_rng(4)
    q = rng.standard_normal(DIMS)
    noise = lambda scale: scale * rng.standard_normal(DIMS)
    docs = [make_doc(0, 0, title="alpha", content="alpha common", vector=-q)]                        # Y: row 0
    for i in range(1, 81):
        docs.append(make_doc(i, 0, title=f"filler {i}", content=f"common word{i} extra", vector=-q + noise(0.5)))
    for i in range(81, 130):
        docs.append(make_doc(i, 0, title=f"other {i}", content=f"unrelated topic{i} text", vector=q + noise(0.8)))
    docs.append(make_doc(500, 0, title="long", content="common " + " ".join(f"pad{j}" for j in range(400)), vector=q))   # Z
    return q, docs


def test_a_chunk_strong_on_vectors_gets_its_keyword_credit_even_when_its_keyword_rank_is_deep(conf):
    q, docs = depth_corpus()
    index = index_of(docs)
    qcode, qvec = li.binary_code(q), li.quantize_int8(q)
    terms = li.query_terms("alpha common")
    shallow, _ = index.search(terms, qcode, ["sujeeth.org"], ["circle"], candidates=50, limit=2, qvec=qvec, text_candidates=50)
    assert index.ids[shallow[0][0]] == "page0-0"                      # Y: keyword rank 1 ties with Z's vector rank 1, and Y comes first
    deep, _ = index.search(terms, qcode, ["sujeeth.org"], ["circle"], candidates=50, limit=2, qvec=qvec, text_candidates=1000)
    assert index.ids[deep[0][0]] == "page500-0"                       # Z also gets 1/(60+~120) from the keyword side


def test_the_search_reads_a_thousand_keyword_results_by_default_and_follows_local_text_recall(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_phrase_boost", 0.0)          # this test is about the depth alone
    q, docs = depth_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    first = lambda: svc.search("alpha common", concepts=["circle"], domains=["sujeeth.org"], limit=2)["results"][0]["chunk_id"]
    assert first() == "page500-0"                                      # default 1000
    monkeypatch.setattr(settings, "local_text_recall", 50)            # a shallower fusion: Z gets no keyword credit
    assert first() == "page0-0"
    monkeypatch.setattr(settings, "local_text_recall", 0)             # 0 means the default again
    assert first() == "page500-0"
