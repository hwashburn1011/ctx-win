"""Stage 7 -- assemble the final video with ffmpeg.

Stitches each segment's visual (shown for the duration of its narration audio,
per timing.json) into a single track, burns in a bottom-left source-attribution
lower third, brackets it with 1-second intro/outro cards, and muxes the
combined narration over it.

Output: data/output/<date>.mp4 -- 1920x1080, H.264 + AAC.

CLI:  python -m produce.assemble --date 2026-05-14 --verbose
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from shutil import which

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (OUTPUT_DIR, PROCESSED_DIR, default_date, get_logger,
                    load_config, parse_date, read_json, setup_logging)
from produce.visuals import _find_font, _text_card


def _host(url: str | None) -> str:
    """Bare host of a URL, for the lower-third attribution."""
    if not url:
        return ""
    return re.sub(r"^https?://(www\.)?", "", url).split("/")[0]


def _concat_list(paths) -> str:
    """ffmpeg concat-demuxer list body for the given clip paths."""
    return "".join(f"file '{Path(p).as_posix()}'\n" for p in paths)


def _have_ffmpeg() -> bool:
    return which("ffmpeg") is not None


def _run_ffmpeg(cmd, log, what: str) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *cmd],
                           capture_output=True, text=True, timeout=600)
    except Exception as e:  # noqa: BLE001
        log.error(f"[assemble] ffmpeg ({what}) failed to run: {e}")
        return False
    if r.returncode != 0:
        log.error(f"[assemble] ffmpeg ({what}) exited {r.returncode}: "
                  f"{(r.stderr or '').strip()[-400:]}")
        return False
    return True


def _lower_third_overlay(text: str, lt: dict, w: int, h: int):
    """A *transparent* w x h RGBA image carrying just the attribution bar.

    Used as-is to overlay onto a video clip (ffmpeg), and composited onto a
    still by _add_lower_third. It must stay transparent everywhere except the
    bar -- an opaque overlay would black out the clip underneath it.
    """
    from PIL import Image, ImageDraw
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if not text or not lt.get("enabled", True):
        return overlay
    box_h = lt.get("height", 56)
    margin = lt.get("margin", 40)
    font_size = lt.get("font_size", 28)
    font = _find_font(font_size)
    draw = ImageDraw.Draw(overlay)
    text_w = draw.textlength(text, font=font)
    y0 = h - box_h - margin
    draw.rectangle([0, y0, int(text_w + 2 * margin), y0 + box_h],
                   fill=(0, 0, 0, int(255 * lt.get("opacity", 0.55))))
    draw.text((margin, y0 + (box_h - font_size) // 2 - 2), text, font=font,
              fill=lt.get("text_color", "#ffffff"))
    return overlay


def _add_lower_third(img, text: str, lt: dict):
    """Composite the attribution lower third onto `img`; returns opaque RGB."""
    from PIL import Image
    base = img.convert("RGBA")
    overlay = _lower_third_overlay(text, lt, base.size[0], base.size[1])
    return Image.alpha_composite(base, overlay).convert("RGB")


def _still_clip(png, duration, out, fps, vcodec, log) -> bool:
    return _run_ffmpeg(
        ["-loop", "1", "-i", str(png), "-t", f"{duration:.3f}",
         "-r", str(fps), "-c:v", vcodec["codec"], "-preset", vcodec["preset"],
         "-crf", str(vcodec["crf"]), "-pix_fmt", "yuv420p",
         "-vf", f"scale={vcodec['w']}:{vcodec['h']}", "-an", str(out)],
        log, f"still {Path(out).name}")


def _clip_segment(clip, lt_png, duration, out, fps, vcodec, log) -> bool:
    """Loop a B-roll clip to fill `duration` and overlay the lower third."""
    return _run_ffmpeg(
        ["-stream_loop", "-1", "-i", str(clip), "-i", str(lt_png),
         "-t", f"{duration:.3f}",
         "-filter_complex",
         f"[0:v]scale={vcodec['w']}:{vcodec['h']},fps={fps}[v];"
         f"[v][1:v]overlay=0:0[o]",
         "-map", "[o]", "-an", "-c:v", vcodec["codec"],
         "-preset", vcodec["preset"], "-crf", str(vcodec["crf"]),
         "-pix_fmt", "yuv420p", str(out)],
        log, f"clip {Path(out).name}")


def run(date, *, logger=None, dry_run=False, force=False):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    timing_path = day_dir / "audio" / "timing.json"
    visuals_path = day_dir / "visuals" / "visuals.json"
    narration = day_dir / "audio" / "narration.wav"
    out_path = OUTPUT_DIR / f"{date:%Y-%m-%d}.mp4"

    if out_path.exists() and not force and not dry_run:
        log.info(f"[assemble] {out_path} exists -- skipping (use --force)")
        return out_path

    for p in (timing_path, visuals_path, narration):
        if not p.exists():
            log.error(f"[assemble] missing input {p} -- run earlier stages first")
            return None
    if not _have_ffmpeg():
        log.error("[assemble] ffmpeg not found on PATH")
        return None

    timing = read_json(timing_path)
    vis_by_idx = {v["index"]: v for v in read_json(visuals_path)["segments"]}
    segments = timing.get("segments", [])
    if not segments:
        log.warning("[assemble] timing.json has no segments")
        return None

    acfg = load_config("assemble.yaml")
    vcfg = load_config("visuals.yaml")
    w, h = vcfg.get("width", 1920), vcfg.get("height", 1080)
    fps = acfg.get("fps", 30)
    intro_s = acfg.get("intro_seconds", 1.0)
    outro_s = acfg.get("outro_seconds", 1.0)
    vc = acfg.get("video", {})
    vcodec = {"codec": vc.get("codec", "libx264"),
              "preset": vc.get("preset", "medium"),
              "crf": vc.get("crf", 20), "w": w, "h": h}
    acodec = acfg.get("audio", {})
    lt = acfg.get("lower_third", {})
    date_label = date.strftime("%B %d, %Y")

    if dry_run:
        log.info(f"[assemble] dry-run -- would assemble {len(segments)} "
                 f"segment(s) + intro/outro into {out_path}")
        return None

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    from PIL import Image

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        clips: list[Path] = []

        # --- intro card ---
        intro_png, intro_clip = td / "intro.png", td / "intro.mp4"
        _text_card(intro_png, acfg.get("intro_title", "AI Daily"),
                   date_label, vcfg, w, h)
        if not _still_clip(intro_png, intro_s, intro_clip, fps, vcodec, log):
            return None
        clips.append(intro_clip)

        # --- one clip per segment ---
        for seg in segments:
            i = seg["index"]
            duration = max(0.5, seg["end"] - seg["start"])
            src = _host(seg.get("source_url"))
            vis = vis_by_idx.get(i)
            visual_file = day_dir / "visuals" / vis["file"] if vis else None
            seg_clip = td / f"seg_{i:03d}.mp4"

            if visual_file and visual_file.suffix == ".mp4" and visual_file.exists():
                lt_png = td / f"lt_{i:03d}.png"
                _lower_third_overlay(src, lt, w, h).save(lt_png)
                ok = _clip_segment(visual_file, lt_png, duration, seg_clip,
                                   fps, vcodec, log)
            else:
                if visual_file and visual_file.exists():
                    base = Image.open(visual_file)
                else:
                    log.warning(f"[assemble] segment {i}: visual missing, "
                                "using a plain card")
                    base = Image.new("RGB", (w, h), (13, 17, 23))
                comp = td / f"comp_{i:03d}.png"
                _add_lower_third(base, src, lt).save(comp)
                ok = _still_clip(comp, duration, seg_clip, fps, vcodec, log)
            if not ok:
                return None
            clips.append(seg_clip)
            log.info(f"[assemble] segment {i + 1}/{len(segments)}: "
                     f"{duration:.1f}s clip built")

        # --- outro card ---
        outro_png, outro_clip = td / "outro.png", td / "outro.mp4"
        _text_card(outro_png, acfg.get("intro_title", "AI Daily"),
                   date_label, vcfg, w, h)
        if not _still_clip(outro_png, outro_s, outro_clip, fps, vcodec, log):
            return None
        clips.append(outro_clip)

        # --- concat the silent video ---
        list_file = td / "concat.txt"
        list_file.write_text(_concat_list(clips), encoding="utf-8")
        silent = td / "silent.mp4"
        if not _run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(list_file),
                            "-c", "copy", str(silent)], log, "concat"):
            return None

        # --- mux narration (delayed past the intro card) ---
        delay = int(intro_s * 1000)
        if not _run_ffmpeg(
                ["-i", str(silent), "-i", str(narration),
                 "-filter_complex", f"[1:a]adelay={delay}:all=1[a]",
                 "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                 "-c:a", acodec.get("codec", "aac"),
                 "-b:a", acodec.get("bitrate", "192k"), str(out_path)],
                log, "mux"):
            return None

    size_mb = out_path.stat().st_size / 1e6
    total = timing.get("total_duration", 0) + intro_s + outro_s
    log.info(f"[assemble] wrote {out_path}  ({size_mb:.1f} MB, "
             f"~{total / 60:.1f} min, {w}x{h})")
    return out_path


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 7: assemble")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="plan only, don't render")
    p.add_argument("--force", action="store_true",
                   help="re-assemble even if the mp4 exists")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== assemble for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
