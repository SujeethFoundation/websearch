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


# -- the relevance check -------------------------------------------------------------------------------------------

from services.best_bets import dropped_best_bets, top_pages  # noqa: E402


def test_a_manual_page_the_main_search_does_not_rank_is_not_a_best_bet():
    main = [hit(f"https://s/{i}") for i in range(8)]
    manual = [hit("https://m/unrelated"), hit("https://s/2")]
    out = merge_best_bets(main, manual, max_best=3, within_rank=5)
    assert [x["url"] for x in out if x["best_bet"]] == ["https://s/2"]
    assert out[0]["url"] == "https://s/2"                 # promoted to the front, once
    assert [x["url"] for x in out].count("https://s/2") == 1
    assert "https://m/unrelated" not in [x["url"] for x in out]


def test_the_check_counts_distinct_pages_not_chunks():
    main = [hit("https://s/1", chunk="a"), hit("https://s/1", chunk="b"), hit("https://s/2"), hit("https://s/3")]
    assert top_pages(main, 2) == {"s/1", "s/2"} or len(top_pages(main, 2)) == 2
    manual = [hit("https://s/3")]
    assert [x["best_bet"] for x in merge_best_bets(main, manual, within_rank=2)][0] is False   # third page: outside
    assert merge_best_bets(main, manual, within_rank=3)[0]["best_bet"] is True                 # inside


def test_rank_zero_keeps_every_manual_page_as_before():
    main = [hit(f"https://s/{i}") for i in range(8)]
    manual = [hit("https://m/1"), hit("https://m/2")]
    out = merge_best_bets(main, manual, max_best=3, within_rank=0)
    assert [x["url"] for x in out[:2]] == ["https://m/1", "https://m/2"] and dropped_best_bets(main, manual, 0) == 0


def test_the_check_reports_how_many_manual_pages_it_left_out():
    main = [hit(f"https://s/{i}") for i in range(8)]
    manual = [hit("https://m/1"), hit("https://m/2"), hit("https://s/1")]
    assert dropped_best_bets(main, manual, 5) == 2


def test_no_manual_page_ranks_when_the_main_search_found_nothing():
    assert merge_best_bets([], [hit("https://m/1")], within_rank=5) == []
