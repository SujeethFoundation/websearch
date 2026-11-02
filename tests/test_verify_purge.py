"""Tests for verify_purge.py (run from the websearch repo root)."""
import json

import verify_purge as vp

ONTOLOGY = {"ontology_version": "9.9.9", "domains": [
    {"domain": "tealhq.com", "crawl_policy": {"exclude_paths": ["/job", "/career-paths/*-certifications"]}},
    {"domain": "sujeeth.org", "crawl_policy": {}},
]}


class Blob:
    def __init__(self, name):
        self.name = name


class Download:
    def __init__(self, data):
        self.data = data

    def readall(self):
        return self.data


class Container:
    def __init__(self, blobs):
        self.blobs = blobs

    def list_blobs(self, name_starts_with=""):
        return [Blob(n) for n in sorted(self.blobs) if n.startswith(name_starts_with)]

    def download_blob(self, name):
        return Download(self.blobs[name])


def chunk(i, domain, url):
    return f"documents/{i}-0.json", json.dumps({"id": f"{i}-0", "domain": domain, "url": url}).encode()


def build(extra=()):
    blobs = dict([
        chunk(1, "sujeeth.org", "https://www.sujeeth.org/"),
        chunk(2, "tealhq.com", "https://www.tealhq.com/post/network"),
        chunk(3, "tealhq.com", "https://www.tealhq.com/career-paths/sales-interview-questions"),
        chunk(4, "other.org", "https://other.org/"),
    ])
    blobs["vectors/1-0.json"] = b"{}"
    blobs.update(dict(extra))
    return Container(blobs)


def write_ontology(tmp_path):
    path = tmp_path / "ontology.json"
    path.write_text(json.dumps(ONTOLOGY), encoding="utf-8")
    return str(path)


def test_a_clean_store_passes(tmp_path, capsys):
    assert vp.main(["--ontology", write_ontology(tmp_path)], container=build()) == 0
    out = capsys.readouterr().out
    assert "stored chunks under an excluded path   : 0   PASS" in out and "WARN" in out  # other.org is not in the ontology
    assert "everything checked is clean" in out


def test_leftover_excluded_chunks_fail_with_examples(tmp_path, capsys):
    extra = [chunk(5, "tealhq.com", "https://www.tealhq.com/job/retail-manager"),
             chunk(6, "tealhq.com", "https://www.tealhq.com/career-paths/aws-architect-certifications")]
    assert vp.main(["--ontology", write_ontology(tmp_path)], container=build(extra)) == 1
    out = capsys.readouterr().out
    assert "excluded path   : 2   FAIL" in out and "tealhq.com  /job: 1" in out and "/career-paths/*-certifications: 1" in out


def test_orphan_vectors_fail(tmp_path, capsys):
    assert vp.main(["--ontology", write_ontology(tmp_path)], container=build([("vectors/99-0.json", b"{}")])) == 1
    assert "vector blobs without a chunk blob      : 1   FAIL" in capsys.readouterr().out


class Resp:
    def __init__(self, docs):
        self.status_code, self._docs, self.text = 200, docs, ""

    def json(self):
        return {"value": self._docs}


class IndexSession:
    def __init__(self, docs):
        self.docs = docs

    def post(self, url, headers=None, data=None, timeout=None):
        body = json.loads(data)
        return Resp(self.docs[body["skip"]:body["skip"] + body["top"]])


def test_index_checks(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SEARCH_ADMIN_KEY", "k")
    docs = [{"id": "1-0", "url": "https://www.sujeeth.org/", "domain": "sujeeth.org"},
            {"id": "7-0", "url": "https://www.tealhq.com/job/x", "domain": "tealhq.com"},   # excluded, and no blob
            {"id": "2-0", "url": "https://www.tealhq.com/post/network", "domain": "tealhq.com"}]
    assert vp.main(["--ontology", write_ontology(tmp_path), "--check-index", "sf-knowledge-v2"], container=build(), session=IndexSession(docs)) == 1
    out = capsys.readouterr().out
    assert "excluded path: 1   FAIL" in out and "no chunk blob   : 1   FAIL" in out
    clean = [d for d in docs if d["id"] != "7-0"]
    assert vp.main(["--ontology", write_ontology(tmp_path), "--check-index", "sf-knowledge-v2"], container=build(), session=IndexSession(clean)) == 0
