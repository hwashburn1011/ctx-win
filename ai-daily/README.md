# ai-daily

A local, free, open-source pipeline that produces one ~15-minute narrated
video each day summarizing the most important AI news from the past 24 hours,
organized into themed sections (Security, Development, New Tools, …). Audio
narration plays over screen captures of the actual source material — Reddit
threads, HN posts, blog posts, YouTube clips. No avatar, no talking head.

Built for one reader: a developer who builds agentic workflows and wants
practical signal (new tools, model releases, agentic patterns, real
benchmarks) over hype.

> **Build status — complete.** All seven stages are implemented and runnable
> end to end: `python run.py` takes the day from raw sources to a finished
> 1920×1080 mp4. Each stage remains independently runnable for debugging.

## Pipeline stages

| # | Stage      | Module(s)                    | Status |
|---|------------|------------------------------|--------|
| 1 | ingest     | `ingest/*`                   | ✅ implemented |
| 2 | extract    | `process/extract.py`         | ✅ implemented |
| 3 | cluster    | `process/cluster.py`         | ✅ implemented |
| 4 | synthesize | `process/synthesize.py`      | ✅ implemented |
| 5 | voice      | `produce/voice.py`           | ✅ implemented |
| 6 | visuals    | `produce/visuals.py`         | ✅ implemented |
| 7 | assemble   | `produce/assemble.py`        | ✅ implemented |

## Setup

Requires **Python 3.11+**, **ffmpeg** on `PATH` (Stages 6–7 and the YouTube
whisper fallback), and the **Claude Code CLI** (`claude`) on `PATH` and logged
in — that is the LLM backend for Stages 2–4 (no API key needed). Verify with
`claude --version`.

```powershell
cd ai-daily
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # Windows PowerShell
# source .venv/bin/activate           # macOS/Linux

# Full install (everything, ~large -- faster-whisper/playwright/kokoro):
pip install -r requirements.txt

# OR a lighter Stage-1-only install:
pip install requests==2.32.3 feedparser==6.0.11 PyYAML==6.0.2 `
            python-dotenv==1.0.1 rich==13.9.4 yt-dlp==2025.2.19

cp .env.example .env                  # only needed for Stages 2+
```

**Stage 5 (voice)** needs the Kokoro ONNX model files. Download them once into
`models/` (paths are set in `config/voice.yaml`):

```powershell
mkdir models
$base = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
curl -L -o models/kokoro-v1.0.onnx  "$base/kokoro-v1.0.onnx"   # ~325 MB
curl -L -o models/voices-v1.0.bin   "$base/voices-v1.0.bin"    # ~28 MB
```

**Stage 6 (visuals)** needs the Chromium browser for Playwright — installed
once with `playwright install chromium` (~150 MB).

## Usage

Run the full pipeline — raw sources to finished mp4:

```powershell
python run.py --date 2026-05-14 --verbose
python run.py --stages extract,cluster,synthesize --force   # re-run a subset
python run.py --stages ingest --dry-run                     # fetch, don't write
```

`run.py` flags: `--date YYYY-MM-DD` (default: yesterday, UTC), `--stages`
(comma-separated subset, default `all`), `--dry-run`, `--verbose`, `--force`.
Stages are idempotent — existing output is skipped unless `--force`.

Every stage is independently runnable for debugging:

```powershell
python -m ingest.reddit      --date 2026-05-14 --verbose
python -m ingest.hackernews  --date 2026-05-14 --verbose
python -m ingest.arxiv       --date 2026-05-14 --verbose
python -m ingest.youtube     --date 2026-05-14 --verbose --limit 5
python -m process.extract    --date 2026-05-14 --verbose --limit 30
python -m process.cluster    --date 2026-05-14 --verbose
python -m process.synthesize --date 2026-05-14 --verbose
python -m produce.voice      --date 2026-05-14 --verbose
python -m produce.visuals    --date 2026-05-14 --verbose
python -m produce.assemble   --date 2026-05-14 --verbose
```

Per-module flags: `--date`, `--verbose`, `--dry-run`, `--force`, `--limit N`.
`extract`'s `--force` also bypasses the per-item extraction cache.

## Output layout

Raw data is written to `data/raw/YYYY-MM-DD/<source>.json` as an envelope:

```json
{ "source": "reddit", "date": "2026-05-14", "fetched_at": "...",
  "count": 12, "items": [ /* list of dicts */ ] }
```

`reddit.fetch(date)` etc. return the bare `items` list; the envelope is added
on write. Logs go to `logs/YYYY-MM-DD.log` (full detail) and the console
(level controlled by `--verbose`).

Stage 2 writes `data/processed/YYYY-MM-DD/extracted.json` — the filtered
structured items that Stage 3 consumes — plus a per-item cache under
`data/processed/YYYY-MM-DD/extract_cache/` so re-runs reuse completed work.
Stage 3 writes `data/processed/YYYY-MM-DD/clusters.json` — topic clusters,
ordered by importance, each with its member items embedded.
Stage 4 writes `data/processed/YYYY-MM-DD/script.json` — the narration script
as ordered segments, each with its `[VISUAL: …]` cue and source URL.
Stage 5 writes `data/processed/YYYY-MM-DD/audio/` — one `segment_NNN.wav` per
segment, a combined `narration.wav`, and `timing.json` (each segment's
start/end offset in the combined track, for syncing visuals in Stage 7).
Stage 6 writes `data/processed/YYYY-MM-DD/visuals/` — one 1920×1080
`segment_NNN.png`/`.mp4` per segment plus `visuals.json` (file + kind).
Stage 7 writes the deliverable: `data/output/YYYY-MM-DD.mp4` — 1920×1080,
H.264 + AAC, intro/outro cards and a burned-in source lower third.

## Tests

Offline unit tests cover the pure logic — date windows and the 24h boundary,
atomic JSON I/O, the Hacker News keyword matcher, feed-date parsing, and
config shape. No network required, runs in well under a second:

```powershell
pip install pytest==8.3.4
python -m pytest -q
```

## Scheduling (cron)

Run once daily after the day has fully closed (UTC). Example crontab entry —
4:30am local, processing the previous day:

```cron
30 4 * * *  cd /path/to/ai-daily && .venv/bin/python run.py >> logs/cron.log 2>&1
```

On Windows, use Task Scheduler to run `.venv\Scripts\python.exe run.py`.

## Configuration

Everything tunable lives in `config/` — nothing source-related is hardcoded.

- `config/channels.yaml` — YouTube channels (by `id:` or `@handle:`).
- `config/subreddits.yaml` — subreddit list + score/comment thresholds.
- `config/sources.yaml` — YouTube/HN/arXiv settings, and the blog/RSS feeds.
- `config/sections.yaml` — the themed sections the digest is organized into.
- `config/llm.yaml` — LLM backend, role→model map, extraction batch size.
- `config/voice.yaml` — Kokoro voice, speed, model file paths.
- `config/visuals.yaml` — frame size, screenshot timeout, clip length, card style.
- `config/assemble.yaml` — fps, intro/outro length, codecs, lower-third style.
- `config/prompts/{extract,cluster,script}.txt` — LLM prompts (Stages 2–4).

## Design decisions & defaults

These were chosen to keep Stage 1 free, auth-free, and reproducible. Change
them in `config/` if you disagree.

- **24-hour window = the UTC calendar day** of `--date`. With the default
  (`yesterday`), a digest covers one complete prior day. Reproducible and
  consistent across all four sources.
- **YouTube uploads via RSS feeds**, not the YouTube Data API — the Data API
  needs a key/quota. The RSS feed exposes the ~15 most recent uploads per
  channel with publish timestamps, which is plenty for a 24h window.
- **Channel handles resolved at runtime** by scraping `channelId` from the
  channel page. Prefer `id:` entries when you know them (no lookup, no risk).
  A handle that fails to resolve logs a warning and is skipped — it never
  breaks the run.
- **`view_count`** on YouTube items comes from the RSS feed at fetch time and
  may be absent (`null`) — the feed does not always include it.
- **faster-whisper is optional.** It only runs as a fallback for caption-less
  (or fetch-blocked) YouTube videos. If it (or yt-dlp) is not installed, the
  video is still ingested with an empty transcript and a `transcript_status`
  flag (`captions` / `no_captions` / `fetch_error` / `transcription_*`).
- **YouTube transcripts via yt-dlp.** Transcripts are fetched with yt-dlp's
  caption extraction, not the lightweight transcript API — yt-dlp speaks to
  YouTube as a player client and, via `youtube.player_clients` in
  `config/sources.yaml` (default `web_safari`, `android_vr`), reaches it even
  from IPs where the transcript API is bot-blocked. A caption-less video falls
  back to yt-dlp audio + faster-whisper. If yt-dlp itself is blocked, set
  `youtube.proxy`.
- **arXiv** is filtered by `submittedDate`; arXiv does not announce on
  weekends, so a weekend `--date` may legitimately yield zero papers.
- **Extraction is batched.** The spec says "for each raw item, call Claude
  Haiku", but every `claude -p` call carries a large fixed prompt overhead, so
  one call per item (~380/day) would be wasteful. `extract` sends items in
  batches (`config/llm.yaml` → `extract.batch_size`, default 12) — identical
  per-item extraction, ~30 calls instead of ~380. A full day runs ~$2.
- **Per-item extraction cache.** Each item's result is cached by id, so an
  interrupted or re-run extraction only pays for what is missing. `--force`
  bypasses it.
- **Themed sections.** The digest is organized into the sections in
  `config/sections.yaml` (Security, Development, New Tools, …). `cluster`
  tags each story with a section; `synthesize` writes the ~15-minute script
  section by section, opening each with an on-screen section card.
- **Blog/RSS ingest.** `ingest/rss.py` follows the feeds in `sources.yaml`
  (`rss.feeds`) — AI leaders and practitioners (Simon Willison, Geoffrey
  Huntley, Karpathy, …). A feed that fails to load is logged and skipped.
- **Editorial balance — practitioner over academic.** arXiv produces hundreds
  of papers a day and would otherwise swamp the digest. `cluster` caps items
  per source (`config/llm.yaml` → `cluster.max_per_source`, arXiv default 35),
  and the extract/cluster/script prompts steer toward releases, what AI
  creators cover, and substantive community discussion — research is supporting
  context. Single-user anecdotes, complaints and hearsay are filtered out.
- **Screenshot quality gate.** A screenshot that comes back blank or
  half-loaded (one colour dominating the frame) is detected and replaced with a
  text card rather than shipped into the video.
- **`common.py`** and **`llm.py`** are small, deliberate additions to the
  spec's layout. Every stage needs the same logging/paths/IO helpers
  (`common.py`); `llm.py` is the "thin wrapper so models can be swapped" the
  spec asked for. Centralizing avoids `produce/` importing from `ingest/`.
- **`.gitignore`** was added so regenerated artifacts (`data/`, `logs/`,
  `.venv/`, `.env`) stay out of version control.
- Reddit requests are paced (~1s apart) and all HTTP carries a descriptive
  User-Agent + retry/backoff, to stay within unauthenticated rate limits.

## LLM backend (Stages 2–4)

`llm.py` is a thin wrapper with two interchangeable backends, selected in
`config/llm.yaml`:

- **`claude-cli`** (default) — shells out to the local Claude Code CLI
  (`claude -p`). No API key; uses your existing Claude Code login. This is the
  backend in use.
- **`anthropic-sdk`** — uses the Anthropic SDK and `ANTHROPIC_API_KEY`.
  Provided for portability; switch with `backend: anthropic-sdk` (or the
  `AI_DAILY_LLM_BACKEND` env var).

Stages request a *role* (`extract` / `cluster` / `synthesize`); `llm.yaml`
maps roles to models — `haiku` for extraction, `sonnet` for clustering and
scripting — so swapping models is a one-line change. The prompt is sent on
stdin, so prompt size is not limited by command-line length. `claude_cli`
also enforces a per-call `max_budget_usd` cap as a safety net.
