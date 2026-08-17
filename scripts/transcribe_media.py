from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.engraver import render_a4_musicxml  # noqa: E402
from app.core.media_transcription import (  # noqa: E402
    AUDIO_PYTHON_ENV,
    transcribe_media,
)
from app.core.options import ConversionOptions, TranscriptionOptions  # noqa: E402
from app.core.pipeline import convert_midi  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe piano audio/video to MIDI, MusicXML, and an A4 PDF"
    )
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "output" / "audio-video" / "transcription",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "transkun", "basic_pitch"),
        default="auto",
    )
    parser.add_argument("--device", choices=("auto", "cpu", "cuda"), default="auto")
    parser.add_argument("--audio-python", type=Path)
    parser.add_argument("--no-beat-alignment", action="store_true")
    parser.add_argument("--minimum-note-ms", type=float, default=55.0)
    parser.add_argument("--allow-triplets", action="store_true")
    parser.add_argument(
        "--style",
        choices=("clean", "balanced", "faithful"),
        default="clean",
    )
    parser.add_argument(
        "--engraving-style",
        choices=("classic", "modern", "compact"),
        default="classic",
    )
    parser.add_argument("--title")
    args = parser.parse_args()

    input_path = args.input.resolve()
    if not input_path.is_file():
        raise SystemExit(f"Input file not found: {input_path}")
    if args.audio_python:
        os.environ[AUDIO_PYTHON_ENV] = str(args.audio_python.resolve())

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(input_path.stem)
    print(f"[1/3] Transcribing {input_path.name}", flush=True)
    transcription = transcribe_media(
        input_path.read_bytes(),
        input_path.name,
        TranscriptionOptions(
            backend=args.backend,
            device=args.device,
            align_beats=not args.no_beat_alignment,
            minimum_note_ms=args.minimum_note_ms,
        ),
    )
    raw_midi_path = output / f"{stem}-raw-transcription.mid"
    midi_path = output / f"{stem}-beat-aligned.mid"
    raw_midi_path.write_bytes(transcription.raw_midi_bytes)
    midi_path.write_bytes(transcription.midi_bytes)

    print("[2/3] Converting aligned MIDI to professional notation", flush=True)
    conversion_options = ConversionOptions(
        style=args.style,
        engraving_style=args.engraving_style,
        allow_triplets=args.allow_triplets,
        include_pedal=False,
        title=args.title or input_path.stem,
        audio_transcription=True,
    )
    musicxml, analysis, conversion_warnings = convert_midi(
        transcription.midi_bytes,
        midi_path.name,
        conversion_options,
    )
    musicxml_path = output / f"{stem}.musicxml"
    musicxml_path.write_text(musicxml, encoding="utf-8")

    print("[3/3] Rendering A4 PDF with MuseScore", flush=True)
    engraving = render_a4_musicxml(musicxml, args.engraving_style)
    pdf_path = output / f"{stem}-A4.pdf"
    preview_path = output / f"{stem}-preview.png"
    if engraving.pdf_bytes:
        pdf_path.write_bytes(engraving.pdf_bytes)
    if engraving.preview_png:
        preview_path.write_bytes(engraving.preview_png)

    report = {
        "input": str(input_path),
        "outputs": {
            "raw_midi": str(raw_midi_path),
            "aligned_midi": str(midi_path),
            "musicxml": str(musicxml_path),
            "pdf": str(pdf_path) if engraving.pdf_bytes else None,
            "preview": str(preview_path) if engraving.preview_png else None,
        },
        "transcription": transcription.analysis,
        "conversion": analysis,
        "engraving": engraving.analysis,
        "warnings": list(
            dict.fromkeys(
                [
                    *transcription.warnings,
                    *conversion_warnings,
                    *engraving.warnings,
                ]
            )
        ),
    }
    report_path = output / f"{stem}-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"MIDI: {midi_path}", flush=True)
    print(f"MusicXML: {musicxml_path}", flush=True)
    print(f"PDF: {pdf_path if engraving.pdf_bytes else 'unavailable'}", flush=True)
    print(f"Report: {report_path}", flush=True)


def _safe_stem(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return (normalized or "transcription")[:80]


if __name__ == "__main__":
    main()
