"""Keyword fusion depth (Azure feeds up to 1,000 keyword results to its rank fusion) and the shadow diagnosis."""
import time

import numpy as np

from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_local_index import AzureSession, azure_configured, conf, filled_store, index_of, make_doc, make_service  # noqa: F401
from test_local_rescoring import VectorEmbedder, wait_compared

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
    assert index.ids[deep[0][0]] == "page500-0"                       # Z also gets 1/(60+~120) from the keyword side, as in Azure


def test_the_local_search_uses_azures_default_depth_and_follows_search_max_text_recall(conf, monkeypatch):
    q, docs = depth_corpus()
    svc = make_service(store=filled_store(docs), embedder=VectorEmbedder(q))
    first = lambda: svc.search("alpha common", concepts=["circle"], domains=["sujeeth.org"], limit=2)["results"][0]["chunk_id"]
    assert first() == "page500-0"                                      # default 1000
    monkeypatch.setattr(settings, "search_max_text_recall", 50)       # mirror an Azure query that was told to recall 50
    assert first() == "page0-0"
    monkeypatch.setattr(settings, "local_text_recall", 1000)          # an explicit local setting wins
    assert first() == "page500-0"


# ---- the diagnosis of a disagreement

def stub_azure_parts(svc, monkeypatch, fail=None):
    calls = []

    def part(query, concepts, domains, which, limit=3):
        calls.append(which)
        if fail == which:
            raise RuntimeError("azure unreachable")
        return [{"rank": 1, "title": f"{which} title", "url": f"https://azure.example/{which}", "score": 0.5, "chunk": "a-1"}]
    monkeypatch.setattr(svc, "_azure_part", part)
    return calls


def disagree(svc, query="w1 w2 w3 w4 w5 w6 w7 w8"):
    svc._maybe_shadow(query, ["circle"], ["sujeeth.org"], 5,
                      {"results": [{"chunk_id": "x-1", "url": "https://elsewhere.org/a1", "title": "T", "score": 1.0}]})


def test_a_disagreement_always_records_the_filters_and_only_diagnoses_when_asked(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    monkeypatch.setattr(settings, "local_shadow_diagnose", False)
    svc = make_service()
    calls = stub_azure_parts(svc, monkeypatch)
    disagree(svc)
    wait_compared(svc, 1)
    (diff,) = svc.local.shadow_diffs()
    assert diff["filters"] == {"concepts": ["circle"], "domains": ["sujeeth.org"]} and "halves" not in diff and calls == []


def test_diagnosing_adds_each_engines_keyword_and_vector_halves(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    monkeypatch.setattr(settings, "local_shadow_diagnose", True)
    monkeypatch.setattr(settings, "local_shadow_diagnose_min_seconds", 0)
    svc = make_service(embedder=VectorEmbedder(np.random.default_rng(1).standard_normal(DIMS)))
    calls = stub_azure_parts(svc, monkeypatch)
    disagree(svc)
    wait_compared(svc, 1)
    (diff,) = svc.local.shadow_diffs()
    halves = diff["halves"]
    assert set(halves) == {"local_keyword", "local_vector", "azure_text", "azure_vector"} and calls == ["text", "vector"]
    assert halves["azure_text"][0]["url"] == "https://azure.example/text"
    assert halves["local_keyword"] and halves["local_keyword"][0]["url"].startswith("https://sujeeth.org/")
    assert halves["local_vector"] and halves["local_vector"][0]["rank"] == 1


def test_a_failing_half_is_reported_without_losing_the_others(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    monkeypatch.setattr(settings, "local_shadow_diagnose", True)
    monkeypatch.setattr(settings, "local_shadow_diagnose_min_seconds", 0)
    svc = make_service(embedder=VectorEmbedder(np.random.default_rng(1).standard_normal(DIMS)))
    stub_azure_parts(svc, monkeypatch, fail="vector")
    disagree(svc)
    wait_compared(svc, 1)
    halves = svc.local.shadow_diffs()[0]["halves"]
    assert "azure unreachable" in halves["azure_vector"]["error"] and halves["azure_text"] and halves["local_keyword"]


def test_diagnosing_is_limited_to_one_comparison_every_few_seconds(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_shadow_sample", 1.0)
    monkeypatch.setattr(settings, "local_shadow_diagnose", True)
    monkeypatch.setattr(settings, "local_shadow_diagnose_min_seconds", 60)
    monkeypatch.setattr(ss, "_diagnose_last", [-1e9])
    svc = make_service(embedder=VectorEmbedder(np.random.default_rng(1).standard_normal(DIMS)))
    calls = stub_azure_parts(svc, monkeypatch)
    disagree(svc, "first question")
    wait_compared(svc, 1)
    disagree(svc, "second question")
    wait_compared(svc, 2)
    newest, older = svc.local.shadow_diffs()[0], svc.local.shadow_diffs()[1]
    assert "halves" in older and "halves" not in newest and calls == ["text", "vector"]


def test_each_azure_half_is_the_hybrid_query_with_the_other_half_removed(conf, monkeypatch):
    azure_configured(monkeypatch)
    hits = [{"id": "p-1", "title": "Mentor", "url": "https://sujeeth.org/m", "@search.score": 3.5}]
    session = AzureSession(hits=hits)
    svc = make_service(session=session)
    text = svc._azure_part("how do i become a mentor", ["mentor"], ["sujeeth.org"], "text")
    vector = svc._azure_part("how do i become a mentor", ["mentor"], ["sujeeth.org"], "vector")
    t_body, v_body = session.posts[0][1], session.posts[1][1]
    assert "vectorQueries" not in t_body and t_body["search"] == "how do i become a mentor" and "hybridSearch" not in t_body
    assert "search" not in v_body and "searchMode" not in v_body and v_body["vectorQueries"][0]["kind"] == "text"
    assert t_body["filter"] == v_body["filter"] and t_body["top"] == 3
    assert text == vector == [{"rank": 1, "title": "Mentor", "url": "https://sujeeth.org/m", "score": 3.5, "chunk": "p-1"}]


def test_the_full_azure_query_is_unchanged_by_the_refactor(conf, monkeypatch):
    azure_configured(monkeypatch)
    monkeypatch.setattr(settings, "search_backend", "azure")
    monkeypatch.setattr(settings, "ai_search_writes", True)
    session = AzureSession(hits=[{"id": "p-1", "title": "Mentor", "url": "https://sujeeth.org/m", "domain": "sujeeth.org",
                                  "concepts": ["mentor"], "@search.score": 0.03}])
    svc = make_service(session=session)
    svc.search("how do i become a mentor", concepts=["mentor"], domains=["sujeeth.org"], limit=5)
    body = session.posts[-1][1]
    assert body["search"] == "how do i become a mentor" and body["searchMode"] == "any" and body["top"] == 5
    assert body["vectorQueries"][0]["kind"] == "text" and body["vectorQueries"][0]["k"] >= 50 and "hybridSearch" not in body
