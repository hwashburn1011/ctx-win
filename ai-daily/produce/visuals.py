"""Stage 6 -- per-segment visuals.

For each script segment, render the visual its cue calls for:
  * screenshot   -- Playwright (headless Chromium) captures the source URL
  * youtube_clip -- yt-dlp grabs a <=15s clip; ffmpeg normalizes it to 1080p
  * text_card    -- a Pillow-rendered card (title + source)
Anything that fails falls back to a text card, so every segment always gets a
visual. Stills are 1920x1080 PNG; clips are 1920x1080 H.264 mp4.

CLI:  python -m produce.visuals --date 2026-05-14 --verbose
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (PROCESSED_DIR, default_date, get_logger, load_config,
                    now_iso, parse_date, read_json, setup_logging, write_json)

_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]


# --- small pure helpers ----------------------------------------------------
def _parse_ts(ts):
    """Parse 'MM:SS' / 'H:MM:SS' / seconds into float seconds (None if bad)."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        parts = [float(p) for p in str(ts).split(":")]
    except ValueError:
        return None
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def _card_title(seg: dict) -> str:
    """Best display title for a text/fallback card: quoted text, else cue."""
    desc = (seg.get("visual") or {}).get("description") or ""
    m = re.search(r"['\"]([^'\"]{6,})['\"]", desc)
    if m:
        return m.group(1).strip()
    if desc:
        return desc[:90].strip()
    return "AI Daily Digest"


def _source_label(seg: dict) -> str:
    """Host of the segment's source URL, for the card subtitle / attribution."""
    url = (seg.get("visual") or {}).get("url") or seg.get("source_url") or ""
    if not url:
        return ""
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


# --- rendering -------------------------------------------------------------
def _find_font(size: int, custom: str | None = None):
    from PIL import ImageFont
    for path in ([custom] if custom else []) + _FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    lines, cur = [], ""
    for word in (text or "").split():
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_width:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines or [""]


def _text_card(out_path, title: str, subtitle: str, cfg: dict, w: int, h: int):
    from PIL import Image, ImageDraw
    tc = cfg.get("text_card", {})
    img = Image.new("RGB", (w, h), tc.get("background", "#0d1117"))
    draw = ImageDraw.Draw(img)
    font_path = tc.get("font") or None
    title_font = _find_font(72, font_path)
    sub_font = _find_font(36, font_path)
    margin = 170

    lines = _wrap(draw, title, title_font, w - 2 * margin)[:6]
    line_h = (getattr(title_font, "size", 72)) + 20
    block_h = len(lines) * line_h
    y = (h - block_h) // 2 - 30
    for ln in lines:
        tw = draw.textlength(ln, font=title_font)
        draw.text(((w - tw) / 2, y), ln, font=title_font,
                  fill=tc.get("foreground", "#e6edf3"))
        y += line_h
    if subtitle:
        tw = draw.textlength(subtitle, font=sub_font)
        draw.text(((w - tw) / 2, y + 28), subtitle, font=sub_font,
                  fill=tc.get("accent", "#58a6ff"))
    draw.rectangle([margin, h - 160, margin + 96, h - 154],
                   fill=tc.get("accent", "#58a6ff"))
    img.save(str(out_path))


def _screenshot(page, url: str, out_path, sc_cfg: dict, log) -> bool:
    target = url
    if sc_cfg.get("reddit_use_old", True) and "reddit.com" in target:
        target = re.sub(r"https?://(www\.)?reddit\.com",
                        "https://old.reddit.com", target)
    try:
        page.goto(target, wait_until=sc_cfg.get("wait_until", "load"),
                  timeout=sc_cfg.get("timeout_seconds", 30) * 1000)
        page.wait_for_timeout(sc_cfg.get("settle_ms", 1200))
        page.screenshot(path=str(out_path))   # viewport shot == width x height
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(f"[visuals] screenshot failed for {target}: {e}")
        return False


def _youtube_clip(url, start, end, out_path, max_seconds, w, h, log) -> bool:
    try:
        import yt_dlp
    except ImportError:
        log.warning("[visuals] yt-dlp not installed -- cannot fetch clip")
        return False
    s = _parse_ts(start) or 0.0
    e = _parse_ts(end)
    if e is None or e <= s:
        e = s + max_seconds
    e = min(e, s + max_seconds)
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "clip.%(ext)s")
        try:
            with yt_dlp.YoutubeDL({
                "quiet": True, "noprogress": True, "outtmpl": src,
                "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
                "download_ranges": yt_dlp.utils.download_range_func(None, [(s, e)]),
                "force_keyframes_at_cuts": True,
            }) as ydl:
                ydl.download([url])
        except Exception as ex:  # noqa: BLE001
            log.warning(f"[visuals] clip download failed: {ex}")
            return False
        files = [os.path.join(td, f) for f in os.listdir(td)]
        if not files:
            log.warning("[visuals] clip download produced no file")
            return False
        vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        cmd = ["ffmpeg", "-y", "-i", files[0], "-t", str(max_seconds),
               "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
               "-pix_fmt", "yuv420p", "-c:a", "aac", str(out_path)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        except Exception as ex:  # noqa: BLE001
            log.warning(f"[visuals] ffmpeg normalize failed: {ex}")
            return False
        if r.returncode != 0:
            log.warning(f"[visuals] ffmpeg exited {r.returncode}: "
                        f"{(r.stderr or '')[-300:]}")
            return False
        return True


def run(date, *, logger=None, dry_run=False, force=False, limit=None):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    script_path = day_dir / "script.json"
    visuals_dir = day_dir / "visuals"
    manifest_path = visuals_dir / "visuals.json"

    if manifest_path.exists() and not force and not dry_run:
        log.info(f"[visuals] {manifest_path} exists -- skipping (use --force)")
        return read_json(manifest_path)

    if not script_path.exists():
        log.error(f"[visuals] {script_path} not found -- run synthesize first")
        return None
    segments = read_json(script_path).get("segments", [])
    if limit:
        segments = segments[:limit]
    if not segments:
        log.warning("[visuals] script has no segments")
        return None

    cfg = load_config("visuals.yaml")
    w, h = cfg.get("width", 1920), cfg.get("height", 1080)
    sc_cfg = cfg.get("screenshot", {})
    max_clip = cfg.get("clip", {}).get("max_seconds", 15)

    if dry_run:
        plan = [(s.get("visual") or {}).get("type", "text_card") for s in segments]
        log.info(f"[visuals] dry-run -- would render {len(plan)} visual(s): {plan}")
        write_json(manifest_path, {"date": f"{date:%Y-%m-%d}", "dry_run": True,
                                   "planned": plan}, dry_run=True, logger=log)
        return None

    visuals_dir.mkdir(parents=True, exist_ok=True)

    # Open one headless browser for all screenshots (if any are needed).
    need_shots = any((s.get("visual") or {}).get("type") == "screenshot"
                     for s in segments)
    pw = browser = page = None
    if need_shots:
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(
                viewport={"width": w, "height": h}).new_page()
        except Exception as e:  # noqa: BLE001
            log.warning(f"[visuals] Playwright unavailable ({e}) -- "
                        "screenshots will fall back to text cards")
            pw = browser = page = None

    entries = []
    try:
        for i, seg in enumerate(segments):
            vis = seg.get("visual") or {}
            cue = (vis.get("type") or "text_card").lower()
            url = vis.get("url") or seg.get("source_url")
            ok, kind, fname = False, None, None

            if cue == "screenshot" and page and url:
                fname = f"segment_{i:03d}.png"
                ok = _screenshot(page, url, visuals_dir / fname, sc_cfg, log)
                kind = "screenshot"
            elif cue == "youtube_clip" and url:
                fname = f"segment_{i:03d}.mp4"
                ok = _youtube_clip(url, vis.get("clip_start"),
                                   vis.get("clip_end"), visuals_dir / fname,
                                   max_clip, w, h, log)
                kind = "youtube_clip"
            elif cue == "text_card":
                fname = f"segment_{i:03d}.png"
                _text_card(visuals_dir / fname, _card_title(seg),
                           _source_label(seg), cfg, w, h)
                ok, kind = True, "text_card"

            if not ok:   # universal fallback -- a card always succeeds
                fname = f"segment_{i:03d}.png"
                _text_card(visuals_dir / fname, _card_title(seg),
                           _source_label(seg), cfg, w, h)
                kind = "fallback_card"
            entries.append({"index": i, "file": fname, "kind": kind,
                            "cue_type": cue})
            log.info(f"[visuals] segment {i + 1}/{len(segments)}: "
                     f"{kind} -> {fname}")
    finally:
        if browser:
            browser.close()
        if pw:
            pw.stop()

    manifest = {
        "date": f"{date:%Y-%m-%d}",
        "generated_at": now_iso(),
        "width": w, "height": h,
        "segment_count": len(entries),
        "segments": entries,
    }
    write_json(manifest_path, manifest, dry_run=dry_run, logger=log)
    n_fallback = sum(1 for e in entries if e["kind"] == "fallback_card")
    log.info(f"[visuals] {len(entries)} visual(s) rendered "
             f"({n_fallback} fell back to text cards) -> {visuals_dir}")
    return manifest


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 6: visuals")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't render, plan only")
    p.add_argument("--force", action="store_true",
                   help="re-render even if visuals.json exists")
    p.add_argument("--limit", type=int, help="cap segments (debugging)")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== visuals for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force,
        limit=args.limit)


if __name__ == "__main__":
    main()
