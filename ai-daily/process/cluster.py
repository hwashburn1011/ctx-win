"""Stage 3 -- group extracted items into topic clusters.

Reads data/processed/<date>/extracted.json, passes the kept items to the
clustering model in a single call, and writes data/processed/<date>/
clusters.json with each cluster's member items embedded -- so Stage 4
(synthesize) has everything it needs without a second lookup.

CLI:  python -m process.cluster --date 2026-05-14 --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (PROCESSED_DIR, PROMPTS_DIR, default_date, get_logger,
                    load_config, now_iso, parse_date, read_json,
                    setup_logging, write_json)
from llm import LLM, LLMError, parse_json

# Fields from each extracted item that the clusterer needs (claims/etc. omitted
# to keep the single prompt compact).
CLUSTER_FIELDS = ("id", "title", "source", "url", "topics", "entities",
                  "essence", "novelty", "category", "developer_relevance")


def _load_prompt() -> str:
    return (PROMPTS_DIR / "cluster.txt").read_text(encoding="utf-8")


def _compact(ext: dict) -> dict:
    return {k: ext.get(k) for k in CLUSTER_FIELDS}


_RANK = {"high": 3, "medium": 2, "low": 1}


def _quality(item: dict) -> int:
    """Crude quality score from novelty + developer_relevance, for ranking."""
    return (_RANK.get((item.get("novelty") or "").lower(), 0)
            + _RANK.get((item.get("developer_relevance") or "").lower(), 0))


def _balance_sources(items, caps, log):
    """Cap over-represented sources (keeping their highest-quality items) so
    one source -- typically arXiv -- doesn't numerically dominate the digest."""
    if not caps:
        return items
    from collections import defaultdict
    grouped = defaultdict(list)
    for it in items:
        grouped[it.get("source")].append(it)
    kept = []
    for src, group in grouped.items():
        cap = caps.get(src)
        if cap and len(group) > cap:
            group = sorted(group, key=_quality, reverse=True)[:cap]
            log.info(f"[cluster] capped {src}: kept top {cap} by quality")
        kept.extend(group)
    return kept


def _assemble_clusters(raw_clusters, by_id, log):
    """Resolve model output into clusters with embedded items.

    Drops clusters whose ids don't resolve, sorts by importance (desc), and
    returns (clusters, set_of_placed_ids). Pure -- unit-tested offline.
    """
    clusters = []
    placed: set[str] = set()
    for c in raw_clusters:
        ids = [i for i in c.get("item_ids", []) if i in by_id]
        if not ids:
            log.warning(f"[cluster] dropping cluster with no resolvable items: "
                        f"'{c.get('topic')}'")
            continue
        placed.update(ids)
        clusters.append({
            "topic": c.get("topic", "(untitled)"),
            "summary": c.get("summary", ""),
            "importance": c.get("importance", 0),
            "is_hype": bool(c.get("is_hype", False)),
            "item_ids": ids,
            "items": [by_id[i] for i in ids],
        })
    clusters.sort(key=lambda c: c.get("importance", 0), reverse=True)
    return clusters, placed


def run(date, *, logger=None, dry_run=False, force=False, limit=None):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    extracted_path = day_dir / "extracted.json"
    out_path = day_dir / "clusters.json"

    if out_path.exists() and not force and not dry_run:
        log.info(f"[cluster] {out_path} exists -- skipping (use --force)")
        return read_json(out_path)

    if not extracted_path.exists():
        log.error(f"[cluster] {extracted_path} not found -- run extract first")
        return None
    items = read_json(extracted_path).get("items", [])
    if limit:
        items = items[:limit]
    if not items:
        log.warning("[cluster] no extracted items to cluster")
        return None

    cfg = load_config("llm.yaml")
    caps = (cfg.get("cluster") or {}).get("max_per_source") or {}
    items = _balance_sources(items, caps, log)
    log.info(f"[cluster] clustering {len(items)} extracted item(s)")

    by_id = {it.get("id"): it for it in items}
    llm = LLM("cluster", config=cfg, logger=log)
    prompt = _load_prompt().replace(
        "{{ITEMS_JSON}}",
        json.dumps([_compact(it) for it in items], ensure_ascii=False, indent=2))

    log.info(f"[cluster] calling {llm.describe()} ...")
    try:
        parsed = parse_json(llm.complete(prompt))
    except LLMError as e:
        log.error(f"[cluster] clustering failed: {e}")
        return None

    raw_clusters = parsed.get("clusters", []) if isinstance(parsed, dict) else []
    if not raw_clusters:
        log.error("[cluster] model returned no clusters")
        return None

    clusters, placed = _assemble_clusters(raw_clusters, by_id, log)
    log.info(f"[cluster] {len(clusters)} cluster(s); {len(placed)} item(s) "
             f"placed, {len(by_id) - len(placed)} left out")
    for c in clusters:
        flag = " [hype]" if c["is_hype"] else ""
        log.info(f"  ({c['importance']}) {c['topic']} -- "
                 f"{len(c['items'])} item(s){flag}")

    payload = {
        "date": f"{date:%Y-%m-%d}",
        "generated_at": now_iso(),
        "model": llm.describe(),
        "input_items": len(items),
        "clustered_items": len(placed),
        "cluster_count": len(clusters),
        "clusters": clusters,
    }
    if write_json(out_path, payload, dry_run=dry_run, logger=log):
        log.info(f"[cluster] wrote {len(clusters)} cluster(s) -> {out_path}")
    return payload


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 3: cluster")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't write outputs")
    p.add_argument("--force", action="store_true",
                   help="re-cluster even if clusters.json exists")
    p.add_argument("--limit", type=int, help="cap items fed to the clusterer")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== cluster for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force,
        limit=args.limit)


if __name__ == "__main__":
    main()
