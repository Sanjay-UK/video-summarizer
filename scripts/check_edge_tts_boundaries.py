#!/usr/bin/env python3
"""Report which boundary events edge-tts emits on this machine."""

from __future__ import annotations

import asyncio
import sys


async def main() -> int:
    try:
        from edge_tts import Communicate
    except ImportError:
        print("edge-tts not installed")
        return 1

    text = "Hello world. This is a short boundary test for highlight captions."
    types_seen: set[str] = set()
    samples: list[dict] = []

    try:
        for boundary_mode in ("WordBoundary", "SentenceBoundary"):
            communicate = Communicate(
                text,
                voice="en-US-GuyNeural",
                boundary=boundary_mode,  # type: ignore[arg-type]
            )
            async for message in communicate.stream():
                if message["type"] != "audio":
                    types_seen.add(message["type"])
                    if len(samples) < 5:
                        samples.append({"mode": boundary_mode, **message})
    except Exception as exc:
        print("Edge TTS stream failed on this machine:", exc)
        print(
            "Library supports WordBoundary/SentenceBoundary when the Bing endpoint accepts "
            "the connection. Upgrade with: pip install -U edge-tts"
        )
        return 2

    print("Boundary events observed (non-audio chunk types):", sorted(types_seen))
    for sample in samples:
        print(sample)
    if "WordBoundary" in types_seen:
        print("RESULT: WordBoundary events are available.")
    elif "SentenceBoundary" in types_seen:
        print("RESULT: Only SentenceBoundary events (no WordBoundary).")
    else:
        print("RESULT: No boundary metadata returned (audio only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
