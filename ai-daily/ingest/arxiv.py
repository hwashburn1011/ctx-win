"""arXiv ingest via the public Atom API (export.arxiv.org).

Pages through the most-recent submissions across the configured categories
(newest-first) and keeps every paper submitted within the target UTC day.
Because cs.AI/cs.CL/cs.LG together produce hundreds of submissions per day,
a single 100-result request is not enough -- we paginate until we walk past
the start of the window.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser

from common import (date_window, get_logger, http_get, in_window, ingest_cli,
                    load_config)

SOURCE = "arxiv"
API_URL = "http://export.arxiv.org/api/query"


def _entry_dt(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def fetch(date, *, logger=None, config=None, limit=None) -> list[dict]:
    log = logger or get_logger()
    cfg = (config or load_config("sources.yaml")).get("arxiv", {})
    categories = cfg.get("categories", ["cs.AI", "cs.CL", "cs.LG"])
    page_size = cfg.get("page_size", 100)
    max_pages = cfg.get("max_pages", 20)

    # Spaces (not '+') so requests URL-encodes correctly; arXiv treats '+' as ' '.
    query = " OR ".join(f"cat:{c}" for c in categories)
    window_start, _ = date_window(date)

    items: list[dict] = []
    seen: set[str] = set()
    scanned = 0
    for page in range(max_pages):
        # export.arxiv.org is frequently slow -- give it a generous timeout.
        try:
            resp = http_get(API_URL, params={
                "search_query": query, "sortBy": "submittedDate",
                "sortOrder": "descending", "start": page * page_size,
                "max_results": page_size}, timeout=60, logger=log)
        except Exception as e:  # noqa: BLE001
            log.warning(f"arxiv: page {page + 1} fetch failed ({e}) -- "
                        f"stopping with {len(items)} item(s) collected so far")
            break
        feed = feedparser.parse(resp.text)
        if not feed.entries:
            break
        scanned += len(feed.entries)
        walked_past_window = False
        for e in feed.entries:
            pub = _entry_dt(e)
            if pub and pub < window_start:
                walked_past_window = True  # entries are newest-first
                break
            if not in_window(pub, date):
                continue  # too new -- keep looking
            arxiv_id = (e.get("id") or "").rsplit("/", 1)[-1]
            if arxiv_id in seen:
                continue
            seen.add(arxiv_id)
            items.append({
                "id": f"arxiv_{arxiv_id}",
                "source": "arxiv",
                "title": " ".join((e.get("title") or "").split()),
                "url": e.get("link"),
                "published_at": pub.isoformat() if pub else None,
                "authors": [a.get("name") for a in e.get("authors", [])],
                "primary_category": (e.get("arxiv_primary_category") or {}).get("term"),
                "abstract": " ".join((e.get("summary") or "").split()),
            })
            if limit and len(items) >= limit:
                log.info(f"arxiv: hit --limit {limit}, stopping")
                return items
        if walked_past_window:
            break
        time.sleep(3)  # arXiv API politeness between page requests

    log.info(f"arxiv: {len(items)} submission(s) within {date} "
             f"(scanned {scanned} recent entries in {categories})")
    if not items and scanned:
        log.info("arxiv: 0 in window -- arXiv does not announce on weekends; "
                 "try an adjacent weekday with --date.")
    return items


if __name__ == "__main__":
    ingest_cli(SOURCE, fetch)
