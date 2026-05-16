"""RSS / blog ingest -- feeds of AI leaders and practitioners.

Each feed configured under `rss.feeds` in config/sources.yaml is parsed with
feedparser; entries published within the target UTC day are kept. No auth.
A feed that fails to load is logged and skipped -- it never breaks the run.
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser

from common import get_logger, in_window, ingest_cli, load_config

SOURCE = "rss"
_AGENT = "Mozilla/5.0 (ai-daily-digest; personal news pipeline)"
_TAG_RE = re.compile(r"<[^>]+>")


def _entry_dt(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def _entry_text(entry) -> str:
    """Plain-text body of a feed entry (full content if present, else summary)."""
    content = entry.get("content")
    raw = content[0].get("value", "") if content else entry.get("summary", "")
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", raw or "")).strip()


def fetch(date, *, logger=None, config=None, limit=None) -> list[dict]:
    log = logger or get_logger()
    feeds = (config or load_config("sources.yaml")).get("rss", {}).get("feeds", [])
    log.info(f"rss: {len(feeds)} feed(s)")
    items: list[dict] = []
    for feed in feeds:
        url = feed.get("url")
        name = feed.get("name") or url or "?"
        if not url:
            continue
        try:
            parsed = feedparser.parse(url, agent=_AGENT)
        except Exception as e:  # noqa: BLE001
            log.error(f"rss: {name}: parse failed: {e}")
            continue
        if parsed.bozo and not parsed.entries:
            log.warning(f"rss: {name}: feed error ({parsed.get('bozo_exception')})")
            continue
        kept = 0
        for e in parsed.entries:
            pub = _entry_dt(e)
            if not in_window(pub, date):
                continue
            link = e.get("link") or ""
            eid = e.get("id") or link or e.get("title", "")
            items.append({
                "id": "rss_" + hashlib.sha1(eid.encode("utf-8")).hexdigest()[:12],
                "source": "rss",
                "title": e.get("title"),
                "url": link,
                "feed": name,
                "author": e.get("author"),
                "published_at": pub.isoformat() if pub else None,
                "content": _entry_text(e)[:5000],
            })
            kept += 1
            if limit and len(items) >= limit:
                log.info(f"rss: hit --limit {limit}, stopping")
                return items
        log.info(f"rss: {name}: kept {kept}/{len(parsed.entries)} entry/ies")
    return items


if __name__ == "__main__":
    ingest_cli(SOURCE, fetch)
