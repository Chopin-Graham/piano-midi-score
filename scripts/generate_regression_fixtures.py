from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

import mido

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TICKS_PER_BEAT = 480
MEASURE_TICKS = TICKS_PER_BEAT * 4


@dataclass(frozen=True, slots=True)
class NoteEvent:
    onset: int
    duration: int
    pitch: int
    velocity: int


def main() -> None:
    output = PROJECT_ROOT / "artifacts" / "regression-expressive-piano.mid"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(build_expressive_piano_midi())
    print(output)


def build_expressive_piano_midi() -> bytes:
    """Create a deterministic, dense piano performance with known hand ranges.

    The fixture intentionally mixes both hands in one MIDI track, includes held
    melody notes over moving inner voices, sixteenth-note figures, pedal, small
    human timing offsets, and occasional near-middle-C crossings. It is difficult
    enough to expose staff/voice/layout regressions while remaining musically
    unambiguous to a pianist.
    """

    rng = random.Random(20260815)
    midi = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)
    meta = mido.MidiTrack()
    piano = mido.MidiTrack()
    midi.tracks.extend([meta, piano])

    meta.append(mido.MetaMessage("track_name", name="Expressive Piano Regression", time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(140), time=0))
    meta.append(
        mido.MetaMessage(
            "time_signature",
            numerator=4,
            denominator=4,
            clocks_per_click=24,
            notated_32nd_notes_per_beat=8,
            time=0,
        )
    )
    meta.append(mido.MetaMessage("key_signature", key="Eb", time=0))
    piano.append(mido.MetaMessage("track_name", name="Piano", time=0))

    harmony = [
        (39, (51, 55, 58)),  # E-flat major
        (34, (46, 50, 53)),  # B-flat major
        (36, (48, 51, 55)),  # C minor
        (32, (44, 48, 51)),  # A-flat major
    ]
    melody = [75, 77, 79, 82, 80, 79, 77, 75, 74, 75, 77, 79, 82, 84, 82, 79]
    notes: list[NoteEvent] = []
    pedals: list[tuple[int, int]] = []

    for measure in range(12):
        measure_start = measure * MEASURE_TICKS
        bass, chord = harmony[measure % len(harmony)]

        # Left-hand bass and an eighth-note broken-chord layer. The uppermost
        # left-hand notes approach middle C but stay playable and continuous.
        for half in range(2):
            onset = measure_start + half * 2 * TICKS_PER_BEAT
            notes.append(_human_note(rng, onset, 920, bass + (12 if half else 0), 63))
        left_pattern = (chord[0], chord[1], chord[2], chord[1]) * 2
        for index, pitch in enumerate(left_pattern):
            onset = measure_start + index * (TICKS_PER_BEAT // 2)
            notes.append(_human_note(rng, onset, 210, pitch, 58 + index % 3 * 3))

        # Right-hand inner voice: compact sixteenths, with chord tones selected
        # well above the bass-staff range.
        upper_chord = tuple(pitch + 24 for pitch in chord)
        for index in range(16):
            onset = measure_start + index * (TICKS_PER_BEAT // 4)
            pitch = upper_chord[(index + measure) % len(upper_chord)]
            if index in {3, 7, 11, 15}:
                pitch += 3
            notes.append(_human_note(rng, onset, 104, pitch, 70 + index % 4 * 3))

        # Sustained melody over the moving inner voice creates a legitimate
        # second voice that must not be truncated or silently discarded.
        melody_a = melody[(measure * 2) % len(melody)]
        melody_b = melody[(measure * 2 + 1) % len(melody)]
        notes.append(_human_note(rng, measure_start, 930, melody_a, 91))
        notes.append(_human_note(rng, measure_start + 2 * TICKS_PER_BEAT, 930, melody_b, 94))

        # Cadential chords add realistic density without exceeding a pianist's
        # practical hand span.
        if measure % 4 == 3:
            cadence_onset = measure_start + 3 * TICKS_PER_BEAT
            for pitch in (upper_chord[0], upper_chord[1], upper_chord[2], upper_chord[0] + 12):
                notes.append(_human_note(rng, cadence_onset, 430, pitch, 86))

        pedals.append((measure_start, 96))
        pedals.append((measure_start + MEASURE_TICKS - 50, 0))

    _append_timed_events(piano, notes, pedals)
    buffer = __import__("io").BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def _human_note(
    rng: random.Random,
    onset: int,
    duration: int,
    pitch: int,
    velocity: int,
) -> NoteEvent:
    offset = rng.randint(-9, 9) if onset else 0
    duration_offset = rng.randint(-8, 8)
    return NoteEvent(
        onset=max(0, onset + offset),
        duration=max(45, duration + duration_offset),
        pitch=pitch,
        velocity=max(1, min(127, velocity + rng.randint(-3, 3))),
    )


def _append_timed_events(
    track: mido.MidiTrack,
    notes: list[NoteEvent],
    pedals: list[tuple[int, int]],
) -> None:
    events: list[tuple[int, int, mido.Message]] = []
    for note in notes:
        events.append(
            (
                note.onset,
                2,
                mido.Message("note_on", note=note.pitch, velocity=note.velocity, channel=0),
            )
        )
        events.append(
            (
                note.onset + note.duration,
                1,
                mido.Message("note_off", note=note.pitch, velocity=0, channel=0),
            )
        )
    for tick, value in pedals:
        events.append(
            (
                tick,
                0,
                mido.Message("control_change", control=64, value=value, channel=0),
            )
        )
    events.sort(key=lambda item: (item[0], item[1], getattr(item[2], "note", -1)))

    previous = 0
    for tick, _, message in events:
        message.time = tick - previous
        track.append(message)
        previous = tick


if __name__ == "__main__":
    main()
