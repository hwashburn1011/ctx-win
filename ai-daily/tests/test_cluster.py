"""Offline unit tests for Stage 3 cluster helpers."""
from common import get_logger
from process import cluster


def test_compact_picks_only_cluster_fields():
    ext = {"id": "x", "title": "T", "source": "hn", "url": "u",
           "topics": ["t"], "entities": ["E"], "essence": "e",
           "novelty": "high", "category": "tool", "developer_relevance": "high",
           "claims": ["dropped"], "extra": "dropped"}
    c = cluster._compact(ext)
    assert "claims" not in c and "extra" not in c
    assert c["essence"] == "e" and c["novelty"] == "high"
    assert set(c) == set(cluster.CLUSTER_FIELDS)


def test_assemble_clusters_resolves_sorts_and_drops_empty():
    by_id = {"a": {"id": "a", "title": "A"},
             "b": {"id": "b", "title": "B"},
             "c": {"id": "c", "title": "C"}}
    raw = [
        {"topic": "low", "item_ids": ["a"], "importance": 3},
        {"topic": "ghost", "item_ids": ["missing"], "importance": 9},
        {"topic": "high", "item_ids": ["b", "c"], "importance": 8,
         "is_hype": True},
    ]
    clusters, placed = cluster._assemble_clusters(raw, by_id, get_logger())

    # "ghost" dropped (no resolvable ids); rest sorted by importance desc
    assert [c["topic"] for c in clusters] == ["high", "low"]
    assert clusters[0]["is_hype"] is True
    assert [it["title"] for it in clusters[0]["items"]] == ["B", "C"]
    assert placed == {"a", "b", "c"}


def test_assemble_clusters_ignores_unknown_ids_within_a_cluster():
    by_id = {"a": {"id": "a", "title": "A"}}
    clusters, placed = cluster._assemble_clusters(
        [{"topic": "mix", "item_ids": ["a", "nope"], "importance": 5}],
        by_id, get_logger())
    assert len(clusters) == 1
    assert clusters[0]["item_ids"] == ["a"]
    assert placed == {"a"}


def test_quality_scores_novelty_and_relevance():
    assert cluster._quality({"novelty": "high", "developer_relevance": "high"}) == 6
    assert cluster._quality({"novelty": "low", "developer_relevance": "medium"}) == 3
    assert cluster._quality({}) == 0


def test_balance_sources_caps_and_keeps_best():
    items = [{"id": f"a{i}", "source": "arxiv", "novelty": "low",
              "developer_relevance": "low"} for i in range(8)]
    items.append({"id": "a-top", "source": "arxiv", "novelty": "high",
                  "developer_relevance": "high"})
    items += [{"id": f"r{i}", "source": "reddit"} for i in range(4)]

    out = cluster._balance_sources(items, {"arxiv": 3}, get_logger())
    arxiv = [i for i in out if i["source"] == "arxiv"]
    reddit = [i for i in out if i["source"] == "reddit"]
    assert len(arxiv) == 3                          # capped
    assert any(i["id"] == "a-top" for i in arxiv)   # highest-quality survives
    assert len(reddit) == 4                         # uncapped source untouched


def test_balance_sources_no_caps_is_passthrough():
    items = [{"id": "x", "source": "arxiv"}, {"id": "y", "source": "hn"}]
    assert cluster._balance_sources(items, {}, get_logger()) == items
