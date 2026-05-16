"""Reddit ingest -- public JSON API (append .json, no auth).

For each configured subreddit we pull ``top.json?t=day`` (which is itself the
"last 24 hours" view), keep posts that clear the score/comment thresholds, and
attach the top-level comments for each kept post.

Note: unlike the other sources, Reddit is not filtered to an exact UTC
calendar day -- ``t=day`` is Reddit's own rolling 24h window and the spec asks
for exactly that. Each post's ``created_at`` is still recorded.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

# Make `import common` work whether run as `python -m ingest.reddit`,
# `python ingest/reddit.py`, or imported by run.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import get_logger, http_json, ingest_cli, load_config

SOURCE = "reddit"
LISTING_URL = "https://www.reddit.com/r/{sub}/top.json"


def _top_comments(permalink: str, limit: int, log) -> list[dict]:
    """Fetch top-level comments for a post via its permalink + .json."""
    url = "https://www.reddit.com" + permalink.rstrip("/") + ".json"
    try:
        data = http_json(url, params={"limit": limit, "sort": "top", "depth": 1},
                          logger=log)
    except Exception as e:  # noqa: BLE001
        log.warning(f"comment fetch failed ({permalink}): {e}")
        return []
    out: list[dict] = []
    if not isinstance(data, list) or len(data) < 2:
        return out
    for child in data[1].get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        c = child.get("data", {})
        body = (c.get("body") or "").strip()
        if not body or body in ("[deleted]", "[removed]"):
            continue
        out.append({"author": c.get("author"), "score": c.get("score"),
                    "body": body})
        if len(out) >= limit:
            break
    return out


def fetch(date, *, logger=None, config=None, limit=None) -> list[dict]:
    log = logger or get_logger()
    cfg = config or load_config("subreddits.yaml")
    subs = cfg.get("subreddits", [])
    min_score = cfg.get("min_score", 100)
    min_comments = cfg.get("min_comments", 50)
    comment_limit = cfg.get("top_comments", 20)

    log.info(f"reddit: {len(subs)} subreddit(s) | min_score={min_score} "
             f"min_comments={min_comments}")
    items: list[dict] = []
    for sub in subs:
        try:
            data = http_json(LISTING_URL.format(sub=sub),
                             params={"t": "day", "limit": 100}, logger=log)
        except Exception as e:  # noqa: BLE001
            log.error(f"r/{sub}: listing fetch failed: {e}")
            continue
        posts = data.get("data", {}).get("children", [])
        kept = 0
        for child in posts:
            p = child.get("data", {})
            created = datetime.fromtimestamp(p.get("created_utc", 0),
                                             tz=timezone.utc)
            score = p.get("score", 0)
            ncom = p.get("num_comments", 0)
            if score < min_score or ncom < min_comments:
                continue
            permalink = p.get("permalink", "")
            items.append({
                "id": f"reddit_{p.get('id')}",
                "source": "reddit",
                "title": p.get("title"),
                "subreddit": sub,
                "url": "https://www.reddit.com" + permalink,
                "external_url": p.get("url"),
                "score": score,
                "num_comments": ncom,
                "created_at": created.isoformat(),
                "author": p.get("author"),
                "selftext": (p.get("selftext") or "").strip(),
                "top_comments": _top_comments(permalink, comment_limit, log),
            })
            kept += 1
            time.sleep(1.0)  # stay well under Reddit's unauthenticated rate cap
            if limit and len(items) >= limit:
                log.info(f"r/{sub}: hit --limit {limit}, stopping")
                return items
        log.info(f"r/{sub}: kept {kept}/{len(posts)} post(s)")
    return items


if __name__ == "__main__":
    ingest_cli(SOURCE, fetch)
