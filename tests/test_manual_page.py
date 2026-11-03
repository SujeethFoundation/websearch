import hashlib
import pytest
from services.manual_page import build_page_documents

URL = "https://app.candid.org/profile/15027428/the-sujeeth-foundation-93-1506908"
TEXT = "The Sujeeth Foundation connects college students with professional mentors. " * 6


def build(**kw):
    args = dict(url=URL, title="The Sujeeth Foundation | Candid", text=TEXT,
                allowed_concepts=["sujeeth_foundation"],
                tag_fn=lambda title, chunk, allowed: list(allowed),
                chunk_chars=200, min_chars=80)
    args.update(kw)
    return build_page_documents(**args)


def test_chunk_ids_match_crawler_convention_and_cover_all_text():
    docs, domain, digest = build()
    page_id = hashlib.sha256(URL.encode()).hexdigest()
    assert domain == "app.candid.org"
    assert [d["id"] for d in docs] == [f"{page_id}-{i}" for i in range(len(docs))]
    assert len(docs) > 1
    assert "".join(d["content"] for d in docs) == " ".join(TEXT.split())
    assert {d["page_hash"] for d in docs} == {digest}
    assert all(d["page_type"] == "manual" and d["url"] == URL for d in docs)


def test_tags_each_chunk_within_allowed_concepts():
    docs, _, _ = build()
    assert all(d["concepts"] == ["sujeeth_foundation"] for d in docs)


def test_explicit_concepts_must_be_allowed_for_the_domain():
    with pytest.raises(ValueError, match="volunteer"):
        build(explicit_concepts=["sujeeth_foundation", "volunteer"])
    docs, _, _ = build(explicit_concepts=["sujeeth_foundation"])
    assert docs[0]["concepts"] == ["sujeeth_foundation"]


@pytest.mark.parametrize("kw,msg", [
    (dict(url="http://app.candid.org/x"), "https"),
    (dict(title="  "), "title"),
    (dict(text="too short"), "at least"),
])
def test_rejects_bad_input(kw, msg):
    with pytest.raises(ValueError, match=msg):
        build(**kw)


def test_same_text_gives_same_ids_so_resubmitting_replaces_rather_than_duplicates():
    a, _, _ = build()
    b, _, _ = build()
    assert [d["id"] for d in a] == [d["id"] for d in b]


def test_description_is_stored_on_every_chunk():
    docs, _, _ = build(description="A short description of the page.")
    assert {d["description"] for d in docs} == {"A short description of the page."}
