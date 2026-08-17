from __future__ import annotations

import argparse
import json
import re
import sys
import time
from io import BytesIO
from pathlib import Path

import mido

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.core.engraver import render_a4_musicxml  # noqa: E402
from app.core.options import ConversionOptions  # noqa: E402
from app.core.pipeline import convert_midi  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate real MIDI files as piano scores")
    parser.add_argument(
        "--downloads",
        type=Path,
        default=Path.home() / "Downloads",
        help="Folder containing MIDI files",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "tmp" / "download-validation" / "pass0",
        help="Directory for reports and generated artifacts",
    )
    parser.add_argument("--render", action="store_true", help="Also render MuseScore PDF/PNG")
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
    parser.add_argument(
        "--match",
        action="append",
        default=[],
        help="Case-insensitive filename fragment; may be supplied more than once",
    )
    args = parser.parse_args()

    files = sorted(
        [*args.downloads.glob("*.mid"), *args.downloads.glob("*.midi")],
        key=lambda path: path.name.casefold(),
    )
    if args.match:
        fragments = [fragment.casefold() for fragment in args.match]
        files = [
            path
            for path in files
            if any(fragment in path.name.casefold() for fragment in fragments)
        ]
    if not files:
        raise SystemExit(f"No MIDI files found in {args.downloads}")

    args.output.mkdir(parents=True, exist_ok=True)
    xml_dir = args.output / "musicxml"
    pdf_dir = args.output / "pdf"
    preview_dir = args.output / "preview"
    xml_dir.mkdir(exist_ok=True)
    if args.render:
        pdf_dir.mkdir(exist_ok=True)
        preview_dir.mkdir(exist_ok=True)

    report_path = args.output / "report.json"
    results: list[dict[str, object]] = []
    options = ConversionOptions(
        style=args.style,
        engraving_style=args.engraving_style,
    )

    for index, path in enumerate(files, start=1):
        started = time.perf_counter()
        artifact_name = f"{index:02d}-{_safe_stem(path.stem)}"
        row: dict[str, object] = {
            "file": path.name,
            "path": str(path),
            "bytes": path.stat().st_size,
            "source": _source_profile(path),
        }
        try:
            musicxml, analysis, warnings = convert_midi(path.read_bytes(), path.name, options)
            xml_path = xml_dir / f"{artifact_name}.musicxml"
            xml_path.write_text(musicxml, encoding="utf-8")
            row.update(
                {
                    "status": "converted",
                    "analysis": analysis,
                    "warnings": warnings,
                    "musicxml": str(xml_path),
                }
            )
            if args.render:
                engraving = render_a4_musicxml(musicxml, args.engraving_style)
                row["engraving"] = engraving.analysis
                row["engraving_warnings"] = engraving.warnings
                if engraving.pdf_bytes:
                    pdf_path = pdf_dir / f"{artifact_name}.pdf"
                    pdf_path.write_bytes(engraving.pdf_bytes)
                    row["pdf"] = str(pdf_path)
                if engraving.preview_png:
                    preview_path = preview_dir / f"{artifact_name}.png"
                    preview_path.write_bytes(engraving.preview_png)
                    row["preview"] = str(preview_path)
        except Exception as exc:  # noqa: BLE001 - validation must continue to the next file
            row.update(
                {
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
        results.append(row)
        report_path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(_progress_line(index, len(files), row), flush=True)

    print(f"Report: {report_path}", flush=True)


def _source_profile(path: Path) -> dict[str, object]:
    midi = mido.MidiFile(file=BytesIO(path.read_bytes()))
    note_ons = 0
    note_tracks = 0
    percussion_notes = 0
    programs: set[tuple[int, int]] = set()
    channels: set[int] = set()
    track_names: list[str] = []

    for track in midi.tracks:
        has_notes = False
        for message in track:
            if message.is_meta and message.type == "track_name" and message.name.strip():
                track_names.append(_printable(message.name.strip()))
            elif not message.is_meta and message.type == "program_change":
                programs.add((int(message.channel), int(message.program)))
            elif not message.is_meta and message.type == "note_on" and message.velocity > 0:
                channel = int(message.channel)
                note_ons += 1
                has_notes = True
                channels.add(channel)
                if channel == 9:
                    percussion_notes += 1
        note_tracks += int(has_notes)

    melodic_programs = sorted(program for channel, program in programs if channel != 9)
    non_piano_programs = sorted(program for program in melodic_programs if not 0 <= program <= 7)
    piano_evidence = any(0 <= program <= 7 for program in melodic_programs) or any(
        re.search(r"piano|grand|钢琴|鋼琴|фортеп", name, re.IGNORECASE)
        for name in track_names
    )
    ensemble_evidence = bool(percussion_notes or non_piano_programs)
    suitability = (
        "piano"
        if piano_evidence and not ensemble_evidence
        else "ensemble"
        if ensemble_evidence
        else "unknown"
    )
    return {
        "midi_type": midi.type,
        "track_count": len(midi.tracks),
        "note_track_count": note_tracks,
        "note_on_count": note_ons,
        "channels": sorted(channels),
        "programs": melodic_programs,
        "non_piano_programs": non_piano_programs,
        "percussion_notes": percussion_notes,
        "track_names": track_names,
        "suitability": suitability,
    }


def _safe_stem(stem: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")
    return (safe or "score")[:80]


def _printable(value: str) -> str:
    return "".join(character if character.isprintable() else "?" for character in value)


def _progress_line(index: int, total: int, row: dict[str, object]) -> str:
    prefix = f"[{index:02d}/{total:02d}] {row['file']}"
    if row["status"] == "failed":
        return f"{prefix} -> FAILED: {row.get('error')} ({row['elapsed_ms']} ms)"
    analysis = row["analysis"]
    quality = analysis["quality"]
    voices = analysis["voices"]
    engraving = row.get("engraving", {})
    layout = ""
    if engraving:
        layout = (
            f" | pages={engraving.get('page_count')}"
            f" systems={engraving.get('measures_per_system')}"
        )
    return (
        f"{prefix} -> {quality['status']} notes={analysis['note_count']}"
        f" measures={analysis['measure_count']} voices={voices}"
        f" overlaps={quality['voice_overlap_count']}"
        f" misplaced={quality['extreme_staff_misplacements']}"
        f" ({row['elapsed_ms']} ms){layout}"
    )


if __name__ == "__main__":
    main()
