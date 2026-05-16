"""Offline unit tests for Stage 6 visuals helpers."""
from produce import visuals


def test_parse_ts_formats():
    assert visuals._parse_ts("0:30") == 30.0
    assert visuals._parse_ts("1:30") == 90.0
    assert visuals._parse_ts("1:02:03") == 3723.0
    assert visuals._parse_ts(45) == 45.0
    assert visuals._parse_ts(None) is None
    assert visuals._parse_ts("") is None
    assert visuals._parse_ts("not-a-time") is None


def test_card_title_extracts_quoted_text():
    seg = {"visual": {"description": "arXiv paper 'Talk is Not Cheap' showing X"}}
    assert visuals._card_title(seg) == "Talk is Not Cheap"


def test_card_title_falls_back_to_description():
    seg = {"visual": {"description": "A plain description with no quotes here"}}
    assert visuals._card_title(seg) == "A plain description with no quotes here"


def test_card_title_default_when_empty():
    assert visuals._card_title({"visual": {}}) == "AI Daily Digest"
    assert visuals._card_title({}) == "AI Daily Digest"


def test_source_label_extracts_host():
    assert visuals._source_label(
        {"visual": {"url": "https://arxiv.org/abs/2511.123"}}) == "arxiv.org"
    assert visuals._source_label(
        {"source_url": "https://www.reddit.com/r/x/y"}) == "reddit.com"
    assert visuals._source_label({}) == ""


def test_wrap_breaks_long_text():
    # a fake "draw" whose textlength is proportional to character count
    class FakeDraw:
        def textlength(self, text, font=None):
            return len(text) * 10

    lines = visuals._wrap(FakeDraw(), "one two three four five", font=None,
                          max_width=80)   # ~8 chars per line
    assert len(lines) > 1
    assert all(len(ln) <= 12 for ln in lines)
    assert " ".join(lines).split() == ["one", "two", "three", "four", "five"]
