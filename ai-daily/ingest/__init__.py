"""Stage 1 -- ingestion modules.

Each module exposes:
  * ``fetch(date, *, logger=None, config=None, limit=None)`` -> list[dict]
  * a ``__main__`` CLI entry point, e.g. ``python -m ingest.reddit --date ...``

All network access goes through ``common.http_get`` (retries + User-Agent).
No source requires authentication.
"""
