
from services.concept_resolver import normalize, tokens


def test_normalize():
    assert normalize("Resume-writing!") == "resume writing"


def test_tokens():
    assert tokens("Week 3: Career Circle") == {"week", "3", "career", "circle"}
