"""Ontology relationships can widen a search (expand) or change its ranking (boost, suppress), and one shared word of a
longer phrase no longer matches a concept."""
import numpy as np
import pytest

import function_app as fa
from services import concept_resolver as cr
from services import local_index as li
from services import search_service as ss
from services.config import settings
from test_local_index import conf, index_of, make_doc  # noqa: F401
from test_local_relevance import DOMAINS, DIMS

CLASSES = [
    {"id": "week", "name": "Week", "aliases": ["weekly topic"]},
    {"id": "week_3", "name": "Week 3", "aliases": []},
    {"id": "week_4", "name": "Week 4", "aliases": []},
    {"id": "career_circle_program", "name": "Career Circle Program", "aliases": ["the program"]},
    {"id": "referral", "name": "Referral", "aliases": []},
    {"id": "networking", "name": "Networking", "aliases": []},
    {"id": "spotlight", "name": "Spotlight", "aliases": []},
    {"id": "receiving_feedback", "name": "Receiving Feedback", "aliases": ["rejection"]},
    {"id": "feedback_survey", "name": "Feedback Survey", "aliases": []},
    {"id": "first_job", "name": "First Job", "aliases": ["monthly gift", "what the world needs"]},
    {"id": "resume_resource", "name": "Resume Resource", "aliases": ["resume review"], "query_phrases": ["resume"]},
]
PREDICATES = [
    {"id": "illustrates", "search_behavior": {"enabled": True, "matched_endpoint": "object", "effect": "expand", "weight": 0}},
    {"id": "is_a", "search_behavior": {"enabled": True, "matched_endpoint": "object", "effect": "expand", "weight": 0.5}},
    {"id": "contains", "search_behavior": {"enabled": True, "matched_endpoint": "subject", "effect": "expand", "weight": 0.5}},
    {"id": "is_part_of", "search_behavior": {"enabled": True, "matched_endpoint": "subject", "effect": "boost", "weight": 0.25}},
    {"id": "supports", "search_behavior": {"enabled": True, "matched_endpoint": "object", "effect": "boost", "weight": 0.25}},
    {"id": "is_distinct_from", "search_behavior": {"enabled": True, "matched_endpoint": "either", "effect": "suppress", "weight": 0.5}},
    {"id": "precedes"},
]
RELATIONSHIPS = [
    {"id": "spot", "subject": "spotlight", "predicate": "illustrates", "object": "networking"},
    {"id": "w3", "subject": "week_3", "predicate": "is_a", "object": "week"},
    {"id": "w4", "subject": "week_4", "predicate": "is_a", "object": "week"},
    {"id": "prog", "subject": "career_circle_program", "predicate": "contains", "object": "week"},
    {"id": "part", "subject": "referral", "predicate": "is_part_of", "object": "networking"},
    {"id": "sup", "subject": "networking", "predicate": "supports", "object": "referral"},
    {"id": "apart", "subject": "receiving_feedback", "predicate": "is_distinct_from", "object": "feedback_survey"},
    {"id": "next", "subject": "week_3", "predicate": "precedes", "object": "week_4",
     "search_behavior": {"enabled": True, "matched_endpoint": "either", "effect": "suppress", "weight": 0.5}},
]


class Ontology:
    def __init__(self, predicates=PREDICATES, relationships=RELATIONSHIPS):
        self.predicates, self.rels = predicates, relationships

    def ontology(self):
        return {"classes": CLASSES, "predicates": self.predicates, "ontology_version": "t"}

    def classes(self):
        return {"classes": CLASSES}

    def terminology(self):
        return {"terminology": []}

    def relationships(self):
        return {"relationships": self.rels}

    def resolve_domains(self, concepts, purpose="search"):
        return {"domains": [{"domain": "sujeeth.org"}], "ontology_version": "t"}


def resolve(query, **kw):
    return cr.ConceptResolver(Ontology(**kw)).resolve_query(query)


def effects(query, **kw):
    return {e["concept_id"]: (e["effect"], e["weight"]) for e in resolve(query, **kw).get("relationship_effects", [])}


# ---- matching ---------------------------------------------------------------------------------------------------

def score(query, alias):
    indexed = cr.ConceptResolver._index_aliases([alias])
    qn = cr.normalize(query)
    return cr.ConceptResolver._score_indexed(qn, set(qn.split()), indexed)[0]


def test_one_shared_word_of_a_longer_phrase_is_not_a_match():
    assert score("what was the first moon landing", "First Job") < 0.28
    assert score("monthly car payment calculator", "monthly gift") < 0.28
    assert score("who won the world series", "what the world needs") < 0.28      # one topic word: the phrase must appear whole
    assert score("base camp at Everest", "base resume") < 0.28


def test_a_phrase_matches_on_whole_words_and_accepts_a_plural():
    assert score("jobbing actors", "job") < 0.28 and score("swaying trees", "sway") < 0.28
    assert score("how do I find a job", "job") >= 1.0
    assert score("tell me about career circles", "career circle") >= 1.0
    assert score("what the world needs", "what the world needs") >= 1.0


def test_two_shared_topic_words_still_match_partly():
    assert 0.28 <= score("review of my old resume", "resume review") < 1.0


def test_a_single_word_reaches_its_concept_only_when_listed():
    assert "resume_resource" in resolve("fix my resume")["matched_concepts"]       # "resume" is a query phrase
    assert resolve("first moon landing")["status"] == "no_match"                  # "first" alone is not


# ---- effects ----------------------------------------------------------------------------------------------------

def test_expand_adds_the_related_concepts_to_the_search():
    found = resolve("what does each week cover")
    assert found["matched_concepts"] == ["week", "week_3", "week_4"]
    assert effects("what does each week cover") == {"week_3": ("expand", 0.5), "week_4": ("expand", 0.5)}
    sources = {m["concept_id"]: m["source"] for m in found["concept_matches"]}
    assert sources == {"week": "query_match", "week_3": "relationship", "week_4": "relationship"}


def test_expand_follows_the_direction_the_predicate_names():
    assert effects("tell me about the program") == {"week": ("expand", 0.5)}      # contains: subject matched, object added
    assert "career_circle_program" not in effects("weekly topic")                 # not the other way round


def test_boost_and_suppress_change_the_ranking_but_not_the_filter():
    found = resolve("how do I ask for a referral")
    assert found["matched_concepts"] == ["referral"]                              # networking is not searched for
    assert effects("how do I ask for a referral") == {"networking": ("boost", 0.25)}   # two relationships, counted once
    found = resolve("how do I handle a rejection")
    assert found["matched_concepts"] == ["receiving_feedback"]
    assert effects("how do I handle a rejection") == {"feedback_survey": ("suppress", -0.5)}


def test_a_relationship_can_carry_its_own_effect():
    assert effects("what happens in week 3")["week_4"] == ("suppress", -0.5)      # precedes has no effect of its own


def test_naming_one_kind_drops_the_expansion_to_the_others():
    found = resolve("what happens in week 3")                                     # matches Week 3 and Week
    assert "week_4" not in found["matched_concepts"]


def test_a_concept_the_question_matched_is_never_boosted_or_suppressed():
    assert "networking" not in effects("networking and a referral")
    assert "feedback_survey" not in effects("rejection and the feedback survey")


def test_a_relationship_or_predicate_can_opt_out():
    rels = [dict(r, search_behavior={"enabled": False}) if r["id"] == "apart" else r for r in RELATIONSHIPS]
    assert effects("how do I handle a rejection", relationships=rels) == {}
    preds = [dict(p, search_behavior={"enabled": False}) if p["id"] == "is_a" else p for p in PREDICATES]
    assert effects("what does each week cover", predicates=preds) == {}


def test_an_ontology_without_effects_behaves_as_before():
    """Only "illustrates" acts: a match on its object adds its subject to the filter, with no ranking weight."""
    old = [{"id": p["id"], "expands_search": p["id"] == "illustrates"} for p in PREDICATES]
    plain = [{k: v for k, v in r.items() if k != "search_behavior"} for r in RELATIONSHIPS]
    assert effects("networking", predicates=old, relationships=plain) == {"spotlight": ("expand", 0.0)}
    assert effects("what does each week cover", predicates=old, relationships=plain) == {}
    assert effects("how do I ask for a referral", predicates=old, relationships=plain) == {}


def test_the_setting_turns_everything_but_illustrates_off(monkeypatch):
    monkeypatch.setattr(settings, "relationship_effects", False)
    assert effects("what does each week cover") == {}
    assert effects("how do I ask for a referral") == {}
    assert effects("networking") == {"spotlight": ("expand", 0.0)}


# ---- ranking ----------------------------------------------------------------------------------------------------

def test_effects_reach_the_ranking_weights_marked_as_related(monkeypatch):
    monkeypatch.setattr(settings, "spotlight_concept_weight", 0)
    found = resolve("how do I handle a rejection")
    assert fa.question_concept_weights(found) == {"receiving_feedback": 1.1, "~feedback_survey": -0.5}
    found = resolve("what does each week cover")
    assert fa.question_concept_weights(found) == {"week": 1.1, "~week_3": 0.5, "~week_4": 0.5}
    found = resolve("networking")                                                 # expand with weight 0: filter only
    assert fa.question_concept_weights(found) == {"networking": 1.1}


def test_only_related_concepts_may_carry_a_negative_weight():
    assert ss.clean_concept_weights({"A": 1.2, "~B": -0.5, "~C": 0.25, "D": -1, "E": 0, "F": "x"}) == {"a": 1.2, "~b": -0.5, "~c": 0.25}


def corpus():
    rng = np.random.default_rng(3)
    q = rng.standard_normal(DIMS)
    docs = [make_doc(i, 0, title=f"Page {i}", content="how to take feedback after a rejection", domain="sujeeth.org",
                     concepts=tags, vector=q + 0.3 * rng.standard_normal(DIMS))
            for i, tags in enumerate([("receiving_feedback",), ("receiving_feedback", "feedback_survey"),
                                      ("receiving_feedback", "growth_mindset")], start=1)]
    return q, docs


def shares(index, weights):
    return index.concept_coverage(np.arange(len(index.ids)), weights).round(3).tolist()


def test_a_related_weight_is_added_to_the_share_without_changing_what_the_questions_own_concepts_are_worth(conf):
    _, docs = corpus()
    index = index_of(docs)
    order = [index.ids.index(f"page{i}-0") for i in (1, 2, 3)]
    own = {"receiving_feedback": 1.1}
    assert [shares(index, own)[i] for i in order] == [1.0, 1.0, 1.0]
    both = {**own, "~feedback_survey": -0.5, "~growth_mindset": 0.25}
    assert [shares(index, both)[i] for i in order] == [1.0, 0.5, 1.25]
    assert shares(index, {"~growth_mindset": 0.25}) == [0.0, 0.0, 0.0]            # nothing of the question's own to measure against


def test_suppress_ranks_a_page_lower_and_boost_higher(conf):
    q, docs = corpus()
    index = index_of(docs)

    def ranked(weights):
        hits = index.search(li.query_terms("how do I handle a rejection"), li.binary_code(q), DOMAINS, ["receiving_feedback"],
                            candidates=50, limit=5, qvec=li.quantize_int8(q), text_candidates=1000,
                            concept_weights=weights, concept_boost=1.0)[0]
        return [index.ids[row] for row, _ in hits]

    got = ranked({"receiving_feedback": 1.1, "~feedback_survey": -0.5, "~growth_mindset": 0.25})
    assert got[0] == "page3-0" and got[-1] == "page2-0" and len(got) == 3            # suppressed, not removed
