"""Offline unit tests for Stage 5 voice helpers."""
from produce import voice


def test_strip_cues_removes_visual_markers():
    assert voice._strip_cues("[VISUAL: a blog screenshot] Hello there") == "Hello there"
    assert voice._strip_cues("Plain narration text") == "Plain narration text"
    assert voice._strip_cues("mid [VISUAL: x] sentence") == "mid  sentence"


def test_strip_cues_handles_empty():
    assert voice._strip_cues(None) == ""
    assert voice._strip_cues("") == ""


def test_build_timing_tiles_with_gap():
    timing = voice._build_timing([10.0, 5.0, 7.0], gap=0.5)
    # each segment owns its trailing gap; last has none
    assert timing[0]["start"] == 0.0 and timing[0]["end"] == 10.5
    assert timing[1]["start"] == 10.5 and timing[1]["end"] == 16.0
    assert timing[2]["start"] == 16.0 and timing[2]["end"] == 23.0
    # end[i] == start[i+1]; last end == sum + 2 gaps
    assert timing[0]["end"] == timing[1]["start"]
    assert timing[1]["end"] == timing[2]["start"]


def test_build_timing_no_gap():
    timing = voice._build_timing([3.0, 4.0], gap=0)
    assert timing[0]["end"] == 3.0 and timing[1]["start"] == 3.0
    assert timing[1]["end"] == 7.0


def test_build_timing_preserves_speech_duration():
    timing = voice._build_timing([8.0, 2.0], gap=1.0)
    assert timing[0]["speech_duration"] == 8.0
    assert timing[1]["speech_duration"] == 2.0
    # the trailing gap is not counted as speech
    assert timing[0]["end"] - timing[0]["start"] == 9.0
