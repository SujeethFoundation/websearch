"""Tests for services/path_rules.py. Run from the websearch repo root: python -m pytest -q tests"""
import pytest

from services.path_rules import (
    PathPolicy,
    PathRulesUnavailable,
    is_blocked_url,
    is_excluded_path,
    is_excluded_url,
    is_outside_includes,
    normalize_rule,
    normalize_rules,
)


def test_rule_matches_the_path_and_everything_beneath_it():
    rules = normalize_rules(["/job"])
    assert is_excluded_path("/job", rules)
    assert is_excluded_path("/job/", rules)
    assert is_excluded_path("/job/retail-manager-santa-monica-ca", rules)
    assert is_excluded_path("/job/a/b/c", rules)


def test_rule_compares_whole_path_segments():
    rules = normalize_rules(["/job"])
    assert not is_excluded_path("/jobs", rules)
    assert not is_excluded_path("/jobsite/x", rules)
    assert not is_excluded_path("/career-paths/job-interview-tips", rules)
    assert not is_excluded_path("/", rules)


def test_trailing_slash_case_and_encoding_do_not_matter():
    assert normalize_rule("/Job/") == "/job"
    assert normalize_rule("job") == "/job"
    assert is_excluded_path("/JOB/Something", normalize_rules(["/job/"]))
    assert is_excluded_path("/%6Aob/x", normalize_rules(["/job"]))


def test_a_bare_slash_never_excludes_the_whole_site():
    assert normalize_rule("/") is None
    assert normalize_rule("") is None
    assert normalize_rules(["/", "", None]) == ()
    assert not is_excluded_path("/anything", normalize_rules(["/"]))


def test_rules_are_deduplicated_and_accept_a_single_string():
    assert normalize_rules(["/job", "/job/", "/JOB"]) == ("/job",)
    assert normalize_rules("/jobs") == ("/jobs",)
    assert normalize_rules(None) == ()


def test_urls_use_only_the_path():
    rules = normalize_rules(["/job"])
    assert is_excluded_url("https://www.tealhq.com/job/x?utm=1#top", rules)
    assert not is_excluded_url("https://www.tealhq.com/career-paths/job", rules)
    assert not is_excluded_url("https://www.tealhq.com/", rules)


class FakeOntology:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.calls = rows or [], fail, 0

    def crawl_manifest(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ontology down")
        return {"crawl_domains": self.rows}


ROWS = [
    {"domain": "tealhq.com", "crawl_policy": {"exclude_paths": ["/job", "/jobs"]}},
    {"domain": "sujeeth.org", "crawl_policy": {"mode": "domain"}},
    "not-a-row",
]


def test_policy_reads_rules_per_domain_and_caches_them():
    ontology = FakeOntology(ROWS)
    policy = PathPolicy(ontology)
    assert policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")
    assert policy.is_excluded("TEALHQ.com", "https://www.tealhq.com/jobs")
    assert not policy.is_excluded("tealhq.com", "https://www.tealhq.com/career-paths/x")
    assert not policy.is_excluded("sujeeth.org", "https://www.sujeeth.org/job/x")
    assert not policy.is_excluded("unknown.example", "https://unknown.example/job/x")
    assert ontology.calls == 1


def test_policy_keeps_old_rules_when_the_ontology_cannot_be_read():
    ontology = FakeOntology(ROWS)
    policy = PathPolicy(ontology, ttl_seconds=0)
    assert policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")
    ontology.fail = True
    assert policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")


def test_policy_refuses_to_answer_if_the_ontology_never_answered():
    policy = PathPolicy(FakeOntology(fail=True))
    with pytest.raises(PathRulesUnavailable):
        policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")



def test_wildcards_match_within_one_segment():
    rules = normalize_rules(["/career-paths/*-certifications"])
    assert is_excluded_path("/career-paths/snowflake-developer-certifications", rules)
    assert is_excluded_path("/career-paths/aws-certifications/extra", rules)
    assert not is_excluded_path("/career-paths/certifications-guide", rules)
    assert not is_excluded_path("/career-paths/a/b-certifications", rules)  # "*" never crosses a "/"
    assert not is_excluded_path("/career-paths", rules)


def test_wildcard_rules_mix_with_plain_rules():
    rules = normalize_rules(["/job", "/career-paths/*-interview-questions", "/tag"])
    assert is_excluded_path("/job/x", rules) and is_excluded_path("/tag/leadership", rules)
    assert is_excluded_path("/career-paths/sales-engineer-interview-questions", rules)
    assert not is_excluded_path("/career-paths/sales-engineer", rules)
    assert is_excluded_url("https://www.tealhq.com/CAREER-PATHS/Sales-Interview-Questions?x=1", rules)


# ---- allow-list (include_paths) ----

INCLUDES = normalize_rules(["/career-advice"])


def test_allow_list_blocks_everything_outside_it():
    assert not is_outside_includes("https://www.indeed.com/career-advice", INCLUDES)
    assert not is_outside_includes("https://www.indeed.com/career-advice/resumes-cover-letters/x", INCLUDES)
    assert not is_outside_includes("https://www.indeed.com/Career-Advice/", INCLUDES)
    assert is_outside_includes("https://www.indeed.com/jobs?q=nurse", INCLUDES)
    assert is_outside_includes("https://www.indeed.com/career/salaries", INCLUDES)
    assert is_outside_includes("https://www.indeed.com/cmp/Indeed/about", INCLUDES)


def test_allow_list_compares_whole_segments():
    assert is_outside_includes("https://www.indeed.com/career-advice-extra/x", INCLUDES)
    assert is_outside_includes("https://www.indeed.com/x/career-advice", INCLUDES)


def test_no_allow_list_means_nothing_is_outside_it():
    assert not is_outside_includes("https://www.indeed.com/jobs", ())
    assert not is_outside_includes("https://www.indeed.com/jobs", normalize_rules(["/"]))


def test_bare_homepage_is_allowed_but_its_query_variants_are_not():
    assert not is_outside_includes("https://www.indeed.com/", INCLUDES)
    assert not is_outside_includes("https://www.indeed.com", INCLUDES)
    assert not is_outside_includes("https://www.indeed.com/#top", INCLUDES)
    assert is_outside_includes("https://www.indeed.com/?from=gnav-career-guide", INCLUDES)


def test_allow_list_supports_wildcards_and_several_paths():
    includes = normalize_rules(["/career-advice", "/guides/*-tips"])
    assert not is_outside_includes("https://x.com/guides/resume-tips/more", includes)
    assert is_outside_includes("https://x.com/guides/resume-ideas", includes)
    assert not is_outside_includes("https://x.com/career-advice/a", includes)


def test_block_list_wins_over_allow_list():
    includes = normalize_rules(["/career-advice"])
    excludes = normalize_rules(["/career-advice/news"])
    assert not is_blocked_url("https://www.indeed.com/career-advice/interviewing", includes, excludes)
    assert is_blocked_url("https://www.indeed.com/career-advice/news/x", includes, excludes)
    assert is_blocked_url("https://www.indeed.com/jobs", includes, excludes)


def test_each_list_works_alone_and_together():
    assert is_blocked_url("https://x.com/job/1", (), normalize_rules(["/job"]))
    assert not is_blocked_url("https://x.com/other", (), normalize_rules(["/job"]))
    assert is_blocked_url("https://x.com/other", INCLUDES, ())
    assert not is_blocked_url("https://x.com/career-advice", INCLUDES, ())
    assert not is_blocked_url("https://x.com/anything", (), ())


INDEED = {
    "domain": "indeed.com",
    "crawl_policy": {
        "include_paths": ["/career-advice"],
        "exclude_paths": ["/career-advice/news"],
    },
}


def test_policy_applies_allow_and_block_lists_per_domain():
    policy = PathPolicy(FakeOntology(ROWS + [INDEED]))
    assert not policy.is_excluded("indeed.com", "https://www.indeed.com/career-advice/finding-a-job/x")
    assert policy.is_excluded("indeed.com", "https://www.indeed.com/career-advice/news/x")
    assert policy.is_excluded("indeed.com", "https://www.indeed.com/jobs?q=x")
    assert policy.is_excluded("INDEED.com", "https://www.indeed.com/cmp/x")
    assert not policy.is_excluded("indeed.com", "https://www.indeed.com/")
    # other domains are untouched by indeed's allow-list
    assert not policy.is_excluded("sujeeth.org", "https://www.sujeeth.org/jobs")
    assert policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")
    assert not policy.is_excluded("tealhq.com", "https://www.tealhq.com/anything-else")


def test_policy_exposes_both_lists():
    policy = PathPolicy(FakeOntology(ROWS + [INDEED]))
    assert policy.includes_for("indeed.com") == ("/career-advice",)
    assert policy.rules_for("indeed.com") == ("/career-advice/news",)
    assert policy.includes_for("tealhq.com") == ()
    assert policy.all_includes() == {"indeed.com": ("/career-advice",)}
    assert "tealhq.com" in policy.all_rules()


def test_policy_ignores_a_bare_slash_include():
    rows = [{"domain": "a.com", "crawl_policy": {"include_paths": ["/"]}}]
    policy = PathPolicy(FakeOntology(rows))
    assert not policy.is_excluded("a.com", "https://a.com/anything")


def test_policy_keeps_old_allow_list_when_the_ontology_cannot_be_read():
    ontology = FakeOntology(ROWS + [INDEED])
    policy = PathPolicy(ontology, ttl_seconds=0)
    assert policy.is_excluded("indeed.com", "https://www.indeed.com/jobs")
    ontology.fail = True
    assert policy.is_excluded("indeed.com", "https://www.indeed.com/jobs")


MUSE = {"domain": "www.themuse.com", "crawl_policy": {"include_paths": ["/advice"], "include_sitemaps": ["/sitemaps/advice.xml"]}}


def test_include_sitemaps_opens_only_the_listed_child_sitemaps():
    policy = PathPolicy(FakeOntology(ROWS + [MUSE]))
    assert policy.sitemaps_for("www.themuse.com") == ("/sitemaps/advice.xml",)
    assert policy.is_sitemap_allowed("www.themuse.com", "https://www.themuse.com/sitemaps/advice.xml")
    assert not policy.is_sitemap_allowed("www.themuse.com", "https://www.themuse.com/sitemaps/jobs-0.xml")
    assert not policy.is_sitemap_allowed("www.themuse.com", "https://www.themuse.com/sitemaps/companies.xml")


def test_without_include_sitemaps_every_sitemap_is_allowed():
    policy = PathPolicy(FakeOntology(ROWS + [MUSE]))
    assert policy.sitemaps_for("tealhq.com") == ()
    assert policy.is_sitemap_allowed("tealhq.com", "https://www.tealhq.com/sitemap-0.xml")
    assert policy.is_sitemap_allowed("unknown.example", "https://unknown.example/s.xml")


def test_include_sitemaps_does_not_change_page_rules():
    policy = PathPolicy(FakeOntology([MUSE]))
    assert not policy.is_excluded("www.themuse.com", "https://www.themuse.com/advice/a-post")
    assert policy.is_excluded("www.themuse.com", "https://www.themuse.com/jobs/x")
