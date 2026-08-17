from __future__ import annotations

from io import BytesIO

import mido


def demo_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    right = mido.MidiTrack()
    left = mido.MidiTrack()
    midi.tracks.extend([meta, right, left])

    meta.append(mido.MetaMessage("track_name", name="Piano Demo", time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(92), time=0))
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
    meta.append(mido.MetaMessage("key_signature", key="C", time=0))
    right.append(mido.MetaMessage("track_name", name="Right Hand", time=0))
    left.append(mido.MetaMessage("track_name", name="Left Hand", time=0))

    right_notes = [
        (0, 230, 72),
        (240, 230, 76),
        (480, 470, 79),
        (960, 230, 76),
        (1200, 230, 74),
        (1440, 470, 72),
        (1920, 230, 74),
        (2160, 230, 77),
        (2400, 470, 81),
        (2880, 230, 79),
        (3120, 230, 77),
        (3360, 470, 76),
    ]
    left_notes = [
        (0, 900, 48),
        (960, 900, 43),
        (1920, 900, 45),
        (2880, 900, 41),
    ]
    _append_notes(right, right_notes, velocity=84)
    _append_notes(left, left_notes, velocity=66, pedal_end=3840)

    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def _append_notes(
    track: mido.MidiTrack,
    notes: list[tuple[int, int, int]],
    *,
    velocity: int,
    pedal_end: int | None = None,
) -> None:
    events: list[tuple[int, int, mido.Message]] = []
    for onset, duration, pitch in notes:
        events.append((onset, 1, mido.Message("note_on", note=pitch, velocity=velocity)))
        events.append(
            (onset + duration, 0, mido.Message("note_off", note=pitch, velocity=0))
        )
    if pedal_end is not None:
        events.append((0, 2, mido.Message("control_change", control=64, value=100)))
        events.append((pedal_end, 2, mido.Message("control_change", control=64, value=0)))
    events.sort(key=lambda event: (event[0], event[1]))

    previous_tick = 0
    for absolute_tick, _, message in events:
        message.time = absolute_tick - previous_tick
        track.append(message)
        previous_tick = absolute_tick

