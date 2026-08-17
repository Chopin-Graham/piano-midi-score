from __future__ import annotations

from io import BytesIO

import mido


def piano_midi_bytes(
    *,
    two_tracks: bool = True,
    jitter: int = 0,
    measures: int = 2,
    include_pedal: bool = True,
) -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    midi.tracks.append(meta)
    meta.append(mido.MetaMessage("track_name", name="Test Piece", time=0))
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
    meta.append(mido.MetaMessage("key_signature", key="C", time=0))

    right_notes: list[tuple[int, int, int, int]] = []
    left_notes: list[tuple[int, int, int, int]] = []
    right_pattern = [72, 76, 79, 76, 74, 77, 81, 77]
    left_pattern = [48, 43, 45, 41]
    for measure in range(measures):
        base = measure * 1920
        for index, pitch in enumerate(right_pattern):
            onset = base + index * 240 + (jitter if index % 2 else -jitter)
            right_notes.append((max(0, onset), 220, pitch, 82))
        for index, pitch in enumerate(left_pattern):
            left_notes.append((base + index * 480, 430, pitch, 68))

    if two_tracks:
        right = mido.MidiTrack()
        left = mido.MidiTrack()
        midi.tracks.extend([right, left])
        right.append(mido.MetaMessage("track_name", name="Right Hand", time=0))
        left.append(mido.MetaMessage("track_name", name="Left Hand", time=0))
        _append_absolute_notes(right, right_notes, channel=0)
        if include_pedal:
            _append_absolute_notes(left, left_notes, channel=0, pedal=True)
        else:
            _append_absolute_notes(left, left_notes, channel=0)
    else:
        piano = mido.MidiTrack()
        midi.tracks.append(piano)
        piano.append(mido.MetaMessage("track_name", name="Piano", time=0))
        _append_absolute_notes(
            piano,
            right_notes + left_notes,
            channel=0,
            pedal=include_pedal,
        )

    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def dense_midi_bytes(note_count: int = 1200) -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(120), time=0))
    track.append(
        mido.MetaMessage(
            "time_signature",
            numerator=4,
            denominator=4,
            clocks_per_click=24,
            notated_32nd_notes_per_beat=8,
            time=0,
        )
    )
    notes = [
        (index * 120, 100, 48 + (index * 7) % 36, 55 + index % 40)
        for index in range(note_count)
    ]
    _append_absolute_notes(track, notes, channel=0)
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def compound_piano_midi_bytes(measures: int = 2) -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    piano = mido.MidiTrack()
    midi.tracks.extend([meta, piano])
    meta.append(mido.MetaMessage("track_name", name="Compound Etude", time=0))
    meta.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(84), time=0))
    meta.append(
        mido.MetaMessage(
            "time_signature",
            numerator=6,
            denominator=8,
            clocks_per_click=36,
            notated_32nd_notes_per_beat=8,
            time=0,
        )
    )
    piano.append(mido.MetaMessage("track_name", name="Piano", time=0))
    notes: list[tuple[int, int, int, int]] = []
    right_pattern = [72, 76, 79, 74, 77, 81]
    for measure in range(measures):
        base = measure * 1440
        notes.extend(
            (base + index * 240, 220, pitch, 82)
            for index, pitch in enumerate(right_pattern)
        )
        notes.extend(
            [
                (base, 700, 48, 68),
                (base + 720, 700, 43, 68),
            ]
        )
    _append_absolute_notes(piano, notes, channel=0)
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def key_change_piano_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    meta = mido.MidiTrack()
    right = mido.MidiTrack()
    left = mido.MidiTrack()
    midi.tracks.extend([meta, right, left])
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
    meta.append(mido.MetaMessage("key_signature", key="C", time=0))
    meta.append(mido.MetaMessage("key_signature", key="Ab", time=3840))
    right.append(mido.MetaMessage("track_name", name="Right Hand", time=0))
    left.append(mido.MetaMessage("track_name", name="Left Hand", time=0))
    right_notes = [(measure * 1920, 1440, 72 + measure % 3, 82) for measure in range(4)]
    left_notes = [(measure * 1920, 1440, 48 - measure % 3, 68) for measure in range(4)]
    _append_absolute_notes(right, right_notes, channel=0)
    _append_absolute_notes(left, left_notes, channel=0)
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def mixed_ensemble_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    events = [
        (0, 0, mido.Message("program_change", program=0, channel=0)),
        (0, 1, mido.Message("note_on", note=60, velocity=80, channel=0)),
        (0, 0, mido.Message("program_change", program=40, channel=1)),
        (0, 1, mido.Message("note_on", note=67, velocity=80, channel=1)),
        (0, 1, mido.Message("note_on", note=36, velocity=80, channel=9)),
        (240, 0, mido.Message("note_off", note=60, velocity=0, channel=0)),
        (240, 0, mido.Message("note_off", note=67, velocity=0, channel=1)),
        (240, 0, mido.Message("note_off", note=36, velocity=0, channel=9)),
    ]
    previous = 0
    for tick, _, message in sorted(events, key=lambda item: (item[0], item[1])):
        message.time = tick - previous
        track.append(message)
        previous = tick
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def dominant_ensemble_midi_bytes() -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=480)
    track = mido.MidiTrack()
    midi.tracks.append(track)
    track.append(mido.Message("program_change", program=0, channel=0, time=0))
    track.append(mido.Message("note_on", note=60, velocity=80, channel=0, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, channel=0, time=240))
    track.append(mido.Message("program_change", program=40, channel=1, time=0))
    for index in range(65):
        track.append(
            mido.Message(
                "note_on",
                note=55 + index % 12,
                velocity=70,
                channel=1,
                time=0 if index == 0 else 60,
            )
        )
        track.append(
            mido.Message(
                "note_off",
                note=55 + index % 12,
                velocity=0,
                channel=1,
                time=30,
            )
        )
    buffer = BytesIO()
    midi.save(file=buffer)
    return buffer.getvalue()


def _append_absolute_notes(
    track: mido.MidiTrack,
    notes: list[tuple[int, int, int, int]],
    *,
    channel: int,
    pedal: bool = False,
) -> None:
    events: list[tuple[int, int, mido.Message]] = []
    for onset, duration, pitch, velocity in notes:
        events.append(
            (onset, 1, mido.Message("note_on", note=pitch, velocity=velocity, channel=channel))
        )
        events.append(
            (
                onset + duration,
                0,
                mido.Message("note_off", note=pitch, velocity=0, channel=channel),
            )
        )
    if pedal and notes:
        events.append((0, 2, mido.Message("control_change", control=64, value=100, channel=channel)))
        events.append(
            (
                max(onset + duration for onset, duration, _, _ in notes),
                2,
                mido.Message("control_change", control=64, value=0, channel=channel),
            )
        )
    events.sort(key=lambda item: (item[0], item[1]))
    previous_tick = 0
    for absolute_tick, _, message in events:
        message.time = absolute_tick - previous_tick
        track.append(message)
        previous_tick = absolute_tick
