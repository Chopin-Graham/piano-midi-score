from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from backend.tests.midi_factory import dense_midi_bytes  # noqa: E402

from app.core.options import ConversionOptions  # noqa: E402
from app.core.pipeline import convert_midi  # noqa: E402


def main() -> None:
    for note_count in (300, 1200, 3000):
        data = dense_midi_bytes(note_count)
        started = time.perf_counter()
        musicxml, analysis, _ = convert_midi(
            data,
            f"benchmark-{note_count}.mid",
            ConversionOptions(style="clean"),
        )
        elapsed = time.perf_counter() - started
        print(
            f"{note_count:>5} source notes -> {analysis['note_count']:>5} notation notes | "
            f"{elapsed * 1000:>8.1f} ms | {len(musicxml) / 1024:>8.1f} KiB"
        )


if __name__ == "__main__":
    main()

