from services.best_bets import best_bet_domains, merge_best_bets

MANIFEST = {"search_domains": [
    {"domain": "sujeeth.org"},
    {"domain": "app.candid.org", "manual_only": True},
    {"domain": "other.example", "manual_only": True},
]}


def hit(url, score=1.0, chunk="c"):
    return {"url": url, "title": url, "score": score, "chunk_id": chunk}


def test_only_selected_manual_domains_are_best_bet_domains():
    assert best_bet_domains(MANIFEST, ["sujeeth.org", "app.candid.org"]) == ["app.candid.org"]
    assert best_bet_domains(MANIFEST, ["sujeeth.org"]) == []
    assert best_bet_domains({}, ["app.candid.org"]) == []


def test_best_bets_come_first_one_per_url_and_capped_at_three():
    manual = [hit("https://a/1", chunk="1"), hit("https://a/1", chunk="2"), hit("https://a/2"),
              hit("https://a/3"), hit("https://a/4")]
    out = merge_best_bets([hit("https://s/1")], manual, max_best=3)
    assert [x["url"] for x in out] == ["https://a/1", "https://a/2", "https://a/3", "https://s/1"]
    assert [x["best_bet"] for x in out] == [True, True, True, False]
    assert out[0]["chunk_id"] == "1"  # the highest-ranked chunk for a URL is kept


def test_best_bet_url_is_removed_from_normal_results():
    out = merge_best_bets([hit("https://a/1"), hit("https://s/1")], [hit("https://A/1")])
    assert [x["url"] for x in out] == ["https://A/1", "https://s/1"]


def test_total_can_reach_eight_with_five_normal_and_three_best_bets():
    normal = [hit(f"https://s/{i}") for i in range(5)]
    manual = [hit(f"https://m/{i}") for i in range(6)]
    assert len(merge_best_bets(normal, manual)) == 8


def test_no_manual_results_leaves_normal_results_unchanged_apart_from_flag():
    out = merge_best_bets([hit("https://s/1")], [])
    assert out == [{**hit("https://s/1"), "best_bet": False}]
