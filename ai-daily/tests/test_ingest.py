"""Offline unit tests for ingest helper logic -- no network, fast."""
import time

import common
from ingest import arxiv as arxiv_mod
from ingest import hackernews as hn
from ingest import youtube as yt

KEYWORDS = ["AI", "LLM", "GPT", "machine learning", "agent"]


def test_hn_matches_whole_word():
    assert hn._matches("New AI note takers blow facts", None, KEYWORDS) is True
    assert hn._matches("GPT-5 released today", None, KEYWORDS) is True
    assert hn._matches("Building an agent framework", None, KEYWORDS) is True


def test_hn_matches_rejects_substring_false_positives():
    # the keyword "AI" must not match inside "Air" / "Chairs"
    assert hn._matches("MacBook Air review", None, KEYWORDS) is False
    assert hn._matches("Chairs and tables", None, KEYWORDS) is False


def test_hn_matches_multiword_and_case_insensitive():
    assert hn._matches("A Machine Learning primer", None, KEYWORDS) is True
    assert hn._matches(None, "https://example.com/llm-guide", KEYWORDS) is True


def test_hn_matches_none_safe():
    assert hn._matches(None, None, KEYWORDS) is False


def test_arxiv_entry_dt_parses_struct_time():
    entry = {"published_parsed": time.strptime("2026-05-14T12:30:00",
                                               "%Y-%m-%dT%H:%M:%S")}
    dt = arxiv_mod._entry_dt(entry)
    assert (dt.year, dt.month, dt.day) == (2026, 5, 14)
    assert dt.tzinfo is not None                       # always tz-aware (UTC)


def test_arxiv_entry_dt_missing_returns_none():
    assert arxiv_mod._entry_dt({}) is None


def test_youtube_view_count_parsing():
    assert yt._view_count({"media_statistics": {"views": "29012"}}) == 29012
    assert yt._view_count({}) is None
    assert yt._view_count({"media_statistics": {"views": None}}) is None


def test_youtube_entry_dt():
    entry = {"published_parsed": time.strptime("2026-05-14T00:00:00",
                                               "%Y-%m-%dT%H:%M:%S")}
    assert yt._entry_dt(entry).day == 14
    assert yt._entry_dt({}) is None


def test_configs_load_and_have_expected_shape():
    chan = common.load_config("channels.yaml")
    assert isinstance(chan.get("channels"), list) and chan["channels"]

    subs = common.load_config("subreddits.yaml")
    assert isinstance(subs.get("subreddits"), list) and subs["subreddits"]
    assert isinstance(subs.get("min_score"), int)
    assert isinstance(subs.get("min_comments"), int)

    src = common.load_config("sources.yaml")
    for key in ("youtube", "hackernews", "arxiv"):
        assert key in src, f"sources.yaml missing '{key}' section"
    assert isinstance(src["hackernews"].get("keywords"), list)


def test_vtt_to_text_strips_tags_and_dedupes_rolling_captions():
    vtt = (
        "WEBVTT\nKind: captions\nLanguage: en\n\n"
        "00:00:00.080 --> 00:00:02.710 align:start position:0%\n"
        "This<00:00:00.320><c> is</c><00:00:00.640><c> a test</c>\n\n"
        "00:00:02.710 --> 00:00:02.720\n"
        "This is a test\n\n"                       # rolling repeat -- dropped
        "00:00:02.720 --> 00:00:05.000\n"
        "second line &amp; more\n"
    )
    assert yt._vtt_to_text(vtt) == "This is a test second line & more"


def test_vtt_to_text_empty():
    assert yt._vtt_to_text("WEBVTT\n\n") == ""


def test_rss_entry_text_strips_html_and_collapses_whitespace():
    from ingest import rss
    entry = {"summary": "<p>Hello   <b>world</b></p>\n<a href='x'>link</a>"}
    assert rss._entry_text(entry) == "Hello world link"


def test_rss_entry_text_prefers_full_content_over_summary():
    from ingest import rss
    entry = {"summary": "short summary",
             "content": [{"value": "<p>the full content</p>"}]}
    assert rss._entry_text(entry) == "the full content"
