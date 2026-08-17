"""Round-trip evaluation: score notation fidelity measured against the source.

The pipeline turns a performance MIDI into engraved MusicXML/PDF.  This module
closes the loop: the MusicXML is converted back to MIDI through MuseScore (so
the comparison reflects what a real notation program understood), and the
resulting note stream is compared with the source, both directly at note level
and perceptually through synthesized-audio chroma similarity.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import mido


@dataclass(frozen=True, slots=True)
class NoteEvent:
    pitch: int
    onset: float
    offset: float
    velocity: int

    @property
    def duration(self) -> float:
        return self.offset - self.onset


def midi_note_events(data: bytes, *, in_beats: bool = False) -> list[NoteEvent]:
    """Extract absolute-time note events, honoring the MIDI tempo map.

    With ``in_beats=True`` positions are reported in quarter-note beats instead
    of seconds.  Beat domain is the right basis for engraving round-trip
    comparisons: the engraved score carries its own (rounded) metronome mark,
    so a second-based comparison accumulates drift from the tempo difference
    alone even when every rhythm is notated correctly.
    """

    midi = mido.MidiFile(file=BytesIO(data))
    tempo = 500_000
    seconds = 0.0
    ticks = 0
    active: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
    events: list[NoteEvent] = []
    for message in mido.merge_tracks(midi.tracks):
        seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        ticks += message.time
        position = ticks / midi.ticks_per_beat if in_beats else seconds
        if message.is_meta:
            if message.type == "set_tempo":
                tempo = int(message.tempo)
            continue
        if message.type == "note_on" and message.velocity > 0:
            active.setdefault((message.channel, message.note), []).append(
                (position, seconds, int(message.velocity))
            )
        elif message.type in {"note_off", "note_on"}:
            stack = active.get((message.channel, message.note), [])
            if stack:
                start, _start_seconds, velocity = stack.pop(0)
                if position > start:
                    events.append(NoteEvent(message.note, start, position, velocity))
    return sorted(events, key=lambda event: (event.onset, event.pitch))


def compare_note_events(
    reference: list[NoteEvent],
    candidate: list[NoteEvent],
    *,
    onset_tolerance: float = 0.1,
) -> dict[str, object]:
    """Greedy onset/pitch matching, mir_eval style.

    A candidate note is a hit when an unmatched reference note with the same
    pitch starts within ``onset_tolerance`` seconds.  Duration agreement is
    reported separately for matched pairs so timing and release quality stay
    visible as independent signals.
    """

    remaining = sorted(reference, key=lambda event: (event.onset, event.pitch))
    hits = 0
    duration_errors: list[float] = []
    onset_errors: list[float] = []
    for note in sorted(candidate, key=lambda event: (event.onset, event.pitch)):
        best_index = None
        best_gap = onset_tolerance
        for index, reference_note in enumerate(remaining):
            if reference_note.pitch != note.pitch:
                continue
            gap = abs(reference_note.onset - note.onset)
            if gap <= best_gap:
                best_gap = gap
                best_index = index
        if best_index is not None:
            matched = remaining.pop(best_index)
            hits += 1
            onset_errors.append(abs(matched.onset - note.onset))
            duration_errors.append(abs(matched.duration - note.duration))

    missed = len(remaining)
    extra = len(candidate) - hits
    precision = hits / len(candidate) if candidate else 0.0
    recall = hits / len(reference) if reference else 0.0
    f1 = (
        2 * precision * recall / (precision + recall) if precision + recall else 0.0
    )

    reference_chroma = [0.0] * 12
    candidate_chroma = [0.0] * 12
    for note in reference:
        reference_chroma[note.pitch % 12] += note.duration
    for note in candidate:
        candidate_chroma[note.pitch % 12] += note.duration
    reference_total = sum(reference_chroma) or 1.0
    candidate_total = sum(candidate_chroma) or 1.0
    chroma_l1 = sum(
        abs(a / reference_total - b / candidate_total)
        for a, b in zip(reference_chroma, candidate_chroma, strict=True)
    )

    return {
        "reference_notes": len(reference),
        "candidate_notes": len(candidate),
        "matched": hits,
        "missed": missed,
        "extra": extra,
        "onset_precision": round(precision, 4),
        "onset_recall": round(recall, 4),
        "onset_f1": round(f1, 4),
        "mean_onset_error": (
            round(sum(onset_errors) / len(onset_errors), 5) if onset_errors else None
        ),
        "mean_duration_error": (
            round(sum(duration_errors) / len(duration_errors), 5)
            if duration_errors
            else None
        ),
        "chroma_histogram_l1": round(chroma_l1, 4),
        "onset_tolerance": onset_tolerance,
    }


def musescore_convert(
    executable: Path,
    input_path: Path,
    output_path: Path,
    *,
    timeout: int = 300,
) -> None:
    completed = subprocess.run(
        [str(executable), "-o", str(output_path), str(input_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not output_path.is_file():
        detail = (completed.stderr or completed.stdout or "unknown MuseScore error").strip()
        raise RuntimeError(f"MuseScore conversion failed: {detail[:400]}")


def audio_similarity(
    audio_python: Path,
    worker_path: Path,
    reference_wav: Path,
    candidate_wav: Path,
    workdir: Path,
    *,
    timeout: int = 300,
) -> dict[str, object]:
    """Chroma/onset-envelope similarity between two renderings, via librosa."""

    output = workdir / "audio-similarity.json"
    completed = subprocess.run(
        [
            str(audio_python),
            str(worker_path),
            "similarity",
            str(reference_wav),
            str(candidate_wav),
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not output.is_file():
        detail = (completed.stderr or completed.stdout or "similarity worker failed").strip()
        raise RuntimeError(detail[-600:])
    return json.loads(output.read_text(encoding="utf-8"))


def render_pdf_pages(
    pdf_path: Path,
    output_prefix: Path,
    *,
    dpi: int = 150,
    pdftoppm: str = "pdftoppm",
    timeout: int = 300,
) -> list[Path]:
    completed = subprocess.run(
        [pdftoppm, "-png", "-r", str(dpi), str(pdf_path), str(output_prefix)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "pdftoppm failed").strip()
        raise RuntimeError(detail[:400])
    return sorted(output_prefix.parent.glob(f"{output_prefix.name}-*.png"))


def evaluate_roundtrip(
    source_midi: bytes,
    roundtrip_midi: bytes,
    *,
    onset_tolerance: float = 0.12,
    in_beats: bool = True,
    audio_similarity_fn: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    report: dict[str, object] = {
        "note_level": compare_note_events(
            midi_note_events(source_midi, in_beats=in_beats),
            midi_note_events(roundtrip_midi, in_beats=in_beats),
            onset_tolerance=onset_tolerance,
        ),
        "comparison_units": "beats" if in_beats else "seconds",
    }
    if audio_similarity_fn is not None:
        try:
            report["audio_level"] = audio_similarity_fn()
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            report["audio_level"] = {"error": str(exc)[:300]}
    return report


def temporary_directory(prefix: str = "piano-roundtrip-"):
    return tempfile.TemporaryDirectory(prefix=prefix)
