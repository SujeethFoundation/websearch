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
        local = body.get("backend") == "local"
        if "broken" in body["query"]:
            data, code = json.dumps({"error": "boom"}).encode(), 502
        else:
            urls = ["https://sujeeth.org/circle", "https://x.org/b"] if local else ["https://x.org/b", "https://x.org/c", "https://sujeeth.org/circle"]
            data, code = json.dumps({"status": "completed", "backend": "local" if local else None,
                                     "results": [{"url": u} for u in urls]}).encode(), 200
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_a_comparison_run_against_a_stand_in_app(tmp_path, capsys):
    server = HTTPServer(("127.0.0.1", 0), App)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps([{"question": "what does a mentor do in week 4", "expect": ["sujeeth.org/circle"]},
                                {"question": "a broken one", "expect": ["x"]}]))
    try:
        code = ev.main(["--url", f"http://127.0.0.1:{server.server_port}", "--key", "k", "--gold", str(gold), "--compare"])
    finally:
        server.shutdown()
    out = capsys.readouterr().out
    assert code == 0 and App.seen[0][0] == "k" and {b.get("backend") for _, b in App.seen} == {"azure", "local"}
    assert " azure: right page first 0/2   in the top 3 1/2" in out and " local: right page first 1/2" in out
    assert "local ranks the right page higher than azure on 1 question(s), lower on 0, the same on 1." in out
    assert "HTTP 502" in out and "did not complete normally" in out


def test_it_says_what_is_missing_without_a_url(capsys, monkeypatch):
    monkeypatch.delenv("WEBSEARCH_URL", raising=False)
    assert ev.main([]) == 2 and "WEBSEARCH_URL" in capsys.readouterr().out
