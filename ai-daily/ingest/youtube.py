"""YouTube ingest -- channel RSS feeds + transcripts (no API key required).

Recent uploads are discovered through each channel's public RSS feed
(youtube.com/feeds/videos.xml?channel_id=...). For every upload inside the
target UTC day we try captions via youtube-transcript-api; if a video has no
captions we fall back to downloading audio with yt-dlp and transcribing it
locally with faster-whisper. faster-whisper is optional -- if it (or yt-dlp)
is not installed, the video is kept with an empty transcript and a status flag.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feedparser

from common import get_logger, http_get, in_window, ingest_cli, load_config

SOURCE = "youtube"
FEED_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


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


def _transcript_api(proxy: str | None):
    """Return (api, kind) for fetching transcripts, honouring `proxy`.

    kind is "legacy" (0.6.x classmethod) or "instance" (1.x). Returns
    (None, None) if youtube-transcript-api is not installed.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        return None, None
    if hasattr(YouTubeTranscriptApi, "get_transcript"):              # 0.6.x
        return YouTubeTranscriptApi, "legacy"
    if not proxy:                                                    # 1.x
        return YouTubeTranscriptApi(), "instance"
    try:
        from youtube_transcript_api.proxies import GenericProxyConfig
        cfg = GenericProxyConfig(http_url=proxy, https_url=proxy)
        return YouTubeTranscriptApi(proxy_config=cfg), "instance"
    except Exception:  # noqa: BLE001 -- proxy config unsupported, fetch direct
        return YouTubeTranscriptApi(), "instance"


def _get_transcript(video_id: str, languages: list[str], proxy: str | None, log):
    """Return (text, status, duration_seconds) using youtube-transcript-api.

    status is one of:
      "captions"      -- transcript fetched successfully
      "no_captions"   -- the video genuinely has no usable transcript
      "fetch_error"   -- the request failed (often a datacenter-IP block);
                         the audio fallback should still be attempted
      "lib_missing"   -- youtube-transcript-api is not installed

    If `proxy` is set, transcript requests are routed through it. Handles both
    the 0.6.x (get_transcript) and 1.x (instance .fetch) package APIs.
    """
    api, kind = _transcript_api(proxy)
    if api is None:
        return None, "lib_missing", None
    try:  # the "no transcript" error classes; importable in 0.6.x and 1.x
        from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
        no_caption_errors = (TranscriptsDisabled, NoTranscriptFound)
    except ImportError:
        no_caption_errors = ()
    try:
        if kind == "legacy":                                         # 0.6.x
            kw = {"languages": languages}
            if proxy:
                kw["proxies"] = {"http": proxy, "https": proxy}
            segs = api.get_transcript(video_id, **kw)
        else:                                                        # 1.x
            fetched = api.fetch(video_id, languages=languages)
            segs = [{"text": s.text, "start": s.start, "duration": s.duration}
                    for s in fetched]
        text = " ".join((s["text"] or "").replace("\n", " ") for s in segs).strip()
        duration = None
        if segs:
            last = segs[-1]
            duration = (last.get("start") or 0) + (last.get("duration") or 0)
        return text, "captions", duration
    except no_caption_errors as e:
        log.debug(f"{video_id}: no captions available ({e})")
        return None, "no_captions", None
    except Exception as e:  # noqa: BLE001
        log.debug(f"{video_id}: transcript fetch failed, may be IP-blocked ({e})")
        return None, "fetch_error", None


def _whisper_fallback(video_id: str, model_size: str, max_seconds: int,
                      proxy: str | None, log):
    """Download audio with yt-dlp, transcribe with faster-whisper (CPU).

    If `proxy` is set, yt-dlp downloads through it -- consistent with the
    transcript fetch, so an IP-blocked host can still get audio.
    """
    try:
        import yt_dlp
        from faster_whisper import WhisperModel
    except ImportError:
        log.warning(f"{video_id}: yt-dlp/faster-whisper not installed -- "
                    "keeping video with empty transcript")
        return None, "transcription_unavailable", None

    proxy_opt = {"proxy": proxy} if proxy else {}
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as td:
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "noprogress": True,
                                   "skip_download": True, **proxy_opt}) as ydl:
                info = ydl.extract_info(url, download=False)
            duration = info.get("duration")
            if duration and duration > max_seconds:
                return None, "skipped_too_long", duration
            out_tmpl = os.path.join(td, f"{video_id}.%(ext)s")
            with yt_dlp.YoutubeDL({
                "quiet": True, "noprogress": True, "format": "bestaudio/best",
                "outtmpl": out_tmpl, **proxy_opt,
                "postprocessors": [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "wav"}],
            }) as ydl:
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
    languages = yt_cfg.get("languages", ["en", "en-US", "en-GB"])
    whisper_model = yt_cfg.get("whisper_model", "base")
    proxy = (yt_cfg.get("proxy") or "").strip() or None

    log.info(f"youtube: {len(channels)} channel(s) | max {max_minutes} min/video"
             + (" | via proxy" if proxy else ""))
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
            text, status, duration = _get_transcript(vid, languages, proxy, log)
            if duration and duration > max_seconds:
                log.info(f"  skip (>{max_minutes} min): {e.get('title')}")
                continue
            if text is None and status in ("no_captions", "fetch_error"):
                text, status, duration = _whisper_fallback(
                    vid, whisper_model, max_seconds, proxy, log)
                if status == "skipped_too_long":
                    log.info(f"  skip (>{max_minutes} min): {e.get('title')}")
                    continue
            pub = _entry_dt(e)
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
