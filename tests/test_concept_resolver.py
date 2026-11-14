
from services.concept_resolver import normalize, tokens


def test_normalize():
    assert normalize("Resume-writing!") == "resume writing"


def test_tokens():
    assert tokens("Week 3: Career Circle") == {"week", "3", "career", "circle"}


# Filler words must not match concepts. Before, "make a gift" matched "how to make a pyramid" and "tell me about
# yourself" matched "tell me how to make a vodka tonic", so unrelated questions returned program pages.
def _score_text(query, alias):
    from services.concept_resolver import ConceptResolver, normalize
    indexed = ConceptResolver._index_aliases([alias])
    qn = normalize(query)
    return ConceptResolver._score_indexed(qn, set(qn.split()), indexed)[0]


def test_filler_words_do_not_match_an_alias():
    assert _score_text("Teach me how to make a pyramid.", "make a gift") < 0.28
    assert _score_text("Tell me how to make a vodka tonic.", "tell me about yourself") < 0.28
    assert _score_text("Give me directions to a bar.", "ways to give") < 0.28


def test_content_words_and_whole_phrases_still_match():
    assert _score_text("How do I make a gift to the foundation?", "make a gift") >= 1.0
    assert _score_text("ways to give", "ways to give") >= 1.0
    assert _score_text("tell me about yourself answer", "tell me about yourself") >= 1.0
    assert _score_text("interview prep", "interview") >= 0.28
