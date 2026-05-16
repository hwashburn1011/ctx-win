"""Offline unit tests for common.py -- no network, fast."""
from datetime import date, datetime, timedelta, timezone

import common


def test_parse_date():
    assert common.parse_date("2026-05-14") == date(2026, 5, 14)


def test_date_window_is_utc_calendar_day():
    start, end = common.date_window(date(2026, 5, 14))
    assert start == datetime(2026, 5, 14, tzinfo=timezone.utc)
    assert end == datetime(2026, 5, 15, tzinfo=timezone.utc)
    assert end - start == timedelta(days=1)


def test_in_window_boundaries():
    d = date(2026, 5, 14)
    start, end = common.date_window(d)
    assert common.in_window(start, d) is True                    # inclusive
    assert common.in_window(end, d) is False                     # exclusive
    assert common.in_window(end - timedelta(seconds=1), d) is True
    assert common.in_window(start - timedelta(seconds=1), d) is False


def test_in_window_naive_datetime_treated_as_utc():
    assert common.in_window(datetime(2026, 5, 14, 12), date(2026, 5, 14)) is True


def test_in_window_none_is_false():
    assert common.in_window(None, date(2026, 5, 14)) is False


def test_default_date_is_yesterday_utc():
    today = datetime.now(timezone.utc).date()
    assert common.default_date() == today - timedelta(days=1)


def test_wrap_raw_envelope():
    env = common.wrap_raw("reddit", date(2026, 5, 14), [{"a": 1}, {"b": 2}])
    assert env["source"] == "reddit"
    assert env["date"] == "2026-05-14"
    assert env["count"] == 2
    assert env["items"] == [{"a": 1}, {"b": 2}]
    assert "fetched_at" in env


def test_write_read_json_roundtrip_with_unicode(tmp_path):
    p = tmp_path / "sub" / "x.json"          # parent dir does not exist yet
    data = {"items": ["unicode: cafe — \U0001f916"], "count": 1}
    assert common.write_json(p, data) is True
    assert common.read_json(p) == data


def test_write_json_dry_run_writes_nothing(tmp_path):
    p = tmp_path / "x.json"
    assert common.write_json(p, {"count": 0}, dry_run=True) is False
    assert not p.exists()


def test_write_json_atomic_leaves_no_tmp_files(tmp_path):
    common.write_json(tmp_path / "x.json", {"count": 0})
    assert [f for f in tmp_path.iterdir() if f.suffix == ".tmp"] == []
