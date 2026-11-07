"""blob_paths_report.py flags chunks under exclude_paths and outside include_paths.

Run from the websearch repo root: python -m pytest -q tests
"""
import json

import blob_paths_report as report


class Blob:
    def __init__(self, name):
        self.name = name


class Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return json.dumps(self.data).encode()


class Container:
    def __init__(self, docs):
        self.docs = docs

    def list_blobs(self, name_starts_with=""):
        return [Blob(n) for n in self.docs if n.startswith(name_starts_with)]

    def download_blob(self, name):
        return Download(self.docs[name])


def chunks(*urls, domain="indeed.com"):
    return {f"documents/d{i}.json": {"id": f"d{i}", "domain": domain, "url": u} for i, u in enumerate(urls)}


def test_load_rules_reads_both_lists(tmp_path):
    path = tmp_path / "ontology.json"
    path.write_text(json.dumps({"ontology_version": "9", "domains": [
        {"domain": "indeed.com", "crawl_policy": {"include_paths": ["/career-advice"], "exclude_paths": ["/career-advice/news"]}},
        {"domain": "tealhq.com", "crawl_policy": {"exclude_paths": ["/job"]}},
        {"domain": "sujeeth.org", "crawl_policy": {}},
    ]}))
    version, rules, includes, domains = report.load_rules(str(path))
    assert version == "9"
    assert rules == {"indeed.com": ("/career-advice/news",), "tealhq.com": ("/job",)}
    assert includes == {"indeed.com": ("/career-advice",)}
    assert domains == {"indeed.com", "tealhq.com", "sujeeth.org"}


def test_matching_rule_reports_the_exclude_rule_first_then_the_allow_list():
    includes, rules = ("/career-advice",), ("/career-advice/news",)
    assert report.matching_rule("https://www.indeed.com/career-advice/news/x", rules, includes) == "/career-advice/news"
    assert report.matching_rule("https://www.indeed.com/jobs", rules, includes) == "outside include_paths [/career-advice]"
    assert report.matching_rule("https://www.indeed.com/career-advice/interviewing", rules, includes) is None
    assert report.matching_rule("https://www.indeed.com/", rules, includes) is None


def test_scan_flags_chunks_outside_the_allow_list():
    container = Container(chunks(
        "https://www.indeed.com/", "https://www.indeed.com/career-advice/a", "https://www.indeed.com/jobs?q=x",
        "https://www.indeed.com/cmp/Indeed", "https://www.indeed.com/career-advice/news/y"))
    rules, includes = {"indeed.com": ("/career-advice/news",)}, {"indeed.com": ("/career-advice",)}
    result = report.scan(container, rules, includes=includes, progress=False)
    assert sum(result["flagged"]["indeed.com"].values()) == 3
    assert result["violations"]["indeed.com"]["outside include_paths [/career-advice]"] == 2
    assert result["violations"]["indeed.com"]["/career-advice/news"] == 1
    assert "https://www.indeed.com/jobs?q=x" in result["samples"]["indeed.com"]["outside include_paths [/career-advice]"]


def test_scan_without_an_allow_list_behaves_as_before():
    container = Container(chunks("https://www.tealhq.com/job/x", "https://www.tealhq.com/resume", domain="tealhq.com"))
    result = report.scan(container, {"tealhq.com": ("/job",)}, progress=False)
    assert result["violations"]["tealhq.com"] == {"/job": 1}


def test_report_passes_when_everything_is_in_scope_and_fails_otherwise(capsys):
    includes, rules = {"indeed.com": ("/career-advice",)}, {}
    clean = report.scan(Container(chunks("https://www.indeed.com/", "https://www.indeed.com/career-advice/a")), rules, includes=includes, progress=False)
    assert report.report("1", rules, includes, {"indeed.com"}, clean, 15, None) == 0
    dirty = report.scan(Container(chunks("https://www.indeed.com/jobs")), rules, includes=includes, progress=False)
    assert report.report("1", rules, includes, {"indeed.com"}, dirty, 15, None) == 1
    out = capsys.readouterr().out
    assert "OUT-OF-SCOPE BLOBS: 1" in out and "FAIL" in out and "outside include_paths [/career-advice]" in out
