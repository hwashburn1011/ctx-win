"""Shared utilities for the ai-daily pipeline.

Logging, date/window handling, filesystem paths, atomic JSON I/O, config
loading, an HTTP helper with retries, and a generic CLI runner for the
Stage-1 ingest modules all live here so the rest of the codebase stays thin.

NOTE: `common.py` is a small, intentional deviation from the layout in the
spec (which lists no shared module). `run.py` and every stage need the same
logging/paths/IO helpers; centralizing them here avoids `produce/` importing
from `ingest/`. See README -> "Design decisions".
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path

import yaml
from rich.logging import RichHandler

# --- Paths -----------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = PROJECT_ROOT / "config"
PROMPTS_DIR = CONFIG_DIR / "prompts"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
OUTPUT_DIR = DATA_DIR / "output"
LOGS_DIR = PROJECT_ROOT / "logs"

USER_AGENT = "ai-daily-digest/0.1 (local personal news pipeline)"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT}


# --- Logging ---------------------------------------------------------------
def setup_logging(verbose: bool = False) -> logging.Logger:
    """Configure rich console logging + a per-day file log under logs/."""
    # Windows consoles default to cp1252; emoji/non-Latin characters in source
    # titles would otherwise crash the console handler. Force UTF-8.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    level = logging.DEBUG if verbose else logging.INFO
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logfile = LOGS_DIR / f"{datetime.now(timezone.utc):%Y-%m-%d}.log"

    console = RichHandler(rich_tracebacks=True, show_path=False, markup=False,
                          log_time_format="%H:%M:%S")
    console.setLevel(level)

    fileh = logging.FileHandler(logfile, encoding="utf-8")
    fileh.setLevel(logging.DEBUG)  # the file always keeps the full detail
    fileh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(fileh)

    for noisy in ("urllib3", "requests", "yt_dlp", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("ai-daily")


def get_logger(name: str = "ai-daily") -> logging.Logger:
    return logging.getLogger(name)


# --- Dates / windows -------------------------------------------------------
def parse_date(s: str) -> date_cls:
    return datetime.strptime(s, "%Y-%m-%d").date()


def default_date() -> date_cls:
    """Yesterday (UTC) -- the digest covers the previous full day."""
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def date_window(d: date_cls) -> tuple[datetime, datetime]:
    """[start, end) UTC datetimes covering the calendar day ``d``."""
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def in_window(dt: datetime | None, d: date_cls) -> bool:
    """True if ``dt`` falls within the UTC calendar day ``d``."""
    if dt is None:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    start, end = date_window(d)
    return start <= dt < end


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Filesystem / IO -------------------------------------------------------
def raw_path(d: date_cls, source: str) -> Path:
    return RAW_DIR / f"{d:%Y-%m-%d}" / f"{source}.json"


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data, dry_run: bool = False, logger=None) -> bool:
    """Atomically write ``data`` as pretty JSON. Returns False on dry-run."""
    path = Path(path)
    log = logger or get_logger()
    if dry_run:
        log.info(f"[dry-run] skipped writing {path} "
                 f"({data.get('count', '?') if isinstance(data, dict) else '?'} items)")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    log.debug(f"wrote {path}")
    return True


def load_config(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def wrap_raw(source: str, d: date_cls, items: list) -> dict:
    """Standard envelope for a raw per-source dump."""
    return {
        "source": source,
        "date": f"{d:%Y-%m-%d}",
        "fetched_at": now_iso(),
        "count": len(items),
        "items": items,
    }


# --- HTTP ------------------------------------------------------------------
def http_get(url, params=None, headers=None, timeout=25, retries=3,
             backoff=2.0, logger=None):
    """GET with a sane User-Agent and retry/backoff.

    Retries network errors, timeouts, 429s and 5xx responses. A 4xx other
    than 429 will not recover, so it fails fast rather than burning retries.
    """
    import requests
    log = logger or get_logger()
    h = dict(DEFAULT_HEADERS)
    if headers:
        h.update(headers)
    last = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=h, timeout=timeout)
        except Exception as e:  # noqa: BLE001 -- network/timeout, retryable
            last = e
            log.warning(f"request error [{attempt}/{retries}] {url}: {e}")
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
        if resp.status_code in (429, 503):
            wait = backoff * attempt
            log.warning(f"HTTP {resp.status_code} from {url} -- retry in {wait:.0f}s")
            last = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(wait)
            continue
        if 400 <= resp.status_code < 500:
            resp.raise_for_status()  # client error -- retrying will not help
        if resp.status_code >= 500:
            last = RuntimeError(f"HTTP {resp.status_code}")
            log.warning(f"HTTP {resp.status_code} from {url} [{attempt}/{retries}]")
            if attempt < retries:
                time.sleep(backoff * attempt)
            continue
        return resp
    raise RuntimeError(f"GET failed after {retries} attempts: {url}") from last


def http_json(url, **kw):
    return http_get(url, **kw).json()


# --- Generic ingest CLI ----------------------------------------------------
def ingest_cli(source: str, fetch_fn):
    """Shared `python -m ingest.<source>` entry point for debugging."""
    p = argparse.ArgumentParser(description=f"ai-daily ingest: {source}")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't write output")
    p.add_argument("--force", action="store_true",
                   help="refetch even if output already exists")
    p.add_argument("--limit", type=int, help="cap items fetched (debugging)")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    d = parse_date(args.date) if args.date else default_date()
    out = raw_path(d, source)

    log.info(f"=== ingest:{source} for {d} ===")
    if out.exists() and not args.force and not args.dry_run:
        log.info(f"{out} already exists -- use --force to refetch. Loading it.")
        items = read_json(out).get("items", [])
    else:
        items = fetch_fn(d, logger=log, limit=args.limit)
        wrote = write_json(out, wrap_raw(source, d, items),
                           dry_run=args.dry_run, logger=log)
        if wrote:
            log.info(f"wrote {len(items)} items -> {out}")

    log.info(f"{source}: {len(items)} item(s) total")
    for it in items[:15]:
        title = (it.get("title") or "(untitled)")[:88]
        extra = it.get("score") or it.get("points") or it.get("view_count")
        log.info(f"  - {title}" + (f"  [{extra}]" if extra else ""))
    return items
