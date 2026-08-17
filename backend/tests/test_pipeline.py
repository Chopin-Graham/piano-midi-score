from xml.etree import ElementTree as ET

from app.core.options import ConversionOptions
from app.core.pipeline import _title_from_midi, convert_midi

from .midi_factory import (
    compound_piano_midi_bytes,
    key_change_piano_midi_bytes,
    piano_midi_bytes,
)


def test_pipeline_produces_deterministic_two_staff_musicxml() -> None:
    data = piano_midi_bytes(two_tracks=True, jitter=9, measures=2)
    options = ConversionOptions(style="balanced")

    xml_one, analysis_one, warnings_one = convert_midi(data, "etude.mid", options)
    xml_two, analysis_two, warnings_two = convert_midi(data, "etude.mid", options)
    root = ET.fromstring(xml_one)

    assert xml_one == xml_two
    assert warnings_one == warnings_two
    assert analysis_one["note_count"] == analysis_two["note_count"]
    assert root.tag == "score-partwise"
    assert root.findtext("./part/measure/attributes/staves") == "2"
    assert len(root.findall("./part/measure")) == analysis_one["measure_count"]
    assert root.findall(".//clef[@number='1']")
    assert root.findall(".//clef[@number='2']")
    assert all(int(node.text or "0") > 0 for node in root.findall(".//note/duration"))


def test_pipeline_writes_pedal_as_direction_not_note_length() -> None:
    xml, _, _ = convert_midi(
        piano_midi_bytes(two_tracks=True, measures=1, include_pedal=True),
        "pedal.mid",
        ConversionOptions(include_pedal=True),
    )
    root = ET.fromstring(xml)

    assert len(root.findall(".//direction-type/pedal")) == 2
    assert max(int(node.text or "0") for node in root.findall(".//note/duration")) <= 1920


def test_pipeline_writes_valid_compound_meter_voice_lengths() -> None:
    xml, analysis, _ = convert_midi(
        compound_piano_midi_bytes(measures=2),
        "compound.mid",
        ConversionOptions(style="balanced", allow_triplets=True),
    )
    root = ET.fromstring(xml)

    assert analysis["meter"] == "6/8"
    assert not root.findall(".//time-modification")
    for measure in root.findall("./part/measure"):
        sequence_totals: list[int] = []
        running = 0
        for child in measure:
            if child.tag == "backup":
                sequence_totals.append(running)
                running = 0
            elif child.tag == "note" and child.find("chord") is None:
                running += int(child.findtext("duration", "0"))
        sequence_totals.append(running)
        assert sequence_totals
        assert all(total == 1440 for total in sequence_totals)


def test_pipeline_preserves_explicit_midi_key_changes() -> None:
    xml, analysis, _ = convert_midi(
        key_change_piano_midi_bytes(),
        "modulating.mid",
        ConversionOptions(),
    )
    root = ET.fromstring(xml)

    fifths = [int(node.text or "0") for node in root.findall("./part/measure/attributes/key/fifths")]
    assert fifths == [0, -4]
    assert [change["measure"] for change in analysis["key_signatures"]] == [1, 3]


def test_placeholder_track_title_falls_back_to_clean_filename() -> None:
    title = _title_from_midi(
        {0: "<Title>", 1: "Piano"},
        "Fatal__GEMN_-_Oshi_no_Ko_OP2 (1).mid",
    )

    assert title == "Fatal GEMN - Oshi no Ko OP2"


def test_corrupted_track_title_falls_back_to_filename() -> None:
    title = _title_from_midi(
        {0: "é□¢Ç□´", 1: "Piano"},
        "Call_of_Silence__Hiroyuki_Sawano.mid",
    )

    assert title == "Call of Silence Hiroyuki Sawano"


def test_null_terminated_generic_title_falls_back_to_filename() -> None:
    title = _title_from_midi(
        {0: "Piano\x00", 1: "track\x00"},
        "The_Crave.mid",
    )

    assert title == "The Crave"


def test_long_title_uses_smaller_credit_type() -> None:
    xml, _, _ = convert_midi(
        piano_midi_bytes(two_tracks=True, measures=1),
        "long.mid",
        ConversionOptions(
            title="Animenz - This Game (In E flat Advanced Version)(modify)",
        ),
    )
    root = ET.fromstring(xml)

    assert root.find("./credit/credit-words").get("font-size") == "18"


def _late_start_midi_bytes(first_onset: int = 1440) -> bytes:
    from io import BytesIO

    import mido

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
    notes = [
        (first_onset, 240, 76),
        (first_onset + 240, 240, 77),
        (first_onset + 480, 480, 79),
        (first_onset + 1440, 480, 48),
        (first_onset + 2400, 240, 74),
    ]
    events: list[tuple[int, int, mido.Message]] = []
    for onset, duration, pitch in notes:
        events.append((onset, 1, mido.Message("note_on", note=pitch, velocity=80, channel=0)))
        events.append(
            (onset + duration, 0, mido.Message("note_off", note=pitch, velocity=0, channel=0))
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


def test_audio_transcription_reframes_leading_rests_as_pickup() -> None:
    xml, _, warnings = convert_midi(
        _late_start_midi_bytes(1440),
        "pickup.mid",
        ConversionOptions(audio_transcription=True),
    )
    root = ET.fromstring(xml)

    first_measure = root.find("./part/measure")
    assert first_measure is not None
    assert first_measure.get("implicit") == "yes"
    # The pickup bar holds exactly the final beat: one beat of content, no rests.
    first_measure_durations = [
        int(node.findtext("duration", "0"))
        for node in first_measure.findall("note")
        if node.find("chord") is None
    ]
    assert sum(first_measure_durations[:2]) == 480
    assert any("弱起" in warning for warning in warnings)


def test_regular_midi_keeps_full_first_measure_without_pickup_reframe() -> None:
    xml, _, _ = convert_midi(
        _late_start_midi_bytes(1440),
        "pickup.mid",
        ConversionOptions(),
    )
    root = ET.fromstring(xml)

    first_measure = root.find("./part/measure")
    assert first_measure is not None
    assert first_measure.get("implicit") != "yes"
