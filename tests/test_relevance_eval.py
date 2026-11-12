"""The relevance scorer: ranks, the summary, and a run against a stand-in search app."""
import importlib.util
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("relevance_eval", os.path.join(HERE, "relevance_eval.py"))
ev = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ev)


def test_the_rank_is_the_place_of_the_first_result_with_an_expected_piece_of_url():
    urls = ["https://www.themuse.com/a", "https://sujeeth.org/Circle", "https://sujeeth.org/circle#2"]
    assert ev.rank_of(["sujeeth.org/circle"], urls) == 2
    assert ev.rank_of(["nothing", "themuse.com/a"], urls) == 1
    assert ev.rank_of(["absent"], urls) is None and ev.rank_of([], urls) is None and ev.rank_of(["x"], []) is None


def test_the_summary_counts_places_and_averages_reciprocal_ranks():
    assert ev.summarize([1, 3, None, 5]) == {"questions": 4, "first": 1, "top3": 2, "top5": 3, "mrr": round((1 + 1 / 3 + 1 / 5) / 4, 3)}
    assert ev.summarize([]) == {"questions": 0, "first": 0, "top3": 0, "top5": 0, "mrr": 0.0}


def test_the_gold_file_is_valid_and_every_question_expects_something():
    gold = json.load(open(os.path.join(HERE, "relevance_gold.json"), encoding="utf-8"))
    assert len(gold) >= 5 and all(item["question"].strip() and item["expect"] for item in gold)


class App(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        App.seen.append((self.headers.get("x-functions-key"), body))
        tried = bool(body.get("ranking"))                       # the app ranks the right page higher with these settings
        if "broken" in body["query"]:
            data, code = json.dumps({"error": "boom"}).encode(), 502
        else:
            urls = ["https://sujeeth.org/circle", "https://x.org/b"] if tried else ["https://x.org/b", "https://x.org/c", "https://sujeeth.org/circle"]
            data, code = json.dumps({"status": "completed", "results": [{"url": u} for u in urls]}).encode(), 200
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_a_run_with_a_trial_column_against_a_stand_in_app(tmp_path, capsys):
    server = HTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"question": "what does a mentor do in week 4", "expect": ["sujeeth.org/circle"]},
                                {"question": "a broken one", "expect": ["x"]}]))
    try:
        code = ev.main(["--url", f"http://127.0.0.1:{server.server_port}", "--key", "k", "--gold", str(gold), "--try", "phrase_boost=0"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 0 and App.seen[0][0] == "k" and any(b.get("ranking") == {"phrase_boost": 0.0} for _, b in App.seen)
    assert " live: right page first 0/2   in the top 3 1/2" in out and "  try: right page first 1/2" in out
    assert "try ranks the right page higher than live on 1 question(s), lower on 0, the same on 1." in out
    assert "HTTP 502" in out and "did not complete normally" in out


def test_an_address_without_a_scheme_or_with_a_trailing_slash_is_made_usable():
    assert ev.normalize_url("host.net") == "https://host.net" and ev.normalize_url("host.net/") == "https://host.net"
    assert ev.normalize_url("  http://localhost:7071/ ") == "http://localhost:7071" and ev.normalize_url("") == ""
    assert ev.DEFAULT_URL.startswith("https://") and not ev.DEFAULT_URL.endswith("/")


def test_without_a_key_and_nobody_to_ask_it_says_so(capsys, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_KEY", raising=False)
    monkeypatch.setattr(ev.sys, "stdin", None)
    assert ev.main(["--url", "host.net"]) == 2 and "Function key" in capsys.readouterr().out


def test_the_key_is_asked_for_hidden_when_a_person_is_there(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_KEY", raising=False)

    class Tty:
        def isatty(self):
            return True
    monkeypatch.setattr(ev.sys, "stdin", Tty())
    asked = []
    monkeypatch.setattr(ev.getpass, "getpass", lambda prompt: asked.append(prompt) or "secret")
    sent = []
    monkeypatch.setattr(ev, "search", lambda url, key, q, limit=5, timeout=60, ranking=None: sent.append((url, key)) or (["https://sujeeth.org/circle"], ""))
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"question": "q", "expect": ["circle"]}]))
    assert ev.main(["--url", "host.net", "--gold", str(gold)]) == 0
    assert asked and sent[0] == ("https://host.net", "secret")


def test_a_wrong_key_stops_at_the_first_question_with_a_clear_message(tmp_path, capsys):
    class Rejecting(BaseHTTPRequestHandler):
        count = 0

        def log_message(self, *args):
            pass

        def do_POST(self):
            Rejecting.count += 1
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(401)
            self.send_header("content-length", "0")
            self.end_headers()
    server = HTTPServer(("127.0.0.1", 0), Rejecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"question": f"q{i}", "expect": ["x"]} for i in range(5)]))
    try:
        code = ev.main(["--url", f"http://127.0.0.1:{server.server_port}", "--key", "wrong", "--gold", str(gold)])
    finally:
        server.shutdown()
    assert code == 2 and "rejected the key" in capsys.readouterr().out and Rejecting.count == 1


def test_an_address_nothing_answers_at_is_reported_not_raised(tmp_path, capsys):
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"question": "q", "expect": ["x"]}]))
    code = ev.main(["--url", "127.0.0.1:9", "--key", "k", "--gold", str(gold)])
    assert code == 2 and "Could not reach the app" in capsys.readouterr().out
    urls, note = ev.search("not a url at all \x00", "k", "q")
    assert urls == [] and note.startswith("unreachable:")


EXPLANATION = {
    "question_concepts": {"career_mentor": 1.1, "week_4": 1.2}, "filter_concepts": ["a", "b", "c", "d", "e"], "filter_domains": ["x", "y"],
    "passing_filters": 7075, "candidates": 1032, "note": None,
    "concept_matches": [{"concept": "week_4", "source": "query_match", "confidence": 1.2}, {"concept": "networking", "source": "relationship", "confidence": None}],
    "answer": [{"title": "Getting the Most from Mentorship", "url": "https://sujeeth.org/career/Getting-the-Most-from-Mentorship/",
                "keyword_rank": 3, "vector_rank": 2, "concept_share": 0.324, "score": 0.0312}],
    "expected": {"pieces": ["sujeeth.org/circle"], "chunks_in_index": 8, "chunks_passing_filters": 8,
                 "best": [{"title": "Career Circle | The Sujeeth Foundation", "url": "https://sujeeth.org/circle", "position": 14, "keyword_rank": 6,
                           "vector_rank": None, "vector_rank_all": 212, "keyword_rank_all": 6, "concept_share": 0.353, "score": 0.0211}],
                 "filtered_out": []},
}


def test_an_explanation_reads_as_a_few_lines():
    text = "\n".join(ev.explain_lines(EXPLANATION))
    assert "the question's own concepts: career_mentor 1.1, week_4 1.2   added by relationship: networking" in text
    assert "7,075 chunks pass the filters (5 concepts, 2 domains); 1,032 entered the ranking" in text
    assert "8 chunks in the index, 8 pass the filters" in text and "place 14:" in text
    assert "vector - (place 212 of all, outside the nearest that count)" in text and "concept share 0.35" in text
    assert 'answer 1: "Getting the Most from Mentorship"' in text and "keyword 3, vector 2" in text


def test_an_explanation_says_plainly_when_the_page_is_missing_or_filtered_out():
    missing = dict(EXPLANATION, expected={"pieces": ["nowhere"], "chunks_in_index": 0, "chunks_passing_filters": 0, "best": [], "filtered_out": []})
    assert "NOT IN THE INDEX" in "\n".join(ev.explain_lines(missing))
    out = dict(EXPLANATION, expected={"pieces": ["x"], "chunks_in_index": 3, "chunks_passing_filters": 0, "best": [],
                                      "filtered_out": [{"url": "https://other.org/p", "tags": ["unrelated"], "why": "it carries none of the question's concepts"}]})
    text = "\n".join(ev.explain_lines(out))
    assert "THE FILTERS LEAVE IT OUT" in text and "tags [unrelated]: it carries none of the question's concepts" in text
    assert ev.explain_lines({"error": "HTTP 404: no such route"}) == ["    could not explain: HTTP 404: no such route"]


def test_explain_is_asked_only_for_questions_not_answered_first(tmp_path, capsys, monkeypatch):
    answers = {"first": ["https://a.org/right"], "second": ["https://a.org/wrong", "https://a.org/right"]}
    monkeypatch.setattr(ev, "search", lambda url, key, q, limit=5, timeout=60, ranking=None: (answers[q], ""))
    asked = []
    monkeypatch.setattr(ev, "explain", lambda url, key, q, expect, timeout=120, ranking=None: asked.append((q, list(expect))) or EXPLANATION)
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"question": "first", "expect": ["right"]}, {"question": "second", "expect": ["right"]}]))
    assert ev.main(["--url", "h", "--key", "k", "--gold", str(gold), "--explain"]) == 0
    out = capsys.readouterr().out
    assert asked == [("second", ["right"])] and "second   (right page at place 2)" in out and "place 14:" in out


def test_tried_settings_are_parsed_strictly():
    assert ev.parse_trial(["phrase_boost=0", "concept_boost=2.5", "max_per_page=1", "unique_pages=0"]) == {
        "phrase_boost": 0.0, "concept_boost": 2.5, "max_per_page": 1.0, "unique_pages": 0.0}
    assert ev.parse_trial(None) == {}
    for bad in (["phrase_boost"], ["unknown=1"], ["phrase_boost=lots"]):
        try:
            ev.parse_trial(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad} should be refused")


def test_a_trial_adds_a_column_run_with_those_settings(tmp_path, capsys, monkeypatch):
    calls = []

    def fake(url, key, q, limit=5, timeout=60, ranking=None):
        calls.append(ranking)
        return (["https://a.org/right"] if ranking else ["https://a.org/wrong", "https://a.org/right"]), ""
    monkeypatch.setattr(ev, "search", fake)
    explained = []
    monkeypatch.setattr(ev, "explain", lambda url, key, q, expect, timeout=120, ranking=None: explained.append(ranking) or EXPLANATION)
    gold = tmp_path / "g.json"
    gold.write_text(json.dumps([{"question": "q", "expect": ["right"]}]))
    assert ev.main(["--url", "h", "--key", "k", "--gold", str(gold), "--try", "phrase_boost=0"]) == 0
    out = capsys.readouterr().out
    assert {"phrase_boost": 0.0} in calls and "try = the app with phrase_boost=0 for this run only" in out
    assert "   try: right page first 1/1" in out and "try ranks the right page higher than live on 1 question(s)" in out
    assert ev.main(["--url", "h", "--key", "k", "--gold", str(gold), "--try", "bogus=1"]) == 2


def test_the_explanation_shows_the_ranking_settings_and_the_word_pairs():
    e = dict(EXPLANATION, settings={"concept_boost": 1.0, "phrase_boost": 1.0, "max_per_page": 2, "rescoring": False, "text_in_memory": True},
             phrases=[{"phrase": "students completed", "weight": 3.1}, {"phrase": "how many", "weight": 0.4}])
    e["answer"] = [dict(EXPLANATION["answer"][0], phrase_share=0.6, phrase_counted=True)]
    text = "\n".join(ev.explain_lines(e))
    assert "ranking: concept boost 1, phrase boost 1, at most 2 chunks per page, rescoring OFF" in text
    assert 'word pairs it looks for side by side: "students completed" 3.1, "how many" 0.4' in text and "word pairs 0.60" in text


def test_the_explanation_says_each_page_is_once_and_how_many_more_chunks_sit_behind_it():
    e = dict(EXPLANATION, settings={"concept_boost": 1.0, "phrase_boost": 1.0, "max_per_page": 2, "unique_pages": True, "rescoring": True, "text_in_memory": True})
    e["expected"] = dict(EXPLANATION["expected"], pages_in_index=1, chunks_in_index=9, chunks_passing_filters=9,
                         best=[dict(EXPLANATION["expected"]["best"][0], other_chunks=8)])
    text = "\n".join(ev.explain_lines(e))
    assert "ranking: concept boost 1, phrase boost 1, each page once, rescoring on" in text
    assert "the expected page: 1 page, 9 chunks in the index, 9 pass the filters; where each stands among the answer's pages:" in text
    assert "place 14:" in text and "(+8 more chunks of this page, behind it)" in text
    one = dict(e, expected=dict(e["expected"], best=[dict(e["expected"]["best"][0], other_chunks=1)]))
    assert "(+1 more chunk of this page, behind it)" in "\n".join(ev.explain_lines(one))
