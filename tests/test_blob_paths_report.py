"""blob_paths_report.py flags chunks under exclude_paths and outside include_paths.

Run from the websearch repo root: python -m pytest -q tests
"""
import json
import re

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


# ---- two-level (and deeper) sections ----

def test_path_section_takes_the_first_n_segments():
    url = "https://www.tealhq.com/Post/Some-Article/part-2?x=1#top"
    assert report.path_section(url, 1) == "post"
    assert report.path_section(url, 2) == "post/some-article"
    assert report.path_section(url, 3) == "post/some-article/part-2"
    assert report.path_section(url, 4) == "post/some-article/part-2"   # shorter paths are not padded
    assert report.path_section("https://www.tealhq.com/post", 2) == "post"
    assert report.path_section("https://www.tealhq.com/", 2) == "(home)"
    assert report.path_section("https://www.tealhq.com//a//b/", 2) == "a/b"
    assert report.first_segment(url) == "post"


def tealhq_chunks():
    urls = ["https://www.tealhq.com/", "https://www.tealhq.com/post", "https://www.tealhq.com/post/a", "https://www.tealhq.com/post/a/more"]
    urls += [f"https://www.tealhq.com/career-paths/role-{i}" for i in range(12)]
    urls += ["https://www.tealhq.com/tools/resume-checker", "https://www.tealhq.com/tools/cover-letter", "https://www.tealhq.com/privacy-policy"]
    return Container(chunks(*urls, domain="tealhq.com"))


def test_scan_depth_two_groups_by_two_segments():
    result = report.scan(tealhq_chunks(), {}, progress=False, depth=2)
    counter = result["chunks"]["tealhq.com"]
    assert counter["post"] == 1 and counter["post/a"] == 2     # /post/a and /post/a/more share a section
    assert counter["tools/resume-checker"] == 1 and counter["(home)"] == 1
    assert sum(1 for s in counter if s.startswith("career-paths/")) == 12


def test_scan_depth_one_is_unchanged():
    counter = report.scan(tealhq_chunks(), {}, progress=False, depth=1)["chunks"]["tealhq.com"]
    assert counter["post"] == 3 and counter["career-paths"] == 12 and counter["tools"] == 2


def test_wide_second_levels_collapse_into_one_row():
    result = report.scan(tealhq_chunks(), {"tealhq.com": ("/career-paths/role-3",)}, progress=False, depth=2)
    counter, flagged, pages = (result[k]["tealhq.com"] for k in ("chunks", "flagged", "pages"))
    g_chunks, g_flagged, g_pages = report.group_sections(counter, flagged, pages, depth=2, collapse=10)
    assert g_chunks["career-paths/*"] == 12 and g_flagged["career-paths/*"] == 1 and len(g_pages["career-paths/*"]) == 12
    assert g_chunks["post/a"] == 2 and g_chunks["tools/resume-checker"] == 1   # narrow ones stay separate
    assert not any(s.startswith("career-paths/role") for s in g_chunks)


def test_collapse_is_off_at_depth_one_or_when_zero():
    result = report.scan(tealhq_chunks(), {}, progress=False, depth=2)
    counter, flagged, pages = (result[k]["tealhq.com"] for k in ("chunks", "flagged", "pages"))
    assert report.group_sections(counter, flagged, pages, depth=1, collapse=10)[0] == counter
    assert report.group_sections(counter, flagged, pages, depth=2, collapse=0)[0] == counter


def test_report_at_depth_two_prints_collapsed_rows_but_csv_keeps_everything(capsys, tmp_path):
    result = report.scan(tealhq_chunks(), {}, progress=False, depth=2)
    csv_path = tmp_path / "paths.csv"
    assert report.report("1", {}, {}, {"tealhq.com"}, result, 50, str(csv_path), depth=2, collapse=10) == 0
    out = capsys.readouterr().out
    assert "first 2 path segments" in out and "career-paths/*" in out and "career-paths/role-3" not in out
    lines = csv_path.read_text().splitlines()
    assert lines[0].startswith("domain,section,chunks")
    assert any(line.startswith("tealhq.com,career-paths/role-3,") for line in lines)


def test_report_heading_at_depth_one_is_unchanged(capsys):
    result = report.scan(tealhq_chunks(), {}, progress=False, depth=1)
    report.report("1", {}, {}, {"tealhq.com"}, result, 15, None, depth=1)
    assert "first path segment, by chunks" in capsys.readouterr().out


# ---- defaults: two-level sections, top 10 ----

def test_defaults_are_two_levels_top_ten_no_collapsing():
    assert (report.DEFAULT_DEPTH, report.DEFAULT_TOP, report.DEFAULT_COLLAPSE) == (2, 10, 0)


def wide_site():
    urls = [f"https://www.tealhq.com/post/article-{i}" for i in range(8)]
    urls += [f"https://www.tealhq.com/career-paths/role-{i}" for i in range(30)]
    urls += [f"https://www.tealhq.com/career-paths/role-{i}/extra" for i in range(5)]   # same section as role-i
    urls += ["https://www.tealhq.com/", "https://www.tealhq.com/pricing"]
    return Container(chunks(*urls, domain="tealhq.com"))


def write_ontology(tmp_path, include=None):
    policy = {"include_paths": include} if include else {}
    path = tmp_path / "ontology.json"
    path.write_text(json.dumps({"ontology_version": "9", "domains": [{"domain": "tealhq.com", "crawl_policy": policy}]}))
    return str(path)


ROW = re.compile(r"^  (\S+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)", re.M)


def table_rows(out):
    """The section rows printed for the one domain in a report, in order."""
    block = out.split("by chunks)")[1].split("OUT-OF-SCOPE BLOBS")[0]
    return [m.group(1) for m in ROW.finditer(block)]


def test_running_with_no_options_lists_the_ten_largest_two_level_sections(capsys, tmp_path):
    code = report.main(["--ontology", write_ontology(tmp_path)], container=wide_site())
    out = capsys.readouterr().out
    rows = table_rows(out)
    assert code == 0
    assert "first 2 path segments, by chunks" in out
    assert len(rows) == 10
    assert rows[0].startswith("career-paths/role-") and all("/" in r for r in rows)     # two-level, biggest first
    assert "... 30 more sections with 30 chunks" in out                                  # 40 sections in all
    assert "career-paths/*" not in out


def test_out_of_scope_rows_do_not_break_the_top_ten(capsys, tmp_path):
    code = report.main(["--ontology", write_ontology(tmp_path, include=["/post"])], container=wide_site())
    out = capsys.readouterr().out
    assert code == 1                                                                     # pages sit outside /post
    assert len(table_rows(out)) == 10                                                    # not one row per out-of-scope page
    assert "of them out of scope" in out                                                 # the hidden ones are counted, not listed
    assert "outside include_paths [/post]" in out                                        # the violations list still names the rule


def test_the_old_one_level_view_is_one_flag_away(capsys, tmp_path):
    report.main(["--ontology", write_ontology(tmp_path), "--depth", "1", "--top", "15"], container=wide_site())
    out = capsys.readouterr().out
    assert "first path segment, by chunks" in out and "career-paths " in out and "career-paths/role" not in out
