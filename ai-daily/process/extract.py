"""Stage 2 -- per-item structured extraction.

Reads the raw ingest dumps for a date, sends the items to the extraction
model in batches (batching amortizes the LLM's fixed per-call overhead and
keeps token cost down), applies the spec's relevance filter, and writes
data/processed/<date>/extracted.json.

Per-item results are cached under data/processed/<date>/extract_cache/ so an
interrupted or re-run extraction reuses completed work rather than paying for
it again.

CLI:  python -m process.extract --date 2026-05-14 --limit 12 --verbose
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (PROCESSED_DIR, PROMPTS_DIR, default_date, get_logger,
                    load_config, now_iso, parse_date, raw_path, read_json,
                    setup_logging, write_json)
from llm import LLM, LLMError, parse_json

SOURCES = ["youtube", "reddit", "hn", "arxiv"]


def _load_prompt() -> str:
    return (PROMPTS_DIR / "extract.txt").read_text(encoding="utf-8")


def _compact(item: dict) -> dict:
    """Trim a raw item to just what the extraction model needs (caps cost)."""
    src = item.get("source")
    out = {"id": item.get("id"), "source": src,
           "title": item.get("title"), "url": item.get("url")}
    if src == "youtube":
        out["channel"] = item.get("channel")
        transcript = (item.get("transcript") or "")[:6000]
        if transcript:
            out["transcript"] = transcript
        else:
            out["note"] = f"transcript unavailable ({item.get('transcript_status')})"
    elif src == "reddit":
        out["subreddit"] = item.get("subreddit")
        out["score"] = item.get("score")
        out["num_comments"] = item.get("num_comments")
        out["selftext"] = (item.get("selftext") or "")[:3000]
        out["top_comments"] = [(c.get("body") or "")[:400]
                               for c in (item.get("top_comments") or [])[:10]]
    elif src == "hn":
        out["points"] = item.get("points")
        out["story_text"] = (item.get("story_text") or "")[:2000]
        out["top_comments"] = [(c.get("text") or "")[:400]
                               for c in (item.get("top_comments") or [])[:10]]
    elif src == "arxiv":
        out["authors"] = item.get("authors")
        out["abstract"] = item.get("abstract")
    return out


def _keep(ext: dict) -> bool:
    """Spec filter: drop hype; drop low developer-relevance unless high novelty."""
    cat = (ext.get("category") or "").lower()
    rel = (ext.get("developer_relevance") or "").lower()
    nov = (ext.get("novelty") or "").lower()
    if cat == "hype":
        return False
    if rel == "low" and nov != "high":
        return False
    return True


def _safe_id(item_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", item_id or "unknown")


def _load_raw_items(date, log) -> list[dict]:
    items: list[dict] = []
    for src in SOURCES:
        p = raw_path(date, src)
        if not p.exists():
            log.warning(f"[extract] no raw file for '{src}' ({p}) -- skipping")
            continue
        items.extend(read_json(p).get("items", []))
    return items


def run(date, *, logger=None, dry_run=False, force=False, limit=None):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    out_path = day_dir / "extracted.json"
    cache_dir = day_dir / "extract_cache"

    if out_path.exists() and not force and not dry_run:
        log.info(f"[extract] {out_path} exists -- skipping (use --force)")
        return read_json(out_path)

    # Collect raw items, dedupe by id, preserve source order.
    seen, items = set(), []
    for it in _load_raw_items(date, log):
        iid = it.get("id")
        if iid and iid not in seen:
            seen.add(iid)
            items.append(it)
    if limit:
        items = items[:limit]
    if not items:
        log.warning("[extract] no raw items found -- run the ingest stage first")
        return None
    log.info(f"[extract] {len(items)} unique raw item(s) to process")

    cfg = load_config("llm.yaml")
    batch_size = max(1, cfg.get("extract", {}).get("batch_size", 12))
    llm = LLM("extract", config=cfg, logger=log)
    prompt_tmpl = _load_prompt()

    # Reuse cached extractions; queue the rest.
    results: dict[str, dict] = {}
    pending: list[dict] = []
    for it in items:
        cf = cache_dir / f"{_safe_id(it['id'])}.json"
        if cf.exists() and not force:
            try:
                results[it["id"]] = read_json(cf)
                continue
            except Exception:  # noqa: BLE001 -- corrupt cache, just re-extract
                pass
        pending.append(it)

    n_batches = (len(pending) + batch_size - 1) // batch_size
    log.info(f"[extract] {len(results)} cached, {len(pending)} to extract "
             f"via {llm.describe()} in {n_batches} batch(es) of {batch_size}")

    for bi in range(n_batches):
        batch = pending[bi * batch_size:(bi + 1) * batch_size]
        prompt = prompt_tmpl.replace(
            "{{ITEMS_JSON}}",
            json.dumps([_compact(it) for it in batch], ensure_ascii=False, indent=2))
        log.info(f"[extract] batch {bi + 1}/{n_batches} ({len(batch)} items)...")
        try:
            parsed = parse_json(llm.complete(prompt))
        except LLMError as e:
            log.error(f"[extract] batch {bi + 1} failed: {e}")
            continue
        if not isinstance(parsed, list):
            log.error(f"[extract] batch {bi + 1}: expected a JSON array, got "
                      f"{type(parsed).__name__}")
            continue
        by_id = {p.get("id"): p for p in parsed if isinstance(p, dict)}
        for it in batch:
            ext = by_id.get(it["id"])
            if not ext:
                log.warning(f"[extract] model returned no result for {it['id']}")
                continue
            ext.setdefault("source", it.get("source"))
            ext.setdefault("url", it.get("url"))
            ext.setdefault("title", it.get("title"))
            results[it["id"]] = ext
            if not dry_run:
                write_json(cache_dir / f"{_safe_id(it['id'])}.json", ext, logger=log)

    extractions = [results[it["id"]] for it in items if it["id"] in results]
    kept = [e for e in extractions if _keep(e)]
    log.info(f"[extract] {len(extractions)} extracted, {len(kept)} kept after "
             f"filter ({len(extractions) - len(kept)} dropped as hype / "
             "low-relevance)")

    payload = {
        "date": f"{date:%Y-%m-%d}",
        "generated_at": now_iso(),
        "model": llm.describe(),
        "raw_items": len(items),
        "extracted": len(extractions),
        "kept": len(kept),
        "items": kept,
    }
    if write_json(out_path, payload, dry_run=dry_run, logger=log):
        log.info(f"[extract] wrote {len(kept)} kept item(s) -> {out_path}")
    return payload


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 2: extract")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't write outputs")
    p.add_argument("--force", action="store_true",
                   help="re-extract, ignoring cached results")
    p.add_argument("--limit", type=int, help="cap items processed (debugging)")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== extract for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force,
        limit=args.limit)


if __name__ == "__main__":
    main()
