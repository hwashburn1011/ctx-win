"""Hacker News ingest via the public Algolia search API.

We pull every story above the points threshold in the target UTC day, keep
the ones whose title/url match the AI keyword list, and attach top-level
comments from the Algolia item endpoint.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import date_window, get_logger, http_json, ingest_cli, load_config

SOURCE = "hn"
SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"


def _matches(title: str | None, url: str | None, keywords: list[str]) -> bool:
    """Whole-word keyword match -- so "AI" does not match "Air"/"chair" etc."""
    hay = f"{title or ''} {url or ''}".lower()
    for k in keywords:
        if re.search(rf"(?<![a-z0-9]){re.escape(k.lower())}(?![a-z0-9])", hay):
            return True
    return False


def _top_comments(obj_id: str, limit: int, log) -> list[dict]:
    try:
        item = http_json(ITEM_URL.format(id=obj_id), logger=log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"HN item {obj_id} fetch failed: {e}")
        return []
    out: list[dict] = []
    for c in item.get("children", []) or []:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        out.append({"author": c.get("author"), "text": text})
        if len(out) >= limit:
            break
    return out


def fetch(date, *, logger=None, config=None, limit=None) -> list[dict]:
    log = logger or get_logger()
    cfg = (config or load_config("sources.yaml")).get("hackernews", {})
    min_points = cfg.get("min_points", 100)
    keywords = cfg.get("keywords", ["AI", "LLM", "Claude", "GPT", "Anthropic",
                                    "OpenAI", "agent", "model"])
    max_comments = cfg.get("max_comments", 15)

    start, end = date_window(date)
    nf = (f"created_at_i>{int(start.timestamp())},"
          f"created_at_i<{int(end.timestamp())},points>{min_points}")

    items: list[dict] = []
    page, pages = 0, 1
    scanned = 0
    while page < pages:
        data = http_json(SEARCH_URL, params={
            "tags": "story", "numericFilters": nf,
            "hitsPerPage": 100, "page": page}, logger=log)
        pages = data.get("nbPages", 1)
        hits = data.get("hits", [])
        scanned += len(hits)
        for hit in hits:
            title, url = hit.get("title"), hit.get("url")
            if not _matches(title, url, keywords):
                continue
            obj_id = hit.get("objectID")
            items.append({
                "id": f"hn_{obj_id}",
                "source": "hn",
                "title": title,
                "url": url or f"https://news.ycombinator.com/item?id={obj_id}",
                "hn_url": f"https://news.ycombinator.com/item?id={obj_id}",
                "points": hit.get("points"),
                "num_comments": hit.get("num_comments"),
                "author": hit.get("author"),
                "created_at": hit.get("created_at"),
                "story_text": (hit.get("story_text") or "").strip(),
                "top_comments": _top_comments(obj_id, max_comments, log),
            })
            if limit and len(items) >= limit:
                log.info(f"hn: hit --limit {limit}, stopping")
                return items
        page += 1
    log.info(f"hn: {len(items)} AI-related story/ies kept "
             f"(scanned {scanned} stories >{min_points} pts)")
    return items


if __name__ == "__main__":
    ingest_cli(SOURCE, fetch)
