"""Stage 4 -- synthesize the narration script.

Reads data/processed/<date>/clusters.json, asks the synthesis model to write a
~7-minute (900-1100 word) narration script that covers the top clusters with
[VISUAL: ...] cues, and writes data/processed/<date>/script.json.

CLI:  python -m process.synthesize --date 2026-05-14 --verbose
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

WORDS_PER_MINUTE = 150          # rough spoken pace, for the duration estimate


def _load_prompt() -> str:
    return (PROMPTS_DIR / "script.txt").read_text(encoding="utf-8")


def _compact_cluster(c: dict) -> dict:
    """Cluster shape the script model needs: topic, summary, member essences."""
    return {
        "topic": c.get("topic"),
        "summary": c.get("summary"),
        "importance": c.get("importance"),
        "is_hype": c.get("is_hype", False),
        "items": [{
            "title": it.get("title"),
            "source": it.get("source"),
            "url": it.get("url"),
            "essence": it.get("essence"),
            "claims": it.get("claims", []),
        } for it in c.get("items", [])],
    }


def _word_count(segments) -> int:
    return sum(len((s.get("text") or "").split()) for s in segments)


def run(date, *, logger=None, dry_run=False, force=False):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    clusters_path = day_dir / "clusters.json"
    out_path = day_dir / "script.json"

    if out_path.exists() and not force and not dry_run:
        log.info(f"[synthesize] {out_path} exists -- skipping (use --force)")
        return read_json(out_path)

    if not clusters_path.exists():
        log.error(f"[synthesize] {clusters_path} not found -- run cluster first")
        return None
    clusters = read_json(clusters_path).get("clusters", [])
    if not clusters:
        log.warning("[synthesize] no clusters to synthesize")
        return None
    log.info(f"[synthesize] writing a script from {len(clusters)} cluster(s)")

    cfg = load_config("llm.yaml")
    llm = LLM("synthesize", config=cfg, logger=log)
    prompt = _load_prompt().replace(
        "{{CLUSTERS_JSON}}",
        json.dumps([_compact_cluster(c) for c in clusters],
                   ensure_ascii=False, indent=2))

    log.info(f"[synthesize] calling {llm.describe()} ...")
    try:
        script = parse_json(llm.complete(prompt))
    except LLMError as e:
        log.error(f"[synthesize] synthesis failed: {e}")
        return None
    if not isinstance(script, dict) or not isinstance(script.get("segments"), list):
        log.error("[synthesize] model output is missing a 'segments' array")
        return None

    segments = script["segments"]
    words = _word_count(segments)
    script["date"] = f"{date:%Y-%m-%d}"
    script["word_count"] = words
    script["generated_at"] = now_iso()
    script["model"] = llm.describe()

    log.info(f"[synthesize] {len(segments)} segment(s), {words} words "
             f"(~{words / WORDS_PER_MINUTE:.1f} min spoken)")
    if not 800 <= words <= 1300:
        log.warning(f"[synthesize] word count {words} is well outside the "
                    "900-1100 target -- consider re-running with --force")

    if write_json(out_path, script, dry_run=dry_run, logger=log):
        log.info(f"[synthesize] wrote script -> {out_path}")
    return script


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 4: synthesize")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't write outputs")
    p.add_argument("--force", action="store_true",
                   help="re-synthesize even if script.json exists")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== synthesize for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
