"""Tests for services/path_rules.py. Run from the websearch repo root: python -m pytest -q tests"""
import pytest

from services.path_rules import (
    PathPolicy,
    is_excluded_path,
    is_excluded_url,
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


def test_policy_allows_everything_if_the_ontology_never_answered():
    policy = PathPolicy(FakeOntology(fail=True))
    assert not policy.is_excluded("tealhq.com", "https://www.tealhq.com/job/x")
