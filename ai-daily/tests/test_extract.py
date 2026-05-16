"""Offline unit tests for Stage 2 helpers and LLM-output parsing."""
import pytest

from llm import LLMError, parse_json
from process import extract


# --- parse_json ------------------------------------------------------------
def test_parse_json_plain_object():
    assert parse_json('{"a": 1}') == {"a": 1}


def test_parse_json_plain_array():
    assert parse_json('[1, 2, 3]') == [1, 2, 3]


def test_parse_json_strips_markdown_fence():
    assert parse_json('```json\n{"ok": true}\n```') == {"ok": True}


def test_parse_json_tolerates_surrounding_prose():
    text = 'Here is the result:\n[{"id": "x"}]\nHope that helps!'
    assert parse_json(text) == [{"id": "x"}]


def test_parse_json_empty_raises():
    with pytest.raises(LLMError):
        parse_json("   ")


def test_parse_json_garbage_raises():
    with pytest.raises(LLMError):
        parse_json("not json at all")


# --- _keep (the spec relevance filter) -------------------------------------
def test_keep_drops_hype():
    assert extract._keep({"category": "hype", "developer_relevance": "high",
                           "novelty": "high"}) is False


def test_keep_drops_low_relevance_unless_high_novelty():
    assert extract._keep({"category": "news", "developer_relevance": "low",
                           "novelty": "medium"}) is False
    assert extract._keep({"category": "news", "developer_relevance": "low",
                           "novelty": "high"}) is True


def test_keep_keeps_relevant_items():
    assert extract._keep({"category": "model_release",
                           "developer_relevance": "high",
                           "novelty": "medium"}) is True


def test_keep_is_case_insensitive():
    assert extract._keep({"category": "HYPE", "developer_relevance": "HIGH",
                           "novelty": "HIGH"}) is False


# --- _compact --------------------------------------------------------------
def test_compact_reddit_truncates_and_keeps_comments():
    item = {"id": "reddit_x", "source": "reddit", "title": "T", "url": "u",
            "subreddit": "LocalLLaMA", "score": 200, "num_comments": 90,
            "selftext": "z" * 5000,
            "top_comments": [{"body": "b" * 800} for _ in range(20)]}
    c = extract._compact(item)
    assert len(c["selftext"]) == 3000
    assert len(c["top_comments"]) == 10
    assert all(len(x) <= 400 for x in c["top_comments"])


def test_compact_youtube_notes_missing_transcript():
    c = extract._compact({"id": "yt_x", "source": "youtube", "title": "T",
                          "url": "u", "transcript": None,
                          "transcript_status": "fetch_error"})
    assert "transcript" not in c
    assert "fetch_error" in c["note"]


def test_compact_arxiv_carries_abstract():
    c = extract._compact({"id": "arxiv_1", "source": "arxiv", "title": "T",
                          "url": "u", "abstract": "an abstract",
                          "authors": ["A", "B"]})
    assert c["abstract"] == "an abstract"
    assert c["authors"] == ["A", "B"]


def test_safe_id_sanitizes():
    assert extract._safe_id("arxiv_2505.123v1") == "arxiv_2505.123v1"
    assert extract._safe_id("a/b c") == "a_b_c"
