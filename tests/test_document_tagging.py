from services.concept_resolver import ConceptResolver

class FakeOntology:
    def ontology(self):
        return {
            "ontology_version": "test",
            "classes": [
                {"id": "resume_resource", "name": "Resume Resource", "aliases": ["CV writing", "resume builder"]},
                {"id": "volunteer", "name": "Volunteer", "aliases": ["volunteering"]},
                {"id": "career_circle_program", "name": "Career Circle Program"},
            ],
        }
    def classes(self):
        return {"classes": []}
    def terminology(self):
        return {"terminology": []}
    def relationships(self):
        return {"relationships": []}

def test_tags_exact_phrase_only_within_allowed_domain_scope():
    resolver = ConceptResolver(FakeOntology())
    assert resolver.tag_document(
        "Resume Writing Guide",
        "Learn how to write a resume and prepare a CV.",
        ["resume_resource", "volunteer"],
    ) == ["resume_resource"]

def test_does_not_assign_unapproved_concepts():
    resolver = ConceptResolver(FakeOntology())
    assert resolver.tag_document(
        "Resume Writing Guide",
        "Learn how to write a resume.",
        ["volunteer"],
    ) == []

def test_single_word_alias_matches_complete_token():
    resolver = ConceptResolver(FakeOntology())
    assert resolver.tag_document(
        "Volunteer Opportunities",
        "Volunteering helps students and mentors.",
        ["volunteer"],
    ) == ["volunteer"]
