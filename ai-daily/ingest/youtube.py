"""YouTube ingest -- channel RSS feeds + transcripts via yt-dlp (no API key).

Recent uploads are discovered through each channel's public RSS feed
(youtube.com/feeds/videos.xml?channel_id=...). Transcripts are fetched with
yt-dlp's caption extraction: yt-dlp speaks to YouTube as a media-player client
and, with the right `player_client`, reaches it where the lightweight
transcript API is IP-blocked. A video with no captions falls back to
downloading audio with yt-dlp and transcribing it locally with faster-whisper
(optional -- if it is not installed the video is kept with an empty transcript
and a status flag).
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from html import unescape as _html_unescape

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser

from common import get_logger, http_get, in_window, ingest_cli, load_config

SOURCE = "youtube"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
# yt-dlp YouTube player clients that bypass the bot-check the default `web`
# client trips from datacenter / shared IPs.
DEFAULT_PLAYER_CLIENTS = ["web_safari", "android_vr"]
_TAG_RE = re.compile(r"<[^>]+>")


def _resolve_channel_id(entry: dict, log) -> tuple[str | None, str | None]:
    """Return (channel_id, label). Resolves a @handle by scraping the page."""
    cid = entry.get("id")
    if cid:
        return cid, entry.get("name") or cid
    handle = entry.get("handle")
    if not handle:
        log.warning(f"channel entry has neither id nor handle: {entry}")
        return None, None
    handle = handle if handle.startswith("@") else "@" + handle
    try:
        html = http_get(f"https://www.youtube.com/{handle}", logger=log).text
    except Exception as e:  # noqa: BLE001
        log.error(f"could not load channel page for {handle}: {e}")
        return None, None
    m = (re.search(r'"channelId":"(UC[\w-]{20,})"', html)
         or re.search(r'"externalId":"(UC[\w-]{20,})"', html)
         or re.search(r'channel/(UC[\w-]{20,})', html))
    if not m:
        log.error(f"could not resolve channelId for {handle} -- skipping")
        return None, None
    log.debug(f"resolved {handle} -> {m.group(1)}")
    return m.group(1), handle


def _entry_dt(entry) -> datetime | None:
    t = entry.get("published_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _view_count(entry) -> int | None:
    stats = entry.get("media_statistics") or {}
    try:
        return int(stats["views"])
    except (KeyError, TypeError, ValueError):
        return None


def _ytdlp_opts(player_clients, proxy, extra=None) -> dict:
    """Base yt-dlp options: quiet, with the configured YouTube player clients
    and (optionally) a proxy."""
    opts = {
        "quiet": True,
        "noprogress": True,
        "extractor_args": {"youtube": {"player_client": list(player_clients)}},
    }
    if proxy:
        opts["proxy"] = proxy
    if extra:
        opts.update(extra)
    return opts


def _vtt_to_text(vtt: str) -> str:
    """Flatten a WebVTT caption file into plain transcript text -- strips the
    inline timing tags and collapses the rolling auto-caption repeats."""
    lines: list[str] = []
    for raw in vtt.splitlines():
        line = raw.strip()
        if (not line or line == "WEBVTT" or "-->" in line
                or line.startswith(("Kind:", "Language:", "NOTE", "STYLE"))):
            continue
        line = _html_unescape(_TAG_RE.sub("", line)).strip()
        if line and (not lines or lines[-1] != line):
            lines.append(line)
    return " ".join(lines)


def _get_transcript(video_id, languages, player_clients, proxy, log):
    """Fetch a transcript via yt-dlp's caption extraction.

    Returns (text, status, duration_seconds). status is one of:
      "captions"     -- transcript fetched
      "no_captions"  -- the video has no usable captions
      "fetch_error"  -- yt-dlp could not reach the video
      "lib_missing"  -- yt-dlp is not installed
    """
    try:
        import yt_dlp
    except ImportError:
        return None, "lib_missing", None
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as td:
        opts = _ytdlp_opts(player_clients, proxy, {
            "skip_download": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(languages),
            "subtitlesformat": "vtt",
            "outtmpl": os.path.join(td, "%(id)s.%(ext)s"),
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:  # noqa: BLE001
            log.debug(f"{video_id}: yt-dlp transcript fetch failed ({e})")
            return None, "fetch_error", None
        duration = info.get("duration")
        vtts = sorted(f for f in os.listdir(td) if f.endswith(".vtt"))
        if not vtts:
            return None, "no_captions", duration
        # prefer a manual ".<lang>.vtt" over an auto "...-orig.vtt" track
        pick = next((f for f in vtts if "-orig." not in f), vtts[0])
        with open(os.path.join(td, pick), encoding="utf-8") as f:
            text = _vtt_to_text(f.read())
        return text or None, "captions" if text else "no_captions", duration


def _whisper_fallback(video_id, model_size, max_seconds, player_clients,
                      proxy, log):
    """Download audio with yt-dlp, transcribe with faster-whisper (CPU)."""
    try:
        import yt_dlp
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning(f"{video_id}: yt-dlp/faster-whisper not installed -- "
                    "keeping video with empty transcript")
        return None, "transcription_unavailable", None

    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as td:
        try:
            with yt_dlp.YoutubeDL(_ytdlp_opts(player_clients, proxy,
                                  {"skip_download": True})) as ydl:
                info = ydl.extract_info(url, download=False)
            duration = info.get("duration")
            if duration and duration > max_seconds:
                return None, "skipped_too_long", duration
            out_tmpl = os.path.join(td, f"{video_id}.%(ext)s")
            with yt_dlp.YoutubeDL(_ytdlp_opts(player_clients, proxy, {
                "format": "bestaudio/best", "outtmpl": out_tmpl,
                "postprocessors": [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "wav"}],
            })) as ydl:
                ydl.download([url])
            wav = os.path.join(td, f"{video_id}.wav")
            if not os.path.exists(wav):
                return None, "audio_download_failed", duration
            log.info(f"{video_id}: transcribing audio with faster-whisper "
                     f"({model_size})...")
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            segs, _ = model.transcribe(wav)
            text = " ".join(s.text.strip() for s in segs).strip()
            return text, "whisper", duration
        except Exception as e:  # noqa: BLE001
            log.warning(f"{video_id}: whisper fallback failed: {e}")
            return None, "transcription_failed", None


def fetch(date, *, logger=None, config=None, limit=None) -> list[dict]:
    log = logger or get_logger()
    channels = (config or load_config("channels.yaml")).get("channels", [])
    yt_cfg = load_config("sources.yaml").get("youtube", {})
    max_minutes = yt_cfg.get("max_video_minutes", 60)
    max_seconds = max_minutes * 60
    languages = yt_cfg.get("languages", ["en.*"])
    whisper_model = yt_cfg.get("whisper_model", "base")
    player_clients = yt_cfg.get("player_clients", DEFAULT_PLAYER_CLIENTS)
    proxy = (yt_cfg.get("proxy") or "").strip() or None

    log.info(f"youtube: {len(channels)} channel(s) | max {max_minutes} min/video"
             f" | clients={player_clients}" + (" | via proxy" if proxy else ""))
    items: list[dict] = []
    for ch in channels:
        cid, label = _resolve_channel_id(ch, log)
        if not cid:
            continue
        feed = feedparser.parse(FEED_URL.format(cid=cid))
        if feed.bozo and not feed.entries:
            log.warning(f"{label}: feed parse error: {feed.get('bozo_exception')}")
            continue
        ch_title = feed.feed.get("title", label)
        recent = [e for e in feed.entries if in_window(_entry_dt(e), date)]
        log.info(f"  {ch_title}: {len(recent)} upload(s) in window")
        for e in recent:
            vid = e.get("yt_videoid")
            if not vid:
                continue
            text, status, duration = _get_transcript(
                vid, languages, player_clients, proxy, log)
            if duration and duration > max_seconds:
                log.info(f"  skip (>{max_minutes} min): {e.get('title')}")
                continue
            if text is None and status in ("no_captions", "fetch_error"):
                text, status, duration = _whisper_fallback(
                    vid, whisper_model, max_seconds, player_clients, proxy, log)
                if status == "skipped_too_long":
                    log.info(f"  skip (>{max_minutes} min): {e.get('title')}")
                    continue
            pub = _entry_dt(e)
            log.info(f"  {e.get('title')!r:.70} -- transcript: {status}")
            items.append({
                "id": f"yt_{vid}",
                "source": "youtube",
                "title": e.get("title"),
                "channel": ch_title,
                "url": e.get("link"),
                "published_at": pub.isoformat() if pub else None,
                "view_count": _view_count(e),
                "duration_seconds": duration,
                "transcript": text,
                "transcript_status": status,
            })
            if limit and len(items) >= limit:
                log.info(f"youtube: hit --limit {limit}, stopping")
                return items
    log.info(f"youtube: {len(items)} video(s) collected")
    return items


if __name__ == "__main__":
    ingest_cli(SOURCE, fetch)
