"""Round-trip evaluation CLI: MIDI -> MusicXML/PDF -> MIDI -> fidelity report.

Closes the engraving loop the way a musician would: engrave the source MIDI,
ask MuseScore what the engraved score actually says (MusicXML -> MIDI), then
compare the result with the source at note level and, optionally, at audio
level (both sides rendered by the same MuseScore synthesizer so timbre
cancels out).  Page PNGs are produced for visual line-by-line review.

Example:
    .venv/Scripts/python.exe scripts/roundtrip_eval.py input.mid \
        --output tmp/roundtrip/case1 --audio --pages
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.engraver import find_musescore, render_a4_musicxml  # noqa: E402
from app.core.media_transcription import find_audio_python  # noqa: E402
from app.core.options import ConversionOptions  # noqa: E402
from app.core.pipeline import convert_midi_with_score  # noqa: E402
from app.core.roundtrip import (  # noqa: E402
    audio_similarity,
    evaluate_roundtrip,
    musescore_convert,
    render_pdf_pages,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
AUDIO_WORKER = PROJECT_ROOT / "backend" / "app" / "audio_worker.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("midi", type=Path, help="Source MIDI file")
    parser.add_argument("--output", type=Path, required=True, help="Output directory")
    parser.add_argument(
        "--style",
        choices=["clean", "balanced", "faithful"],
        default="clean",
    )
    parser.add_argument(
        "--audio-transcription",
        action="store_true",
        help="Treat input as an audio-transcription MIDI (release cleanup etc.)",
    )
    parser.add_argument("--audio", action="store_true", help="Add audio-level similarity")
    parser.add_argument("--pages", action="store_true", help="Render PDF pages to PNG")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument(
        "--onset-tolerance",
        type=float,
        default=0.12,
        help="Onset matching tolerance in beats (quarter notes)",
    )
    args = parser.parse_args()

    musescore = find_musescore()
    if musescore is None:
        print("MuseScore Studio 4 was not found", file=sys.stderr)
        return 2

    output_dir: Path = args.output
    output_dir.mkdir(parents=True, exist_ok=True)
    source_bytes = args.midi.read_bytes()

    options = ConversionOptions(
        style=args.style,
        audio_transcription=args.audio_transcription,
    )
    musicxml, analysis, warnings, _score = convert_midi_with_score(
        source_bytes,
        args.midi.name,
        options,
    )
    xml_path = output_dir / f"{args.midi.stem}.musicxml"
    xml_path.write_text(musicxml, encoding="utf-8")

    engraving = render_a4_musicxml(musicxml, options.engraving_style)
    pdf_path = None
    if engraving.pdf_bytes:
        pdf_path = output_dir / f"{args.midi.stem}-A4.pdf"
        pdf_path.write_bytes(engraving.pdf_bytes)

    roundtrip_path = output_dir / f"{args.midi.stem}-roundtrip.mid"
    musescore_convert(musescore, xml_path, roundtrip_path)

    def audio_fn():
        # Both sides are synthesized by the same deterministic tone generator
        # inside the audio worker, so tempo-rounding and MIDI-import quirks of
        # external synthesizers cannot masquerade as engraving differences.
        return audio_similarity(
            find_audio_python(),
            AUDIO_WORKER,
            args.midi,
            roundtrip_path,
            output_dir,
        )

    report = evaluate_roundtrip(
        source_bytes,
        roundtrip_path.read_bytes(),
        onset_tolerance=args.onset_tolerance,
        audio_similarity_fn=audio_fn if args.audio else None,
    )

    page_images: list[str] = []
    if args.pages and pdf_path is not None:
        page_images = [
            str(path)
            for path in render_pdf_pages(
                pdf_path,
                output_dir / "page",
                dpi=args.dpi,
            )
        ]

    report.update(
        {
            "input": str(args.midi),
            "style": args.style,
            "audio_transcription": args.audio_transcription,
            "pipeline_note_count": analysis["note_count"],
            "pipeline_warnings": warnings,
            "notation": analysis["notation"],
            "quality": analysis["quality"],
            "engraving": {
                key: engraving.analysis.get(key)
                for key in ("available", "page_count", "system_count", "singleton_systems")
            },
            "page_images": page_images,
            "artifacts": {
                "musicxml": str(xml_path),
                "pdf": str(pdf_path) if pdf_path else None,
                "roundtrip_midi": str(roundtrip_path),
            },
        }
    )
    report_path = output_dir / "roundtrip-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["note_level"], ensure_ascii=False))
    if "audio_level" in report:
        print(json.dumps(report["audio_level"], ensure_ascii=False))
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
