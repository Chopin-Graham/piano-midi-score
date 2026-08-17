from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.demo import demo_midi_bytes  # noqa: E402
from app.core.options import ConversionOptions  # noqa: E402
from app.core.pipeline import convert_midi  # noqa: E402


def main() -> None:
    artifacts = PROJECT_ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    midi_output = artifacts / "sample-piano.mid"
    xml_output = artifacts / "sample-piano.musicxml"
    data = demo_midi_bytes()
    musicxml, _, _ = convert_midi(
        data,
        midi_output.name,
        ConversionOptions(style="balanced", title="Piano Demo"),
    )
    midi_output.write_bytes(data)
    xml_output.write_text(musicxml, encoding="utf-8")
    print(midi_output)
    print(xml_output)


if __name__ == "__main__":
    main()
