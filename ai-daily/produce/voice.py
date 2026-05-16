"""Stage 5 -- narration audio via Kokoro TTS (local, ONNX runtime).

Reads data/processed/<date>/script.json, synthesizes one WAV per segment with
Kokoro, concatenates them (with a short gap) into narration.wav, and writes a
timing manifest mapping each segment to its start/end offset in the combined
track -- everything Stage 7 needs to place visuals against the audio.

CLI:  python -m produce.voice --date 2026-05-14 --verbose
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (PROCESSED_DIR, PROJECT_ROOT, default_date, get_logger,
                    load_config, now_iso, parse_date, read_json,
                    setup_logging, write_json)

_CUE_RE = re.compile(r"\[VISUAL:[^\]]*\]", re.IGNORECASE)


def _strip_cues(text: str) -> str:
    """Remove any [VISUAL: ...] cue that leaked into narration text."""
    return _CUE_RE.sub("", text or "").strip()


def _build_timing(durations, gap):
    """Tile per-segment speech durations into start/end offsets.

    Each segment owns its trailing gap, so end[i] == start[i+1] and the last
    end equals the total track length -- visuals tile with no holes. Pure.
    """
    entries = []
    cursor = 0.0
    n = len(durations)
    for i, d in enumerate(durations):
        start = cursor
        cursor += d
        if gap > 0 and i < n - 1:
            cursor += gap
        entries.append({"start": round(start, 3), "end": round(cursor, 3),
                        "speech_duration": round(d, 3)})
    return entries


def run(date, *, logger=None, dry_run=False, force=False, limit=None):
    log = logger or get_logger()
    day_dir = PROCESSED_DIR / f"{date:%Y-%m-%d}"
    script_path = day_dir / "script.json"
    audio_dir = day_dir / "audio"
    manifest_path = audio_dir / "timing.json"

    if manifest_path.exists() and not force and not dry_run:
        log.info(f"[voice] {manifest_path} exists -- skipping (use --force)")
        return read_json(manifest_path)

    if not script_path.exists():
        log.error(f"[voice] {script_path} not found -- run synthesize first")
        return None
    segments = read_json(script_path).get("segments", [])
    if limit:
        segments = segments[:limit]
    if not segments:
        log.warning("[voice] script has no segments")
        return None

    cfg = load_config("voice.yaml")
    voice = cfg.get("voice", "af_heart")
    speed = cfg.get("speed", 1.0)
    lang = cfg.get("lang", "en-us")
    gap = cfg.get("segment_gap_seconds", 0.4)
    model_cfg = cfg.get("model", {})
    onnx_path = PROJECT_ROOT / model_cfg.get("onnx", "models/kokoro-v1.0.onnx")
    voices_path = PROJECT_ROOT / model_cfg.get("voices", "models/voices-v1.0.bin")

    missing = [str(p) for p in (onnx_path, voices_path) if not p.exists()]
    if missing:
        log.error(f"[voice] Kokoro model file(s) missing: {missing}. Download "
                  "them from https://github.com/thewh1teagle/kokoro-onnx/"
                  "releases and place them per config/voice.yaml.")
        return None

    try:
        import numpy as np
        import soundfile as sf
        from kokoro_onnx import Kokoro
    except ImportError as e:
        log.error(f"[voice] dependency missing ({e}) -- "
                  "pip install kokoro-onnx soundfile")
        return None

    log.info(f"[voice] loading Kokoro ({voice}, speed={speed})...")
    kokoro = Kokoro(str(onnx_path), str(voices_path))

    rendered = []  # (orig_index, segment, samples, sample_rate)
    for i, seg in enumerate(segments):
        text = _strip_cues(seg.get("text"))
        if not text:
            log.warning(f"[voice] segment {i} has no narration text -- skipping")
            continue
        log.info(f"[voice] segment {i + 1}/{len(segments)} "
                 f"({len(text.split())} words)...")
        samples, sr = kokoro.create(text, voice=voice, speed=speed, lang=lang)
        rendered.append((i, seg, np.asarray(samples, dtype=np.float32), sr))

    if not rendered:
        log.error("[voice] no audio was produced")
        return None

    sample_rate = rendered[0][3]
    timing = _build_timing([len(s) / sr for _, _, s, sr in rendered], gap)

    if not dry_run:
        audio_dir.mkdir(parents=True, exist_ok=True)

    pieces, seg_entries = [], []
    for pos, (orig_i, seg, samples, sr) in enumerate(rendered):
        seg_file = f"segment_{orig_i:03d}.wav"
        if not dry_run:
            sf.write(str(audio_dir / seg_file), samples, sr, subtype="PCM_16")
        pieces.append(samples)
        if gap > 0 and pos < len(rendered) - 1:
            pieces.append(np.zeros(int(gap * sr), dtype=np.float32))
        seg_entries.append({
            "index": orig_i,
            "file": seg_file,
            "start": timing[pos]["start"],
            "end": timing[pos]["end"],
            "speech_duration": timing[pos]["speech_duration"],
            "visual": seg.get("visual"),
            "source_url": seg.get("source_url"),
        })

    combined = np.concatenate(pieces)
    total = len(combined) / sample_rate
    if not dry_run:
        sf.write(str(audio_dir / "narration.wav"), combined, sample_rate,
                 subtype="PCM_16")

    manifest = {
        "date": f"{date:%Y-%m-%d}",
        "generated_at": now_iso(),
        "engine": f"kokoro-onnx:{voice}",
        "sample_rate": sample_rate,
        "segment_count": len(seg_entries),
        "total_duration": round(total, 3),
        "narration_file": "narration.wav",
        "segments": seg_entries,
    }
    write_json(manifest_path, manifest, dry_run=dry_run, logger=log)
    log.info(f"[voice] {len(seg_entries)} segment(s), {total:.1f}s total "
             f"(~{total / 60:.1f} min)" + ("" if dry_run else
             f" -> {audio_dir / 'narration.wav'}"))
    return manifest


def main():
    p = argparse.ArgumentParser(description="ai-daily Stage 5: voice")
    p.add_argument("--date", help="YYYY-MM-DD (default: yesterday, UTC)")
    p.add_argument("--verbose", action="store_true", help="debug logging")
    p.add_argument("--dry-run", action="store_true", help="don't write outputs")
    p.add_argument("--force", action="store_true",
                   help="re-synthesize even if timing.json exists")
    p.add_argument("--limit", type=int, help="cap segments (debugging)")
    args = p.parse_args()

    log = setup_logging(args.verbose)
    date = parse_date(args.date) if args.date else default_date()
    log.info(f"=== voice for {date} ===")
    run(date, logger=log, dry_run=args.dry_run, force=args.force,
        limit=args.limit)


if __name__ == "__main__":
    main()
