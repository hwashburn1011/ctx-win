#!/usr/bin/env python3
"""ai-daily -- daily AI news digest pipeline orchestrator.

Runs the pipeline stages in order. Stage 1 (ingest) is implemented; stages
2-7 are scaffolded and will be filled in incrementally -- see README.

Usage:
    python run.py                                  # all stages, yesterday
    python run.py --date 2026-05-14 --stages ingest --verbose
    python run.py --stages ingest --force --dry-run

Each stage is idempotent: if its output already exists it is skipped unless
--force is passed.
"""
from __future__ import annotations

import argparse
import sys
import traceback

from common import (default_date, parse_date, raw_path, read_json,
                    setup_logging, wrap_raw, write_json)
from ingest import arxiv, hackernews, reddit, rss, youtube
from process import cluster as cluster_stage
from process import extract as extract_stage
from process import synthesize as synthesize_stage
from produce import assemble as assemble_stage
from produce import visuals as visuals_stage
from produce import voice as voice_stage

STAGES = ["ingest", "extract", "cluster", "synthesize", "voice",
          "visuals", "assemble"]

# (raw-file source key, fetch callable) -- file key differs from display name
# for hackernews/hn so it matches `python -m ingest.hackernews` output.
INGEST_SOURCES = [
    ("youtube", youtube.fetch),
    ("reddit", reddit.fetch),
    ("hn", hackernews.fetch),
    ("arxiv", arxiv.fetch),
    ("rss", rss.fetch),
]


def stage_ingest(date, log, dry_run, force):
    summary = {}
    for src, fetch_fn in INGEST_SOURCES:
        out = raw_path(date, src)
        if out.exists() and not force:
            log.info(f"[ingest:{src}] {out} exists -- skipping (use --force)")
            try:
                summary[src] = read_json(out).get("count", "?")
            except Exception:  # noqa: BLE001
                summary[src] = "?"
            continue
        log.info(f"[ingest:{src}] fetching {date} ...")
        try:
            items = fetch_fn(date, logger=log)
        except Exception as e:  # noqa: BLE001
            log.error(f"[ingest:{src}] FAILED: {e}")
            log.debug(traceback.format_exc())
            summary[src] = "ERROR"
            continue
        write_json(out, wrap_raw(src, date, items), dry_run=dry_run, logger=log)
        summary[src] = len(items)
    log.info("[ingest] summary: "
             + ", ".join(f"{k}={v}" for k, v in summary.items()))
    return True  # ingest is resilient per-source; never halts the pipeline


# Stages 2-7 each return their result, or None on failure. A None return
# halts the run -- every downstream stage depends on the previous one's output.
def stage_extract(date, log, dry_run, force):
    return extract_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


def stage_cluster(date, log, dry_run, force):
    return cluster_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


def stage_synthesize(date, log, dry_run, force):
    return synthesize_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


def stage_voice(date, log, dry_run, force):
    return voice_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


def stage_visuals(date, log, dry_run, force):
    return visuals_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


def stage_assemble(date, log, dry_run, force):
    return assemble_stage.run(date, logger=log, dry_run=dry_run, force=force) is not None


STAGE_FNS = {"ingest": stage_ingest, "extract": stage_extract,
             "cluster": stage_cluster, "synthesize": stage_synthesize,
             "voice": stage_voice, "visuals": stage_visuals,
             "assemble": stage_assemble}


def main():
    p = argparse.ArgumentParser(description="ai-daily pipeline orchestrator")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--stages", default="all",
                   help=f"comma-separated subset of: {','.join(STAGES)} "
                        "(default: all)")
    p.add_argument("--dry-run", action="store_true",
                   help="run stages but don't write outputs")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--force", action="store_true",
                   help="re-run stages even if output already exists")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()

    if args.stages == "all":
        stages = STAGES
    else:
        stages = [s.strip() for s in args.stages.split(",") if s.strip()]
        bad = [s for s in stages if s not in STAGES]
        if bad:
            log.error(f"unknown stage(s): {bad}. valid: {STAGES}")
            sys.exit(2)

    log.info(f"=== ai-daily run | date={date} | stages={stages} | "
             f"dry_run={args.dry_run} force={args.force} ===")
    for stage in stages:
        ok = STAGE_FNS[stage](date, log, args.dry_run, args.force)
        if ok is False and not args.dry_run:
            log.error(f"=== stage '{stage}' failed -- halting; downstream "
                      "stages depend on its output ===")
            sys.exit(1)
    log.info("=== run complete ===")


if __name__ == "__main__":
    main()
