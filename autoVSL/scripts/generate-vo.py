#!/usr/bin/env python3
"""Generate VO lines for free using Microsoft Edge TTS (no API key)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import edge_tts

# Deep, calm male voices closest to ElevenLabs Daniel/Adam
DEFAULT_VOICE = "en-US-ChristopherNeural"
ALT_VOICE = "en-US-GuyNeural"


async def generate_line(text: str, voice: str, output: Path) -> None:
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(output))


async def main() -> int:
    parser = argparse.ArgumentParser(description="Generate VSL voiceover for free via Edge TTS")
    parser.add_argument("slug", nargs="?", default="fairy-flame", help="VSL slug under vsls/")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"TTS voice (default: {DEFAULT_VOICE})")
    parser.add_argument("--list-voices", action="store_true", help="List recommended voices and exit")
    args = parser.parse_args()

    if args.list_voices:
        print("Recommended free voices (deep, calm):")
        for v in [DEFAULT_VOICE, ALT_VOICE, "en-US-EricNeural", "en-GB-RyanNeural"]:
            print(f"  {v}")
        return 0

    root = Path(__file__).resolve().parent.parent
    vo_file = root / "vsls" / args.slug / "elevenlabs-vo.json"
    out_dir = root / "vsls" / args.slug / "media" / "audio"
    out_dir.mkdir(parents=True, exist_ok=True)

    if not vo_file.exists():
        print(f"Missing {vo_file}", file=sys.stderr)
        return 1

    with vo_file.open() as f:
        data = json.load(f)

    lines = data["lines"]
    print(f"Generating {len(lines)} VO lines → {out_dir}")
    print(f"Voice: {args.voice} (free, no API key)\n")

    for line in lines:
        out = out_dir / line["filename"]
        print(f"  {line['filename']} …", end=" ", flush=True)
        await generate_line(line["text"], args.voice, out)
        print("done")

    print(f"\n✓ {len(lines)} files saved. Cost: $0")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
