"""One pass for several searches: search_many must give exactly what the same searches give one at a time, while
working out what they share (the question's embedding, keyword scores, word pairs and code distances) once."""
import threading

import numpy as np
import pytest

import function_app as fa
from services import index_snapshot as snap
from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_index_snapshot import FakeContainer  # noqa: F401
from test_local_index import DIMS, WORDS, conf, index_of, make_doc, make_service, new_store  # noqa: F401
from test_local_rescoring import VectorEmbedder

MAIN_DOMAINS = ["manual.sujeeth.org", "sujeeth.org", "themuse.com"]
MAIN_CONCEPTS = ["circle", "mentor", "spotlight"]


def big_corpus(pages=120, per_page=4, seed=11):
    """Three domains (a small manual one inside the big ones' filter), concepts including spotlight on some pages."""
    rng = np.random.default_rng(seed)
    docs = []
    for p in range(pages):
        domain = ["themuse.com", "sujeeth.org", "themuse.com", "manual.sujeeth.org"][p % 4] if p % 9 else "sujeeth.org"
        concepts = [["circle"], ["mentor"], ["circle", "mentor"], ["resume"]][p % 4]
        if domain == "sujeeth.org" and p % 3 == 0:
            concepts = concepts + ["spotlight"]
        if domain == "manual.sujeeth.org":
            concepts = ["circle", "mentor"]                  # manually added pages carry the question's own concepts
        for n in range(per_page):
            docs.append(make_doc(p, n, title=f"Title {p} {WORDS[p % len(WORDS)]}", domain=domain, concepts=concepts, rng=rng))
    return docs


def jobs_like_production(**extra):
    """The three searches run_search makes: main, best bets (manual domain) and spotlights."""
    base = dict(candidates=50, text_candidates=1000, phrase_pool=100, **extra)
    return [
        dict(domains=MAIN_DOMAINS, concepts=MAIN_CONCEPTS, limit=5, concept_weights={"mentor": 1.0, "circle": 0.5},
             concept_boost=1.0, max_per_page=2, phrase_boost=1.0, unique_pages=True, **base),
        dict(domains=["manual.sujeeth.org"], concepts=MAIN_CONCEPTS, limit=10, concept_weights=None, concept_boost=1.0,
             max_per_page=2, phrase_boost=1.0, unique_pages=True, **dict(base, candidates=100)),
        dict(domains=["sujeeth.org"], concepts=["spotlight"], limit=6, concept_weights=None, concept_boost=1.0,
             max_per_page=2, phrase_boost=1.0, unique_pages=True, **dict(base, candidates=60)),
    ]


def separately(index, terms, qcode, qvec, question, jobs):
    out = []
    for j in jobs:
        out.append(index.search(
            terms, qcode, j["domains"], j["concepts"], candidates=j["candidates"], limit=j["limit"], qvec=qvec,
            text_candidates=j.get("text_candidates"), concept_weights=j.get("concept_weights"),
            concept_boost=j.get("concept_boost", 0.0), max_per_page=j.get("max_per_page", 0), question=question,
            phrase_boost=j.get("phrase_boost", 0.0), phrase_pool=j.get("phrase_pool", 100),
            unique_pages=j.get("unique_pages", False)))
    return out


def together(index, terms, qcode, qvec, question, jobs):
    return index.search_many(index.begin(terms, qcode, qvec, question), jobs)


def question_inputs(seed):
    rng = np.random.default_rng(seed)
    question = " ".join(rng.choice(WORDS, 4))
    vector = rng.standard_normal(DIMS)
    return question, li.query_terms(question), li.binary_code(vector), li.quantize_int8(vector)


@pytest.fixture
def index(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_pool", 20)     # a shortlist is made even on this small corpus
    return index_of(big_corpus())


# ---------------------------------------------------------------------------------------------------------------
# The same answers
# ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("seed", range(25))
def test_one_pass_gives_exactly_what_three_searches_give(index, seed):
    question, terms, qcode, qvec = question_inputs(seed)
    jobs = jobs_like_production()
    assert together(index, terms, qcode, qvec, question, jobs) == separately(index, terms, qcode, qvec, question, jobs)


@pytest.mark.parametrize("pool", [20, 300, 5000])
@pytest.mark.parametrize("unique", [True, False])
@pytest.mark.parametrize("phrase_boost", [0.0, 1.0])
@pytest.mark.parametrize("max_per_page", [0, 2])
def test_the_same_whatever_the_ranking_settings_and_shortlist_size(index, monkeypatch, pool, unique, phrase_boost, max_per_page):
    monkeypatch.setattr(settings, "local_rescore_pool", pool)
    question, terms, qcode, qvec = question_inputs(3)
    jobs = [dict(j, unique_pages=unique, phrase_boost=phrase_boost, max_per_page=max_per_page) for j in jobs_like_production()]
    assert together(index, terms, qcode, qvec, question, jobs) == separately(index, terms, qcode, qvec, question, jobs)


def test_the_same_with_codes_only_and_with_keywords_only(index):
    question, terms, qcode, _ = question_inputs(4)
    jobs = jobs_like_production()
    assert together(index, terms, qcode, None, question, jobs) == separately(index, terms, qcode, None, question, jobs)      # no vector
    assert together(index, terms, None, None, question, jobs) == separately(index, terms, None, None, question, jobs)       # no embedding


def test_jobs_that_do_not_lie_inside_each_other_are_still_the_same(index):
    question, terms, qcode, qvec = question_inputs(5)
    jobs = [dict(jobs_like_production()[0], domains=["themuse.com"], concepts=["circle"]),
            dict(jobs_like_production()[0], domains=["sujeeth.org"], concepts=["mentor", "resume"]),
            dict(jobs_like_production()[1])]
    assert together(index, terms, qcode, qvec, question, jobs) == separately(index, terms, qcode, qvec, question, jobs)


def test_the_order_of_the_jobs_does_not_change_any_answer(index):
    question, terms, qcode, qvec = question_inputs(6)
    jobs = jobs_like_production()
    forward = together(index, terms, qcode, qvec, question, jobs)
    backward = together(index, terms, qcode, qvec, question, list(reversed(jobs)))
    assert backward == list(reversed(forward))


def test_a_job_with_no_matching_chunks_answers_nothing_like_a_single_search(index):
    question, terms, qcode, qvec = question_inputs(7)
    jobs = jobs_like_production() + [dict(jobs_like_production()[0], domains=["nowhere.example"])]
    out = together(index, terms, qcode, qvec, question, jobs)
    assert out[3] == ([], 0) and out == separately(index, terms, qcode, qvec, question, jobs)


def test_an_empty_index_answers_nothing(conf):
    empty = li.LocalIndex(iter([]))
    question, terms, qcode, qvec = question_inputs(8)
    assert together(empty, terms, qcode, qvec, question, jobs_like_production()) == [([], 0)] * 3


def test_a_job_that_fails_is_reported_in_place_and_the_others_still_answer(index):
    question, terms, qcode, qvec = question_inputs(9)
    jobs = jobs_like_production()
    broken = {"domains": ["sujeeth.org"], "concepts": ["circle"], "limit": 5}              # no "candidates"
    out = together(index, terms, qcode, qvec, question, [jobs[0], broken, jobs[2]])
    assert isinstance(out[1], KeyError)
    expect = separately(index, terms, qcode, qvec, question, [jobs[0], jobs[2]])
    assert [out[0], out[2]] == expect


# ---------------------------------------------------------------------------------------------------------------
# The shared work is done once
# ---------------------------------------------------------------------------------------------------------------

def test_the_shared_work_is_done_once_not_once_per_search(index, monkeypatch):
    question, terms, qcode, qvec = question_inputs(10)
    jobs = jobs_like_production()
    counts = {"keywords": 0, "phrases": 0, "popcount": 0}
    real_kw, real_ph, real_pop = index.keyword_scores, index.question_phrases, li._popcount_rows

    def kw(t):
        counts["keywords"] += 1
        return real_kw(t)

    def ph(q):
        counts["phrases"] += 1
        return real_ph(q)

    def pop(words):
        counts["popcount"] += 1
        return real_pop(words)
    monkeypatch.setattr(index, "keyword_scores", kw)
    monkeypatch.setattr(index, "question_phrases", ph)
    monkeypatch.setattr(li, "_popcount_rows", pop)

    sizes = [len(index._rows(j["domains"], j["concepts"])) for j in jobs]
    assert all(size > settings.local_rescore_pool for size in sizes), sizes                 # every job needs the code distances
    separately(index, terms, qcode, qvec, question, jobs)
    assert counts == {"keywords": 3, "phrases": 3, "popcount": 3}                           # what it cost before
    for key in counts:
        counts[key] = 0
    together(index, terms, qcode, qvec, question, jobs)
    assert counts == {"keywords": 1, "phrases": 1, "popcount": 1}


def test_a_part_that_no_search_needs_is_never_worked_out(index, monkeypatch):
    question, terms, qcode, qvec = question_inputs(11)
    calls = []
    monkeypatch.setattr(index, "question_phrases", lambda q: calls.append(q) or [])
    jobs = [dict(j, phrase_boost=0.0) for j in jobs_like_production()]
    together(index, terms, qcode, qvec, question, jobs)
    assert calls == []


# ---------------------------------------------------------------------------------------------------------------
# On an index restored from a snapshot (what the instances really search)
# ---------------------------------------------------------------------------------------------------------------

def test_the_same_on_an_index_restored_from_a_snapshot(index, conf):
    container = FakeContainer()
    store = snap.SnapshotStore(container, "index-snapshot")
    manifest = store.publish(index, "v1", set())
    restored = store.load(manifest, li.LocalIndex, keep_text=True, text_skip=set(), rescore=True)
    assert isinstance(restored.texts, snap.LazyTexts)
    question, terms, qcode, qvec = question_inputs(12)
    jobs = jobs_like_production()
    assert together(restored, terms, qcode, qvec, question, jobs) == separately(index, terms, qcode, qvec, question, jobs)


# ---------------------------------------------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------------------------------------------

def service(embedder=None):
    docs = big_corpus()
    store = new_store()
    store.upsert_entities([li.entity_from_search_doc(d) for d in docs])
    store.bump_version()
    return make_service(store=store, embedder=embedder)


def service_jobs():
    return [
        dict(concepts=MAIN_CONCEPTS, domains=MAIN_DOMAINS, limit=5, concept_weights={"mentor": 1.0}),
        dict(concepts=MAIN_CONCEPTS, domains=["manual.sujeeth.org"], limit=10),
        dict(concepts=["spotlight"], domains=["sujeeth.org"], limit=6),
    ]


def test_the_service_gives_the_same_answers_and_embeds_the_question_once(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_pool", 20)
    embedder = VectorEmbedder(np.random.default_rng(2).standard_normal(DIMS))
    svc = service(embedder)
    question = "w3 w14 w27 mentor circle"
    one_pass = svc.search_many(question, service_jobs())
    assert embedder.calls == [question]
    apart = [svc.search(question, **job) for job in service_jobs()]
    assert one_pass == apart and len(embedder.calls) == 4
    assert all(a["count"] > 0 for a in one_pass[:2]) and one_pass[0]["search_mode"] == "hybrid"


def test_a_pass_counts_as_one_search_in_the_numbers(conf):
    svc = service()
    svc.search_many("w3 w14", service_jobs())
    assert svc.local.describe()["searches"] == 1


def test_an_invalid_job_is_reported_in_place(conf):
    svc = service()
    jobs = service_jobs()
    out = svc.search_many("w3 w14", [jobs[0], dict(jobs[1], concepts=[]), jobs[2]])
    assert isinstance(out[1], ValueError) and out[0]["count"] > 0 and out[2]["query"] == "w3 w14"


def test_a_question_that_cannot_be_embedded_falls_back_to_keywords_for_every_job(conf):
    svc = service()
    svc.embedder.fail = True
    out = svc.search_many("w3 w14", service_jobs())
    assert [a["search_mode"] for a in out] == ["keyword"] * 3 and [a["embedding_enabled"] for a in out] == [False] * 3


def test_the_search_cache_serves_each_job_and_is_shared_with_single_searches(conf, monkeypatch):
    monkeypatch.setattr(settings, "search_cache_seconds", 60)
    ss._query_cache_clear()
    embedder = VectorEmbedder(np.random.default_rng(5).standard_normal(DIMS))
    svc = service(embedder)
    first = svc.search_many("w3 w14 w27", service_jobs())
    assert len(embedder.calls) == 1
    again = svc.search_many("W3  w14 w27?", service_jobs())                               # same question: case, spacing, punctuation
    assert len(embedder.calls) == 1
    assert [a["query"] for a in again] == ["W3  w14 w27?"] * 3                                 # a cached answer carries the asker's own wording
    assert [dict(a, query="w3 w14 w27") for a in again] == first
    single = svc.search("w3 w14 w27", **service_jobs()[1])
    assert len(embedder.calls) == 1 and single == first[1]
    fresh = svc.search_many("w3 w14 w27", service_jobs() + [dict(service_jobs()[0], limit=3)])   # one new job: only it is computed
    assert len(embedder.calls) == 2 and fresh[:3] == first
    ss._query_cache_clear()


def test_an_instance_still_loading_refuses_the_whole_pass(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_index_fail_fast", True)
    svc = service()
    svc.local._load_lock.acquire()
    try:
        with pytest.raises(li.LocalIndexLoading):
            svc.search_many("w3", service_jobs())
    finally:
        svc.local._load_lock.release()


# ---------------------------------------------------------------------------------------------------------------
# run_search
# ---------------------------------------------------------------------------------------------------------------

RESOLUTION = {"matched_concepts": MAIN_CONCEPTS, "domains": MAIN_DOMAINS, "ontology_version": "1", "concept_matches": []}


def wire(monkeypatch, svc, bet_domains=("manual.sujeeth.org",), resolution=None):
    monkeypatch.setattr(fa, "searcher", svc)
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(resolution or RESOLUTION))
    monkeypatch.setattr(fa, "plan_best_bets", lambda domains: (list(bet_domains), False))


def legacy_run_search(svc, query, resolution):
    """What run_search did before: three separate searches, applied one after the other."""
    result = svc.search(query, concepts=resolution["matched_concepts"], domains=resolution["domains"], limit=5,
                        concept_weights=fa.question_concept_weights(resolution))
    result.update({"status": "completed", "ontology_version": resolution.get("ontology_version"),
                   "concepts": resolution["matched_concepts"], "domains": resolution["domains"], "resolution": resolution})
    bets = svc.search(query, concepts=resolution["matched_concepts"], domains=["manual.sujeeth.org"], limit=10)
    spot = svc.search(query, concepts=["spotlight"], domains=["sujeeth.org"], limit=fa.SPOTLIGHT_SLOTS * 3)
    fa.add_best_bets(result, bets, ["manual.sujeeth.org"])
    fa.add_spotlights(result, spot, ["sujeeth.org"])
    if settings.search_unique_pages:
        result["results"] = fa.unique_pages(result.get("results"))
        result["count"] = len(result["results"])
    return result


@pytest.mark.parametrize("query", ["w3 w14 mentor", "w27 w5 w60 circle", "week 4 agenda"])
def test_run_search_gives_the_same_final_answer_as_three_separate_searches(conf, monkeypatch, query):
    monkeypatch.setattr(settings, "local_rescore_pool", 20)
    svc = service(VectorEmbedder(np.random.default_rng(len(query)).standard_normal(DIMS)))
    wire(monkeypatch, svc)
    now = fa.run_search(query)
    assert now == legacy_run_search(svc, query, dict(RESOLUTION))
    assert now["best_bets"]["status"] in ("applied", "no_match") and now["spotlights"]["status"] in ("applied", "no_match")


def test_run_search_makes_only_the_searches_it_needs(conf, monkeypatch):
    svc = service()
    wire(monkeypatch, svc, bet_domains=())
    seen = []
    real = svc.search_many
    monkeypatch.setattr(svc, "search_many", lambda q, jobs: seen.append(len(jobs)) or real(q, jobs))
    out = fa.run_search("w3 w14", limit=5)
    assert seen == [2] and out["best_bets"]["status"] == "none"                             # main + spotlights (spotlight is matched)
    wire(monkeypatch, svc, bet_domains=("manual.sujeeth.org",), resolution=dict(RESOLUTION, matched_concepts=["circle", "mentor"]))
    seen.clear()
    out = fa.run_search("w3 w14", limit=5)
    assert seen == [2] and "spotlights" not in out                                          # main + best bets, no spotlight concept


def test_a_side_search_that_fails_leaves_the_main_answer_alone(conf, monkeypatch):
    svc = service()
    wire(monkeypatch, svc)
    real = svc.search_many
    monkeypatch.setattr(svc, "search_many", lambda q, jobs: [real(q, jobs)[0]] + [RuntimeError("table busy")] * (len(jobs) - 1))
    out = fa.run_search("w3 w14 mentor")
    assert out["status"] == "completed" and out["best_bets"]["status"] == "unavailable"
    assert out["spotlights"]["status"] == "unavailable" and out["count"] > 0


def test_a_failing_main_search_fails_the_request_as_before(conf, monkeypatch):
    svc = service()
    wire(monkeypatch, svc)
    monkeypatch.setattr(svc, "search_many", lambda q, jobs: [ValueError("no concepts")] + [None] * (len(jobs) - 1))
    with pytest.raises(ValueError):
        fa.run_search("w3 w14 mentor")


def test_an_unreadable_manifest_marks_best_bets_unavailable_and_still_searches(conf, monkeypatch):
    svc = service()
    wire(monkeypatch, svc)
    monkeypatch.undo()                                                                       # back to the real planner, with a broken ontology
    monkeypatch.setattr(fa, "searcher", svc)
    monkeypatch.setattr(fa.concept_resolver, "resolve_query", lambda **kw: dict(RESOLUTION))

    def broken():
        raise RuntimeError("ONTOLOGY_API_URL is not configured")
    monkeypatch.setattr(fa.ontology, "search_manifest", broken)
    out = fa.run_search("w3 w14 mentor")
    assert out["best_bets"] == {"status": "unavailable", "count": 0, "max": fa.BEST_BETS_MAX} and out["count"] > 0


def test_run_search_makes_one_pass_over_the_index(conf, monkeypatch):
    svc = service(VectorEmbedder(np.random.default_rng(9).standard_normal(DIMS)))
    wire(monkeypatch, svc)
    fa.run_search("w3 w14 mentor")
    assert svc.local.describe()["searches"] == 1 and len(svc.embedder.calls) == 1


# ---------------------------------------------------------------------------------------------------------------
# relevance of best bets, and how many results a request may ask for
# ---------------------------------------------------------------------------------------------------------------

def page_keys(results):
    return [fa._page_identity(r) for r in results]


@pytest.mark.parametrize("query", ["w3 w14 mentor", "w27 w5 w60 circle", "week 4 agenda"])
def test_a_best_bet_must_also_rank_in_the_main_search(conf, monkeypatch, query):
    monkeypatch.setattr(settings, "local_rescore_pool", 20)
    svc = service(VectorEmbedder(np.random.default_rng(len(query)).standard_normal(DIMS)))
    monkeypatch.setattr(settings, "best_bet_max_rank", 0)
    wire(monkeypatch, svc)
    ungated = fa.run_search(query, limit=10)
    monkeypatch.setattr(settings, "best_bet_max_rank", 3)
    wire(monkeypatch, svc)
    gated = fa.run_search(query, limit=10)
    # The main ranking alone (run_search puts reserved spotlight pages in front of it, which is not a rank).
    main = svc.search(query, concepts=RESOLUTION["matched_concepts"], domains=RESOLUTION["domains"], limit=10,
                      concept_weights=fa.question_concept_weights(RESOLUTION))
    top3 = set(page_keys(main["results"])[:3])
    bets = [r for r in gated["results"] if r.get("best_bet")]
    assert all(fa._page_identity(r) in top3 for r in bets)                      # every best bet is a top-3 page of the main search
    assert {fa._page_identity(r) for r in bets} <= {fa._page_identity(r) for r in ungated["results"] if r.get("best_bet")}
    assert gated["best_bets"]["count"] == len(bets)


def test_the_check_off_places_every_manual_page_first_as_before(conf, monkeypatch):
    monkeypatch.setattr(settings, "local_rescore_pool", 20)
    svc = service(VectorEmbedder(np.random.default_rng(3).standard_normal(DIMS)))
    monkeypatch.setattr(settings, "best_bet_max_rank", 0)
    wire(monkeypatch, svc)
    out = fa.run_search("w3 w14 mentor", limit=5)
    assert "not_relevant" not in out["best_bets"]


class FakeRequest:
    params = {}

    def __init__(self, body):
        self.body = body

    def get_json(self):
        return self.body


@pytest.mark.parametrize("asked, expected", [(50, 20), (12, 12), (None, 5), (0, 1)])
def test_a_request_may_ask_for_up_to_the_configured_limit(monkeypatch, asked, expected):
    seen = []
    monkeypatch.setattr(fa, "run_search", lambda **kw: seen.append(kw["limit"]) or {"results": []})
    body = {"query": "mentor"} if asked is None else {"query": "mentor", "limit": asked}
    fa.search_post(FakeRequest(body))
    assert seen == [expected]


def test_the_limit_ceiling_is_a_setting(monkeypatch):
    seen = []
    monkeypatch.setattr(fa, "run_search", lambda **kw: seen.append(kw["limit"]) or {"results": []})
    monkeypatch.setattr(settings, "search_max_limit", 5)
    fa.search_post(FakeRequest({"query": "mentor", "limit": 50}))
    assert seen == [5]


# ---------------------------------------------------------------------------------------------------------------
# boosting Spotlights
# ---------------------------------------------------------------------------------------------------------------

CAREER = {"concept_matches": [
    {"concept_id": "career_development", "confidence": 1.2, "source": "query_match"},
    {"concept_id": "spotlight", "confidence": None, "source": "relationship"}]}


def test_a_spotlight_added_by_relationship_counts_toward_the_ranking(monkeypatch):
    monkeypatch.setattr(settings, "spotlight_concept_weight", 1.0)
    assert fa.question_concept_weights(CAREER) == {"career_development": 1.2, "spotlight": 1.0}


def test_the_spotlight_weight_is_a_setting_and_zero_leaves_it_out(monkeypatch):
    monkeypatch.setattr(settings, "spotlight_concept_weight", 0.5)
    assert fa.question_concept_weights(CAREER)["spotlight"] == 0.5
    monkeypatch.setattr(settings, "spotlight_concept_weight", 0)
    assert "spotlight" not in fa.question_concept_weights(CAREER)


def test_other_relationship_concepts_stay_out_of_the_weights(monkeypatch):
    monkeypatch.setattr(settings, "spotlight_concept_weight", 1.0)
    res = {"concept_matches": [{"concept_id": "networking", "confidence": 1.1, "source": "query_match"},
                               {"concept_id": "week_4", "confidence": None, "source": "relationship"}]}
    assert fa.question_concept_weights(res) == {"networking": 1.1}


def test_a_spotlight_the_question_named_keeps_its_own_weight(monkeypatch):
    monkeypatch.setattr(settings, "spotlight_concept_weight", 1.0)
    res = {"concept_matches": [{"concept_id": "spotlight", "confidence": 1.4, "source": "query_match"}]}
    assert fa.question_concept_weights(res) == {"spotlight": 1.4}


def test_three_spotlight_slots_by_default():
    assert settings.spotlight_slots == 3 and fa.SPOTLIGHT_SLOTS == 3
