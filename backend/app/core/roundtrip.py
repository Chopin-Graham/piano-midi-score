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
    pitch starts within ``onset_tolerance``.  A constant timeline shift is
    estimated and removed first: deliberate reframings (for example turning
    the opening bar into a pickup) move every onset by the same amount, which
    is a notation decision, not a fidelity loss.  Duration agreement is
    reported separately for matched pairs.
    """

    offset = _estimate_global_offset(reference, candidate, onset_tolerance)
    if offset:
        candidate = [
            NoteEvent(note.pitch, note.onset + offset, note.offset + offset, note.velocity)
            for note in candidate
        ]

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
        "global_offset_removed": round(offset, 4),
    }


def _estimate_global_offset(
    reference: list[NoteEvent],
    candidate: list[NoteEvent],
    tolerance: float,
    *,
    max_offset: float = 4.0,
    step: float = 0.02,
) -> float:
    """Constant shift (in the comparison unit) that best overlaps the onsets.

    Periodic music aligns at many whole-beat shifts, so ties resolve to the
    smallest absolute offset.  Pitch-blind matching is deliberate here: the
    global rhythmic structure dominates, and the chosen shift is re-applied
    before the strict pitch-constrained pass.
    """

    if not reference or not candidate:
        return 0.0
    reference_onsets = sorted(note.onset for note in reference)
    candidate_onsets = sorted(note.onset for note in candidate)
    span = max(reference_onsets[-1], candidate_onsets[-1])
    limit = min(max_offset, span * 0.25)

    def overlap(shift: float) -> int:
        count = 0
        index = 0
        for onset in candidate_onsets:
            moved = onset + shift
            while index < len(reference_onsets) and reference_onsets[index] < moved - tolerance:
                index += 1
            probe = index
            while probe < len(reference_onsets) and reference_onsets[probe] <= moved + tolerance:
                probe += 1
            if probe > index:
                count += 1
        return count

    best_shift = 0.0
    best_count = overlap(0.0)
    steps = int(limit / step)
    for k in range(1, steps + 1):
        for shift in (k * step, -k * step):
            count = overlap(shift)
            if count > best_count:
                best_count = count
                best_shift = shift
    return best_shift


def musicxml_to_midi_bytes(musicxml: str, executable: Path) -> bytes:
    """Convert MusicXML to MIDI through MuseScore's own importer/exporter."""

    with tempfile.TemporaryDirectory(prefix="piano-xml-to-midi-") as temporary:
        workdir = Path(temporary)
        xml_path = workdir / "input.musicxml"
        midi_path = workdir / "output.mid"
        xml_path.write_text(musicxml, encoding="utf-8")
        musescore_convert(executable, xml_path, midi_path)
        return midi_path.read_bytes()


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
    reference = midi_note_events(source_midi, in_beats=in_beats)
    candidate = midi_note_events(roundtrip_midi, in_beats=in_beats)
    report: dict[str, object] = {
        "note_level": compare_note_events(
            reference,
            candidate,
            onset_tolerance=onset_tolerance,
        ),
        # A one-grid-cell displacement is a deliberate quantization decision,
        # not a lost note; the ladder keeps both views visible.
        "f1_ladder": {
            str(tolerance): compare_note_events(
                reference,
                candidate,
                onset_tolerance=tolerance,
            )["onset_f1"]
            for tolerance in (0.06, 0.12, 0.26)
            if abs(tolerance - onset_tolerance) > 1e-9
        },
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
