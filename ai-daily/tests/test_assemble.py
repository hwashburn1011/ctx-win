"""Offline unit tests for Stage 7 assemble helpers."""
from pathlib import Path

from produce import assemble


def test_host_extracts_bare_host():
    assert assemble._host("https://arxiv.org/abs/2511.1") == "arxiv.org"
    assert assemble._host("http://www.reddit.com/r/x") == "reddit.com"
    assert assemble._host("https://news.ycombinator.com/item?id=1") \
        == "news.ycombinator.com"
    assert assemble._host(None) == ""
    assert assemble._host("") == ""


def test_concat_list_uses_posix_paths_and_file_directive():
    body = assemble._concat_list([Path("a/intro.mp4"), Path("a/seg_000.mp4")])
    lines = body.strip().splitlines()
    assert lines == ["file 'a/intro.mp4'", "file 'a/seg_000.mp4'"]
    # forward slashes even if Path rendered them natively
    assert "\\" not in body


def test_concat_list_empty():
    assert assemble._concat_list([]) == ""


def test_lower_third_overlay_is_transparent_above_the_bar():
    lt = {"enabled": True, "height": 56, "margin": 40, "font_size": 28}
    img = assemble._lower_third_overlay("arxiv.org", lt, 600, 400)
    assert img.mode == "RGBA"
    # the area above the bar must be fully transparent -- an opaque overlay
    # would black out the video clip it sits on
    assert img.getpixel((300, 50))[3] == 0
    # the bar itself (bottom-left) is drawn
    assert img.getpixel((20, 320))[3] > 0


def test_lower_third_overlay_empty_when_no_text():
    img = assemble._lower_third_overlay("", {"enabled": True}, 200, 200)
    assert img.getcolors() == [(200 * 200, (0, 0, 0, 0))]
