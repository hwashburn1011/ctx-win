"""Offline unit tests for Stage 4 synthesize helpers."""
from process import synthesize


def test_word_count_sums_segment_text():
    segs = [{"text": "one two three"}, {"text": "four five"}, {"text": ""}]
    assert synthesize._word_count(segs) == 5


def test_word_count_handles_missing_text():
    assert synthesize._word_count([{"visual": {}}, {"text": None}]) == 0


def test_compact_cluster_shape():
    cluster = {
        "topic": "Agents", "summary": "stuff happened", "importance": 8,
        "is_hype": False, "item_ids": ["a"],
        "items": [{"title": "T", "source": "hn", "url": "u",
                   "essence": "e", "claims": ["c1"], "topics": ["x"],
                   "novelty": "high"}],
    }
    c = synthesize._compact_cluster(cluster)
    assert c["topic"] == "Agents" and c["importance"] == 8
    item = c["items"][0]
    assert set(item) == {"title", "source", "url", "essence", "claims"}
    assert item["claims"] == ["c1"]
    # cluster-level item_ids and item-level novelty are not forwarded
    assert "item_ids" not in c and "novelty" not in item


def test_compact_cluster_defaults_claims_to_list():
    c = synthesize._compact_cluster({"topic": "T", "items": [{"title": "x"}]})
    assert c["items"][0]["claims"] == []
    assert c["is_hype"] is False
