from __future__ import annotations

from io import BytesIO

import mido

from app.core.key_detection import estimate_key_timeline
from app.core.models import MeasureSpan, Meter, QuantizedNote
from app.core.pipeline import convert_midi


def _scale_notes(tonic_pc: int, measure_indexes: range, *, octave: int = 5) -> list[QuantizedNote]:
    """One strongly diatonic measure content per measure for a major scale."""

    major_steps = [0, 2, 4, 5, 7, 9, 11]
    notes: list[QuantizedNote] = []
    source = 0
    for measure_index in measure_indexes:
        base_onset = measure_index * 1920
        for order, step in enumerate(major_steps):
            source += 1
            notes.append(
                QuantizedNote(
                    source,
                    (octave * 12) + tonic_pc + step,
                    base_onset + order * 240,
                    240,
                    84,
                    0,
                    0,
                )
            )
    return notes


def _measures(count: int) -> list[MeasureSpan]:
    meter = Meter(4, 4)
    return [
        MeasureSpan(index, index * meter.measure_length, meter.measure_length, meter)
        for index in range(count)
    ]


def test_estimate_key_timeline_detects_modulation() -> None:
    notes = _scale_notes(0, range(0, 4)) + _scale_notes(6, range(4, 8))

    changes = estimate_key_timeline(notes, _measures(8))

    assert len(changes) == 2
    assert changes[0].measure_index == 0
    assert changes[0].key.fifths == 0
    assert changes[1].key.fifths == 6
    assert 3 <= changes[1].measure_index <= 5


def test_estimate_key_timeline_stable_for_single_key() -> None:
    notes = _scale_notes(0, range(0, 8))

    changes = estimate_key_timeline(notes, _measures(8))

    assert len(changes) == 1
    assert changes[0].key.fifths == 0


def test_estimate_key_timeline_handles_sparse_measures() -> None:
    notes = _scale_notes(0, range(0, 2)) + _scale_notes(6, range(6, 8))

    changes = estimate_key_timeline(notes, _measures(8))

    assert changes[0].key.fifths == 0
    assert any(change.key.fifths == 6 for change in changes[1:])


def _modulating_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    piano = mido.MidiTrack()
    midi.tracks.extend([meta, piano])
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(96), time=0))
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
    events: list[tuple[int, int, mido.Message]] = []
    for note in _scale_notes(0, range(0, 4)) + _scale_notes(6, range(4, 8)):
        events.append(
            (
                note.onset,
                1,
                mido.Message("note_on", note=note.pitch, velocity=84, channel=0, time=0),
            )
        )
        events.append(
            (
                note.onset + note.duration,
                0,
                mido.Message("note_off", note=note.pitch, velocity=0, channel=0, time=0),
            )
        )
    events.sort(key=lambda item: (item[0], item[1]))
    previous = 0
    for tick, _, message in events:
        message.time = tick - previous
        piano.append(message)
        previous = tick
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def test_pipeline_infers_key_changes_without_key_signature_events() -> None:
    musicxml, analysis, _warnings = convert_midi(
        _modulating_midi_bytes(),
        "modulating.mid",
    )

    signatures = analysis["key_signatures"]
    assert len(signatures) >= 2
    assert signatures[0]["fifths"] == 0
    assert any(signature["fifths"] == 6 for signature in signatures[1:])
    assert musicxml.count("<key>") >= 2
