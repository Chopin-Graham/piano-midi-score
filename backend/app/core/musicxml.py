from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from functools import cache
from statistics import median
from xml.etree import ElementTree as ET

from .meter_map import measure_index_at
from .models import (
    CANONICAL_DIVISIONS,
    ClefChange,
    DynamicMark,
    Hand,
    KeyEstimate,
    Meter,
    QuantizedNote,
    ScoreModel,
    Staff,
)
from .ottava import OttavaSpan, detect_ottava_spans


@dataclass(frozen=True, slots=True)
class DurationSpec:
    value: int
    note_type: str
    dots: int = 0
    time_modification: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class NotationNote:
    pitch: int
    onset: int
    duration: int
    velocity: int
    staff: Staff
    voice: int
    tie_stop: bool
    tie_start: bool
    pitch_step: str | None = None
    pitch_alter: int = 0
    pitch_octave: int | None = None
    hand: Hand | None = None
    arpeggiated: bool = False
    trill: bool = False
    grace: bool = False
    staccato: bool = False
    tremolo_start: bool = False
    tremolo_stop: bool = False


@dataclass(slots=True)
class VoiceItem:
    onset: int
    duration: int
    spec: DurationSpec
    notes: list[NotationNote]
    is_rest: bool = False
    measure_rest: bool = False
    beam: str | None = None
    tuplet_start: bool = False
    tuplet_stop: bool = False
    in_tuplet: bool = False
    boundary_rest: bool = False
    hidden_rest: bool = False
    grace_notes: list[NotationNote] = field(default_factory=list)


DURATION_SPECS = [
    DurationSpec(1920, "whole"),
    DurationSpec(1440, "half", 1),
    DurationSpec(960, "half"),
    DurationSpec(720, "quarter", 1),
    DurationSpec(480, "quarter"),
    DurationSpec(384, "quarter", 0, (5, 4)),
    DurationSpec(360, "eighth", 1),
    DurationSpec(320, "quarter", 0, (3, 2)),
    DurationSpec(288, "eighth", 1, (5, 4)),
    DurationSpec(240, "eighth"),
    DurationSpec(192, "eighth", 0, (5, 4)),
    DurationSpec(180, "16th", 1),
    DurationSpec(160, "eighth", 0, (3, 2)),
    DurationSpec(144, "16th", 1, (5, 4)),
    DurationSpec(120, "16th"),
    DurationSpec(96, "16th", 0, (5, 4)),
    DurationSpec(90, "32nd", 1),
    DurationSpec(80, "16th", 0, (6, 4)),
    DurationSpec(60, "32nd"),
    DurationSpec(48, "32nd", 0, (5, 4)),
    DurationSpec(40, "32nd", 0, (6, 4)),
    DurationSpec(30, "64th"),
]
SPEC_BY_VALUE = {spec.value: spec for spec in DURATION_SPECS}

# Specs without a time ratio: the only safe choices for decomposing an
# arbitrary span.  Picking a tuplet spec for an unrelated binary duration
# prints a ratio where none belongs (a "5:4 quarter" rest for 400 ticks) and
# leaves unrepresentable remainders behind.
BINARY_SPECS = [spec for spec in DURATION_SPECS if spec.time_modification is None]

# Tuplet member tables, by (actual, normal) ratio.  Every entry satisfies the
# MusicXML consistency rule: duration == nominal(type, dots) * normal / actual.
# A group is bracketed only when its members' real durations sum to exactly
# span = first_member_duration * actual; groups that cannot be completed keep
# their exact (duration, type, time-modification) triplets without a bracket,
# which stays loadable instead of corrupting the measure with a type whose
# nominal length disagrees with the written duration.
TUPLET_MEMBER_SPECS: dict[tuple[int, int], dict[int, DurationSpec]] = {
    (3, 2): {
        80: DurationSpec(80, "16th", 0, (3, 2)),
        160: DurationSpec(160, "eighth", 0, (3, 2)),
        240: DurationSpec(240, "eighth", 1, (3, 2)),
        320: DurationSpec(320, "quarter", 0, (3, 2)),
    },
    (6, 4): {
        40: DurationSpec(40, "32nd", 0, (6, 4)),
        80: DurationSpec(80, "16th", 0, (6, 4)),
        160: DurationSpec(160, "eighth", 0, (6, 4)),
        240: DurationSpec(240, "eighth", 1, (6, 4)),
    },
    (5, 4): {
        48: DurationSpec(48, "32nd", 0, (5, 4)),
        96: DurationSpec(96, "16th", 0, (5, 4)),
        144: DurationSpec(144, "16th", 1, (5, 4)),
        192: DurationSpec(192, "eighth", 0, (5, 4)),
        288: DurationSpec(288, "eighth", 1, (5, 4)),
    },
}

SHARP_PITCHES = {
    0: ("C", 0),
    1: ("C", 1),
    2: ("D", 0),
    3: ("D", 1),
    4: ("E", 0),
    5: ("F", 0),
    6: ("F", 1),
    7: ("G", 0),
    8: ("G", 1),
    9: ("A", 0),
    10: ("A", 1),
    11: ("B", 0),
}
FLAT_PITCHES = {
    0: ("C", 0),
    1: ("D", -1),
    2: ("D", 0),
    3: ("E", -1),
    4: ("E", 0),
    5: ("F", 0),
    6: ("G", -1),
    7: ("G", 0),
    8: ("A", -1),
    9: ("A", 0),
    10: ("B", -1),
    11: ("B", 0),
}


def score_to_musicxml(score: ScoreModel) -> str:
    root = ET.Element("score-partwise", version="4.0")
    _add_work_and_identification(root, score)
    _add_defaults(root, score.engraving_style)
    _add_credit(root, score.title, score.engraving_style)
    _add_part_list(root)

    part = ET.SubElement(root, "part", id="P1")
    ottava_spans = detect_ottava_spans(score.notes)
    atoms = _notation_atoms(score.notes, score, ottava_spans)
    atoms_by_location: dict[tuple[int, Staff, int], list[NotationNote]] = defaultdict(list)
    for atom in atoms:
        measure_index = measure_index_at(score.measures, atom.onset)
        atoms_by_location[(measure_index, atom.staff, atom.voice)].append(atom)

    # MuseScore already has a mature spacing and collision engine.  Earlier
    # versions injected estimated system breaks here, but real reference scores
    # showed that those hints could collide with MuseScore's own automatic
    # breaks and leave one-measure systems.  Keep the MusicXML semantic and let
    # the engraver decide horizontal layout from the final glyph geometry.
    break_before: set[int] = set()
    pedal_by_measure = _pedals_by_measure(score)
    dynamics_by_measure: dict[int, list[DynamicMark]] = defaultdict(list)
    for mark in score.dynamics:
        dynamics_by_measure[mark.measure_index].append(mark)
    clefs_by_measure: dict[int, list[ClefChange]] = defaultdict(list)
    for clef_change in score.clef_changes:
        clefs_by_measure[clef_change.measure_index].append(clef_change)
    ottava_by_measure = _ottava_directions_by_measure(
        score,
        ottava_spans,
    )
    keys_by_measure = {
        change.measure_index: change.key for change in score.key_changes
    }
    tempos_by_measure = _tempos_by_measure(score)
    tempo_texts_by_measure: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for tempo_text in score.tempo_texts:
        text_measure, text_offset = _score_time_location(
            score, min(tempo_text.tick, score.measures[-1].end)
        )
        tempo_texts_by_measure[text_measure].append((text_offset, tempo_text.text))

    previous_meter: Meter | None = None
    current_key = score.key
    for measure_index, measure_span in enumerate(score.measures):
        measure_attributes = {"number": str(measure_index + 1)}
        if measure_span.implicit:
            measure_attributes["implicit"] = "yes"
        measure = ET.SubElement(part, "measure", measure_attributes)
        if measure_index == 0 or measure_index in break_before:
            print_element = ET.SubElement(measure, "print")
            if measure_index in break_before:
                print_element.set("new-system", "yes")
            if measure_index == 0:
                _add_first_measure_layout(print_element)

        meter_changed = measure_index == 0 or measure_span.meter != previous_meter
        clef_changes = clefs_by_measure.get(measure_index, [])
        boundary_clef_changes = [change for change in clef_changes if change.offset == 0]
        positioned_clef_changes = [change for change in clef_changes if change.offset > 0]
        key_changed = measure_index in keys_by_measure
        if key_changed:
            current_key = keys_by_measure[measure_index]
        if meter_changed or boundary_clef_changes or key_changed:
            _add_attributes(
                measure,
                current_key,
                measure_span.meter,
                include_staff_setup=measure_index == 0,
                include_time=meter_changed,
                include_key=measure_index == 0 or key_changed,
                clef_changes=boundary_clef_changes,
            )
        for tempo_offset, tempo_bpm, tempo_visible in tempos_by_measure.get(measure_index, []):
            _add_tempo(measure, tempo_bpm, offset=tempo_offset, visible=tempo_visible)
        for text_offset, tempo_text in tempo_texts_by_measure.get(measure_index, []):
            _add_tempo_text(measure, tempo_text, offset=text_offset)
        previous_meter = measure_span.meter

        for offset, down in pedal_by_measure.get(measure_index, []):
            _add_pedal(measure, offset, down)

        for dynamic_mark in dynamics_by_measure.get(measure_index, []):
            _add_dynamic(measure, dynamic_mark)

        _add_positioned_octave_shifts(
            measure,
            ottava_by_measure.get(measure_index, []),
        )
        _add_positioned_clef_changes(measure, positioned_clef_changes)

        sequences: list[tuple[Staff, int, list[VoiceItem]]] = []
        for staff in (Staff.RIGHT, Staff.LEFT):
            present_voices = sorted(
                {
                    voice
                    for current_measure, current_staff, voice in atoms_by_location
                    if current_measure == measure_index and current_staff == staff
                }
            )
            if not present_voices:
                present_voices = [1]
            staff_sequences: list[tuple[Staff, int, list[VoiceItem]]] = []
            for voice in present_voices:
                voice_atoms = atoms_by_location.get((measure_index, staff, voice), [])
                staff_sequences.append(
                    (
                        staff,
                        voice,
                        _voice_items(
                            voice_atoms,
                            measure_span.start,
                            measure_span.duration,
                            measure_span.meter,
                        ),
                    )
                )
            _mark_hidden_padding_rests(
                staff_sequences,
                measure_span.duration,
                measure_span.meter,
            )
            sequences.extend(staff_sequences)

        for sequence_index, (staff, voice, items) in enumerate(sequences):
            if sequence_index:
                backup = ET.SubElement(measure, "backup")
                ET.SubElement(backup, "duration").text = str(measure_span.duration)
            staff_sequences = [item for item in sequences if item[0] == staff]
            stem_directions = _stem_directions(staff_sequences)
            pending_skip = 0
            for item in items:
                # Voice padding covered by other voices is a time skip, not a
                # printed symbol.  `<forward>` keeps the voice's time accounting
                # exact without leaving invisible (gray-on-screen) rests in the
                # editor.  Tuplet-member rests must stay literal: a skip inside
                # a bracket breaks the tuplet ratio.
                # Rests whose duration has no exact note-type spec (sub-grid
                # cracks left where two beat-group grids meet) also become
                # skips: printing them would require a fake type whose nominal
                # length disagrees with the written duration, which importers
                # report as a corrupt measure.
                if (
                    item.is_rest
                    and not item.measure_rest
                    and item.duration not in SPEC_BY_VALUE
                ):
                    pending_skip += item.duration
                    continue
                # A ratio-marked rest outside any completed bracket hangs
                # MuseScore's importer; as pure padding it is a time skip.
                if item.is_rest and item.spec.time_modification and not item.in_tuplet:
                    pending_skip += item.duration
                    continue
                if item.is_rest and item.hidden_rest and not item.spec.time_modification:
                    pending_skip += item.duration
                    continue
                if pending_skip:
                    _write_forward(measure, pending_skip, staff, voice)
                    pending_skip = 0
                _write_item(
                    measure,
                    item,
                    staff,
                    voice,
                    current_key.fifths,
                    stem_directions.get(voice),
                )
            if pending_skip:
                _write_forward(measure, pending_skip, staff, voice)

        if measure_index == len(score.measures) - 1:
            barline = ET.SubElement(measure, "barline", location="right")
            ET.SubElement(barline, "bar-style").text = "light-heavy"

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=True)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?>\n'
        '<!DOCTYPE score-partwise PUBLIC "-//Recordare//DTD MusicXML 4.0 Partwise//EN" '
        '"http://www.musicxml.org/dtds/partwise.dtd">\n'
        + body
    )


def _add_work_and_identification(root: ET.Element, score: ScoreModel) -> None:
    work = ET.SubElement(root, "work")
    ET.SubElement(work, "work-title").text = score.title
    identification = ET.SubElement(root, "identification")
    encoding = ET.SubElement(identification, "encoding")
    ET.SubElement(encoding, "software").text = "Piano MIDI Score 0.1.0"
    supports = ET.SubElement(encoding, "supports", element="print", type="yes")
    supports.set("attribute", "new-system")
    supports.set("value", "yes")


PAGE_GEOMETRY = {
    "classic": {
        "millimeters": "6.6",
        "width": "1273",
        "height": "1800",
        "side_margin": "73",
        "vertical_margin": "72",
        "credit_x": "636.5",
        "credit_y": "1718",
    },
    "modern": {
        "millimeters": "7.0",
        "width": "1200",
        "height": "1697",
        "side_margin": "80",
        "vertical_margin": "72",
        "credit_x": "600",
        "credit_y": "1620",
    },
    "compact": {
        "millimeters": "6.2",
        "width": "1355",
        "height": "1916",
        "side_margin": "72",
        "vertical_margin": "76",
        "credit_x": "677.5",
        "credit_y": "1829",
    },
}


def _page_geometry(engraving_style: str) -> dict[str, str]:
    return PAGE_GEOMETRY.get(engraving_style, PAGE_GEOMETRY["classic"])


def _add_defaults(root: ET.Element, engraving_style: str) -> None:
    geometry = _page_geometry(engraving_style)
    defaults = ET.SubElement(root, "defaults")
    scaling = ET.SubElement(defaults, "scaling")
    # MusicXML scaling overrides MuseScore's style-file Spatium.  Classic uses
    # a professional 1.65 mm piano-score staff; compact remains a genuine
    # 1.55 mm performer edition rather than a margin-only variant.
    ET.SubElement(scaling, "millimeters").text = geometry["millimeters"]
    ET.SubElement(scaling, "tenths").text = "40"
    page_layout = ET.SubElement(defaults, "page-layout")
    ET.SubElement(page_layout, "page-height").text = geometry["height"]
    ET.SubElement(page_layout, "page-width").text = geometry["width"]
    for page_type in ("odd", "even"):
        margins = ET.SubElement(page_layout, "page-margins", type=page_type)
        ET.SubElement(margins, "left-margin").text = geometry["side_margin"]
        ET.SubElement(margins, "right-margin").text = geometry["side_margin"]
        ET.SubElement(margins, "top-margin").text = geometry["vertical_margin"]
        ET.SubElement(margins, "bottom-margin").text = geometry["vertical_margin"]
    system_layout = ET.SubElement(defaults, "system-layout")
    system_margins = ET.SubElement(system_layout, "system-margins")
    ET.SubElement(system_margins, "left-margin").text = "0"
    ET.SubElement(system_margins, "right-margin").text = "0"
    ET.SubElement(system_layout, "system-distance").text = "120"
    ET.SubElement(system_layout, "top-system-distance").text = "90"
    appearance = ET.SubElement(defaults, "appearance")
    for line_type, width in (
        ("staff", "1.0"),
        ("stem", "0.8"),
        ("beam", "5.0"),
        ("light barline", "1.2"),
        ("heavy barline", "5.0"),
    ):
        ET.SubElement(appearance, "line-width", type=line_type).text = width
def _add_credit(root: ET.Element, title: str, engraving_style: str) -> None:
    geometry = _page_geometry(engraving_style)
    if len(title) <= 42:
        font_size = "22"
    elif len(title) <= 64:
        font_size = "18"
    else:
        font_size = "16"
    credit = ET.SubElement(root, "credit", page="1")
    ET.SubElement(credit, "credit-type").text = "title"
    ET.SubElement(
        credit,
        "credit-words",
        {
            "default-x": geometry["credit_x"],
            "default-y": geometry["credit_y"],
            "justify": "center",
            "valign": "top",
            "font-size": font_size,
        },
    ).text = title


def _add_part_list(root: ET.Element) -> None:
    part_list = ET.SubElement(root, "part-list")
    score_part = ET.SubElement(part_list, "score-part", id="P1")
    ET.SubElement(score_part, "part-name").text = "Piano"
    ET.SubElement(score_part, "part-abbreviation").text = "Pno."
    instrument = ET.SubElement(score_part, "score-instrument", id="P1-I1")
    ET.SubElement(instrument, "instrument-name").text = "Acoustic Grand Piano"
    midi_instrument = ET.SubElement(score_part, "midi-instrument", id="P1-I1")
    ET.SubElement(midi_instrument, "midi-channel").text = "1"
    ET.SubElement(midi_instrument, "midi-program").text = "1"


def _add_first_measure_layout(print_element: ET.Element) -> None:
    system_layout = ET.SubElement(print_element, "system-layout")
    ET.SubElement(system_layout, "top-system-distance").text = "150"
    staff_layout = ET.SubElement(print_element, "staff-layout", number="2")
    ET.SubElement(staff_layout, "staff-distance").text = "78"


def _add_attributes(
    measure: ET.Element,
    key_signature: KeyEstimate,
    meter: Meter,
    *,
    include_staff_setup: bool,
    include_time: bool,
    include_key: bool,
    clef_changes: list[ClefChange],
) -> None:
    attributes = ET.SubElement(measure, "attributes")
    if include_staff_setup:
        ET.SubElement(attributes, "divisions").text = str(CANONICAL_DIVISIONS)
    if include_key:
        key = ET.SubElement(attributes, "key")
        ET.SubElement(key, "fifths").text = str(key_signature.fifths)
        ET.SubElement(key, "mode").text = key_signature.mode
    if include_time:
        time = ET.SubElement(attributes, "time")
        ET.SubElement(time, "beats").text = str(meter.numerator)
        ET.SubElement(time, "beat-type").text = str(meter.denominator)
    if include_staff_setup:
        ET.SubElement(attributes, "staves").text = "2"
    if include_staff_setup and not clef_changes:
        clef_changes = [
            ClefChange(0, Staff.RIGHT, "treble"),
            ClefChange(0, Staff.LEFT, "bass"),
        ]
    for change in sorted(clef_changes, key=lambda item: int(item.staff)):
        clef = ET.SubElement(attributes, "clef", number=str(int(change.staff)))
        ET.SubElement(clef, "sign").text = change.sign
        ET.SubElement(clef, "line").text = str(change.line)


def _add_tempo(measure: ET.Element, bpm: float, offset: int = 0, visible: bool = True) -> None:
    rounded = max(20, min(300, round(bpm)))
    direction = ET.SubElement(measure, "direction", placement="above")
    if visible:
        direction_type = ET.SubElement(direction, "direction-type")
        metronome = ET.SubElement(direction_type, "metronome", parentheses="no")
        ET.SubElement(metronome, "beat-unit").text = "quarter"
        ET.SubElement(metronome, "per-minute").text = str(rounded)
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "sound", tempo=str(rounded))
    ET.SubElement(direction, "staff").text = "1"


def _add_tempo_text(measure: ET.Element, text: str, offset: int = 0) -> None:
    direction = ET.SubElement(measure, "direction", placement="above")
    direction_type = ET.SubElement(direction, "direction-type")
    ET.SubElement(direction_type, "words", {"font-style": "italic"}).text = text
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "staff").text = "1"


def _tempos_by_measure(score: ScoreModel) -> dict[int, list[tuple[int, float, bool]]]:
    """Place tempo changes on the measure grid and choose printable marks.

    Every MIDI tempo event becomes a `<sound tempo>` direction so playback
    follows the original tempo map (rit./accel. included).  A visible
    metronome mark is printed only for sustained plateaus — an event that
    stays in force for at least a whole note, or a final tempo reached after
    such a span — so gradual ritardandos do not litter the page with numbers.
    """

    if not score.tempo_changes:
        return {0: [(0, score.tempo_bpm, True)]}

    sustained_span = CANONICAL_DIVISIONS * 4
    changes = score.tempo_changes
    result: dict[int, list[tuple[int, float, bool]]] = defaultdict(list)
    last_visible_bpm: float | None = None
    for index, change in enumerate(changes):
        measure_index, offset = _score_time_location(
            score, min(change.tick, score.measures[-1].end)
        )
        sustained = (
            index + 1 < len(changes)
            and changes[index + 1].tick - change.tick >= sustained_span
        ) or (
            index + 1 == len(changes)
            and index > 0
            and change.tick - changes[index - 1].tick >= sustained_span
        )
        visible = index == 0 or (
            sustained
            and last_visible_bpm is not None
            and abs(change.bpm - last_visible_bpm) / last_visible_bpm >= 0.03
        )
        if visible:
            last_visible_bpm = change.bpm
        result[measure_index].append((offset, change.bpm, visible))
    return result


def _add_pedal(measure: ET.Element, offset: int, down: bool) -> None:
    direction = ET.SubElement(measure, "direction", placement="below")
    direction_type = ET.SubElement(direction, "direction-type")
    pedal_type = "start" if down else "stop"
    ET.SubElement(direction_type, "pedal", type=pedal_type, line="yes", sign="no")
    if offset:
        ET.SubElement(direction, "offset", sound="yes").text = str(offset)
    ET.SubElement(direction, "staff").text = "2"


def _add_dynamic(measure: ET.Element, mark: DynamicMark) -> None:
    # Between the staves is the conventional piano-score dynamic position.
    direction = ET.SubElement(measure, "direction", placement="below")
    direction_type = ET.SubElement(direction, "direction-type")
    dynamics = ET.SubElement(direction_type, "dynamics")
    ET.SubElement(dynamics, mark.mark)
    ET.SubElement(direction, "sound", dynamics=str(mark.velocity_percent))
    ET.SubElement(direction, "staff").text = "1"


def _add_octave_shift(
    measure: ET.Element,
    offset: int,
    staff: Staff,
    shift_type: str,
    size: int,
    placement: str,
) -> None:
    direction = ET.SubElement(measure, "direction", placement=placement)
    direction_type = ET.SubElement(direction, "direction-type")
    ET.SubElement(
        direction_type,
        "octave-shift",
        type=shift_type,
        size=str(size),
        # MusicXML octave-shift numbers are part-wide, not staff-local.  Using
        # number 1 for simultaneous 8va and 8vb lines lets importers pair a
        # stop from one staff with the start on the other, leaking the octave
        # transposition into later notes.  The grand staff has stable 1/2 IDs.
        number=str(int(staff)),
    )
    if offset:
        ET.SubElement(direction, "offset").text = str(offset)
    ET.SubElement(direction, "staff").text = str(int(staff))


def _add_positioned_octave_shifts(
    measure: ET.Element,
    directions: list[tuple[int, Staff, str, int, str]],
) -> None:
    """Write mid-measure ottavas at the MusicXML time cursor.

    MuseScore imports a direction's start offset but can ignore the stop offset
    when all directions precede the note stream. Explicit forward/backup moves
    preserve both anchors and therefore the full dashed-line span.
    """

    cursor = 0
    for offset, staff, shift_type, size, placement in directions:
        if offset > cursor:
            forward = ET.SubElement(measure, "forward")
            ET.SubElement(forward, "duration").text = str(offset - cursor)
            cursor = offset
        _add_octave_shift(
            measure,
            0,
            staff,
            shift_type,
            size,
            placement,
        )
    if cursor:
        backup = ET.SubElement(measure, "backup")
        ET.SubElement(backup, "duration").text = str(cursor)


def _add_positioned_clef_changes(
    measure: ET.Element,
    changes: list[ClefChange],
) -> None:
    """Place clef changes at an exact MusicXML time cursor inside a measure."""

    cursor = 0
    grouped: dict[int, list[ClefChange]] = defaultdict(list)
    for change in changes:
        grouped[change.offset].append(change)
    for offset, offset_changes in sorted(grouped.items()):
        if offset > cursor:
            forward = ET.SubElement(measure, "forward")
            ET.SubElement(forward, "duration").text = str(offset - cursor)
            cursor = offset
        attributes = ET.SubElement(measure, "attributes")
        for change in sorted(offset_changes, key=lambda item: int(item.staff)):
            clef = ET.SubElement(
                attributes,
                "clef",
                number=str(int(change.staff)),
            )
            ET.SubElement(clef, "sign").text = change.sign
            ET.SubElement(clef, "line").text = str(change.line)
    if cursor:
        backup = ET.SubElement(measure, "backup")
        ET.SubElement(backup, "duration").text = str(cursor)


def _notation_atoms(
    notes: list[QuantizedNote],
    score: ScoreModel,
    ottava_spans: list[OttavaSpan] | None = None,
) -> list[NotationNote]:
    atoms: list[NotationNote] = []
    for note in notes:
        if note.staff is None:
            continue
        # MusicXML pitch values remain at concert/sounding pitch under an
        # octave-shift. MuseScore uses the direction for notation and must not
        # receive a second pitch displacement in the note element.
        # Grace notes are always short and never cross a barline, so they skip
        # the measure splitter (a split grace would lose its attachment).
        if note.grace:
            pieces = [(note.onset, note.duration)]
        else:
            pieces = _split_note_across_measures(note.onset, note.duration, score)
        for index, (onset, duration) in enumerate(pieces):
            atoms.append(
                NotationNote(
                    pitch=note.pitch,
                    onset=onset,
                    duration=duration,
                    velocity=note.velocity,
                    staff=note.staff,
                    voice=note.voice,
                    tie_stop=index > 0,
                    tie_start=index + 1 < len(pieces),
                    pitch_step=note.pitch_step,
                    pitch_alter=note.pitch_alter,
                    pitch_octave=note.pitch_octave,
                    hand=note.hand,
                    arpeggiated=note.arpeggiated,
                    trill=note.trill and index == 0,
                    grace=note.grace,
                    staccato=note.staccato and index == 0,
                    tremolo_start=note.tremolo_start and index == 0,
                    tremolo_stop=note.tremolo_stop and index == 0,
                )
            )
    return sorted(atoms, key=lambda atom: (atom.onset, atom.staff, atom.voice, atom.pitch))


def _split_note_across_measures(
    onset: int,
    duration: int,
    score: ScoreModel,
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    current = onset
    remaining = duration
    while remaining > 0:
        measure = score.measures[measure_index_at(score.measures, current)]
        relative = current - measure.start
        to_measure_end = measure.end - current
        if to_measure_end <= 0:
            raise ValueError(f"音符位置 {current} 超出小节时间轴")
        available = min(remaining, to_measure_end)

        # A complete 6/8 bar is conventionally one dotted half, not two dotted
        # quarters tied together.  Prefer a single exact duration when the note
        # begins at the barline and fills the available measure segment; metric
        # group splitting remains in force for genuine syncopations.
        if relative == 0 and available == to_measure_end and available in SPEC_BY_VALUE:
            pieces.append((current, available))
            current += available
            remaining -= available
            continue

        if _uses_additive_groups(measure.meter):
            available = min(
                available,
                _distance_to_next_group(relative, measure.duration, measure.meter),
            )
        else:
            within_beat = relative % measure.meter.beat_length
            if within_beat:
                available = min(available, measure.meter.beat_length - within_beat)

        piece = _choose_duration_clean(available)
        pieces.append((current, piece))
        current += piece
        remaining -= piece
    return pieces


def _split_readable_span(
    onset: int, duration: int, measure_length: int, meter: Meter
) -> list[tuple[int, int]]:
    pieces: list[tuple[int, int]] = []
    current = onset
    remaining = duration
    while remaining > 0:
        relative = current % measure_length
        to_measure_end = measure_length - relative
        available = min(remaining, to_measure_end)

        if _uses_additive_groups(meter):
            available = min(
                available,
                _distance_to_next_group(relative, measure_length, meter),
            )
        else:
            within_beat = relative % meter.beat_length
            if within_beat:
                available = min(available, meter.beat_length - within_beat)

        piece = _choose_duration_clean(available)
        pieces.append((current, piece))
        current += piece
        remaining -= piece
    return pieces


def _choose_duration(available: int) -> int:
    if available in SPEC_BY_VALUE:
        return available
    for spec in BINARY_SPECS:
        if spec.value <= available:
            return spec.value
    # Quantized input should never reach this branch; retaining the exact value makes
    # the failure visible to format validation instead of silently dropping time.
    return available


_SPEC_VALUES_DESC = tuple(sorted(SPEC_BY_VALUE, reverse=True))


@cache
def _exactly_decomposable(duration: int) -> bool:
    """Whether *duration* splits into one or more exact note-type specs."""

    if duration in SPEC_BY_VALUE:
        return True
    if duration < _SPEC_VALUES_DESC[-1]:
        return False
    return any(
        _exactly_decomposable(duration - value)
        for value in _SPEC_VALUES_DESC
        if value < duration
    )


def _choose_duration_clean(available: int) -> int:
    """Largest exact piece, shrunk until the remaining tail also decomposes.

    Greedy largest-first selection can strand a sub-grid tail (100 = 90 + 10,
    and 10 has no note type).  Shrinking the chosen piece one step (60 + 40)
    keeps every piece exactly representable.
    """

    piece = _choose_duration(available)
    if piece == available or piece not in SPEC_BY_VALUE:
        return piece
    tail = available - piece
    while tail and not _exactly_decomposable(tail):
        smaller = next((spec.value for spec in BINARY_SPECS if spec.value < piece), None)
        if smaller is None:
            break
        piece = smaller
        tail = available - piece
    return piece


def _voice_items(
    atoms: list[NotationNote],
    measure_start: int,
    measure_length: int,
    meter: Meter,
) -> list[VoiceItem]:
    if not atoms:
        return [
            VoiceItem(
                onset=0,
                duration=measure_length,
                spec=DurationSpec(measure_length, "whole"),
                notes=[],
                is_rest=True,
                measure_rest=True,
            )
        ]

    grace_atoms = [atom for atom in atoms if atom.grace]
    timed_atoms = [atom for atom in atoms if not atom.grace]
    timed_onsets = {atom.onset for atom in timed_atoms}
    attachable: list[NotationNote] = []
    demoted: list[NotationNote] = []
    for grace in grace_atoms:
        target = grace.onset + grace.duration
        if target in timed_onsets:
            attachable.append(grace)
        else:
            demoted.append(replace(grace, grace=False))
    if demoted:
        timed_atoms = sorted(
            [*timed_atoms, *demoted],
            key=lambda atom: (atom.onset, atom.pitch),
        )

    grouped: dict[tuple[int, int], list[NotationNote]] = defaultdict(list)
    for atom in timed_atoms:
        grouped[(atom.onset - measure_start, atom.duration)].append(atom)

    items: list[VoiceItem] = []
    cursor = 0
    for (onset, duration), chord_notes in sorted(grouped.items()):
        if onset > cursor:
            items.extend(_rest_items(cursor, onset - cursor, measure_length, meter))
        if onset < cursor:
            # Never fail the whole conversion on a same-voice overlap: fold a
            # same-attack arrival into the open chord, clip the previous
            # item's tail to the largest printable value, or — when the
            # overlap is shorter than any note type — merge it into the chord.
            previous = items[-1] if items else None
            if previous is not None and not previous.is_rest:
                clipped = onset - previous.onset
                exact = max(
                    (value for value in SPEC_BY_VALUE if value <= clipped),
                    default=0,
                )
                if previous.onset != onset and exact:
                    previous.duration = exact
                    previous.spec = SPEC_BY_VALUE[exact]
                    cursor = previous.onset + exact
                else:
                    previous.notes.extend(chord_notes)
                    previous.notes.sort(key=lambda note: note.pitch)
                    continue
            else:
                items.pop() if previous is not None else None
                cursor = items[-1].onset + items[-1].duration if items else 0
        if onset < cursor:
            continue
        spec = SPEC_BY_VALUE.get(duration, DurationSpec(duration, "64th"))
        items.append(
            VoiceItem(
                onset=onset,
                duration=duration,
                spec=spec,
                notes=sorted(chord_notes, key=lambda note: note.pitch),
            )
        )
        cursor = onset + duration

    if cursor < measure_length:
        items.extend(_rest_items(cursor, measure_length - cursor, measure_length, meter))
    for item in items:
        if item.is_rest and (item.onset == 0 or item.onset + item.duration == measure_length):
            item.boundary_rest = True

    if sum(item.duration for item in items) != measure_length:
        raise ValueError(
            f"声部时值总和不等于小节长度：{sum(item.duration for item in items)} != {measure_length}"
        )
    items = _complete_tuplet_groups(items, meter)
    items = _rescue_stranded_ratio_notes(items)
    _mark_beams(items, meter)
    for grace in attachable:
        target = grace.onset + grace.duration - measure_start
        for item in items:
            if not item.is_rest and item.onset == target:
                item.grace_notes.append(grace)
                break
    return items


def _rescue_stranded_ratio_notes(items: list[VoiceItem]) -> list[VoiceItem]:
    """Re-notate ratio-marked notes that no complete tuplet group claimed.

    A lone 80-tick note (meant as a sextuplet member) cannot be printed as-is:
    bare ratio members hang MuseScore's importer, and no plain note type has
    that length.  When an adjacent padding rest can donate exactly enough time
    to reach a plain, exactly representable value (80 + 40 = a plain 16th),
    the note keeps its attack and the padding shrinks or disappears — total
    voice time is preserved and the result imports cleanly.
    """

    result = list(items)
    for index, item in enumerate(result):
        if item.is_rest or not item.spec.time_modification or item.in_tuplet:
            continue
        candidates = sorted(
            (spec.value for spec in BINARY_SPECS if spec.value != item.duration),
            key=lambda value: (abs(value - item.duration), value),
        )
        for target in candidates:
            delta = target - item.duration
            if delta > 0 and index + 1 < len(result):
                following = result[index + 1]
                if (
                    following.is_rest
                    and not following.measure_rest
                    and not following.in_tuplet
                    and not following.tuplet_start
                    and not following.tuplet_stop
                ):
                    leftover = following.duration - delta
                    if leftover == 0 or leftover in SPEC_BY_VALUE:
                        item.duration = target
                        item.spec = SPEC_BY_VALUE[target]
                        if leftover:
                            result[index + 1] = replace(
                                following,
                                onset=following.onset + delta,
                                duration=leftover,
                                spec=SPEC_BY_VALUE[leftover],
                            )
                        else:
                            result.pop(index + 1)
                        break
            if delta > 0 and index > 0:
                preceding = result[index - 1]
                if (
                    preceding.is_rest
                    and not preceding.measure_rest
                    and not preceding.in_tuplet
                    and not preceding.tuplet_start
                    and not preceding.tuplet_stop
                    and preceding.duration <= delta
                    and item.duration + preceding.duration in SPEC_BY_VALUE
                ):
                    item.onset -= preceding.duration
                    item.duration += preceding.duration
                    item.spec = SPEC_BY_VALUE[item.duration]
                    result.pop(index - 1)
                    break
        # If no adjacent rest can donate, the note stays as written; the batch
        # render gate flags any score where that still happens.
    return _rescue_stranded_ratio_runs(result)


def _rescue_stranded_ratio_runs(items: list[VoiceItem]) -> list[VoiceItem]:
    """Last resort: re-notate a whole stranded run on plain binary values.

    Two triplet eighths at a measure tail (2/3 of a triplet) can never close
    a bracket, and no neighbor may have free time to donate.  Each note drops
    to the nearest shorter plain value and the freed ticks collect in one
    padding rest, keeping the voice's total time and every printed duration
    exact.
    """

    result: list[VoiceItem] = []
    index = 0
    while index < len(items):
        item = items[index]
        if item.is_rest or not item.spec.time_modification or item.in_tuplet:
            result.append(item)
            index += 1
            continue
        run: list[VoiceItem] = []
        stop = index
        while stop < len(items):
            current = items[stop]
            if current.in_tuplet or not current.spec.time_modification:
                break
            run.append(current)
            stop += 1

        freed = 0
        converted: list[VoiceItem] = []
        for member in run:
            if member.is_rest:
                freed += member.duration
                continue
            plain = max(
                (spec.value for spec in BINARY_SPECS if spec.value <= member.duration),
                default=None,
            )
            if plain is None:
                plain = min(spec.value for spec in BINARY_SPECS)
            freed += member.duration - plain
            member.duration = plain
            member.spec = SPEC_BY_VALUE[plain]
            member.beam = None
            converted.append(member)
        if freed < 0:
            donor = result[-1] if result else None
            if (
                donor is not None
                and donor.is_rest
                and not donor.in_tuplet
                and donor.duration + freed > 0
            ):
                donor.duration += freed
                donor.spec = SPEC_BY_VALUE.get(
                    donor.duration, DurationSpec(donor.duration, "64th")
                )
                freed = 0
        if freed < 0:
            # No padding anywhere can cover the extension: keep the run as-is.
            result.extend(run)
        else:
            result.extend(converted)
            if freed:
                onset = run[0].onset
                if converted:
                    onset = converted[-1].onset + converted[-1].duration
                result.append(
                    VoiceItem(
                        onset=onset,
                        duration=freed,
                        spec=SPEC_BY_VALUE.get(freed, DurationSpec(freed, "64th")),
                        notes=[],
                        is_rest=True,
                    )
                )
        index = stop
    return result


def _mark_hidden_padding_rests(
    sequences: list[tuple[Staff, int, list[VoiceItem]]],
    measure_length: int,
    meter: Meter,
) -> None:
    sounding_sequences = [
        (voice, items)
        for _, voice, items in sequences
        if any(not item.is_rest for item in items)
    ]
    if len(sounding_sequences) <= 1:
        return

    for voice, items in sounding_sequences:
        other_intervals = [
            (item.onset, item.onset + item.duration)
            for other_voice, other_items in sounding_sequences
            if other_voice != voice
            for item in other_items
            if not item.is_rest
        ]
        note_items = [item for item in items if not item.is_rest]
        sparse_voice = (
            len(note_items) <= 2
            or sum(item.duration for item in note_items) <= measure_length * 0.45
        )
        other_coverage = _covered_duration(other_intervals)
        for item in items:
            if not item.is_rest or item.measure_rest:
                continue
            rest_end = item.onset + item.duration
            fully_covered = _interval_is_covered(item.onset, rest_end, other_intervals)
            boundary_padding = item.onset == 0 or rest_end == measure_length
            overlap = _overlap_duration(item.onset, rest_end, other_intervals)
            short_voice_padding = item.duration <= meter.beat_length and (
                overlap * 2 >= item.duration
                or other_coverage >= measure_length * 0.45
            )
            if (
                fully_covered
                or boundary_padding
                or (sparse_voice and other_coverage >= measure_length * 0.65)
                or (sparse_voice and short_voice_padding)
                or (
                    voice > 1
                    and short_voice_padding
                    and other_coverage >= measure_length * 0.55
                )
            ):
                item.hidden_rest = True


def _interval_is_covered(
    start: int,
    end: int,
    intervals: list[tuple[int, int]],
) -> bool:
    cursor = start
    for interval_start, interval_end in sorted(intervals):
        if interval_end <= cursor:
            continue
        if interval_start > cursor:
            return False
        cursor = max(cursor, interval_end)
        if cursor >= end:
            return True
    return cursor >= end


def _covered_duration(intervals: list[tuple[int, int]]) -> int:
    if not intervals:
        return 0
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return sum(end - start for start, end in merged)


def _overlap_duration(
    start: int,
    end: int,
    intervals: list[tuple[int, int]],
) -> int:
    return _covered_duration(
        [
            (max(start, interval_start), min(end, interval_end))
            for interval_start, interval_end in intervals
            if interval_start < end and interval_end > start
        ]
    )


def _stem_directions(
    sequences: list[tuple[Staff, int, list[VoiceItem]]],
) -> dict[int, str]:
    sounding = [
        (voice, items)
        for _, voice, items in sequences
        if any(not item.is_rest for item in items)
    ]
    if len(sounding) <= 1:
        return {}

    hands: dict[int, Hand | None] = {}
    for voice, items in sounding:
        known = [note.hand for item in items for note in item.notes if note.hand is not None]
        hands[voice] = Counter(known).most_common(1)[0][0] if known else None
    known_hands = {hand for hand in hands.values() if hand is not None}
    if known_hands == {Hand.LEFT, Hand.RIGHT}:
        return {
            voice: "up" if hand == Hand.RIGHT else "down"
            for voice, hand in hands.items()
            if hand is not None
        }

    centers = {
        voice: median(
            note.pitch
            for item in items
            if not item.is_rest
            for note in item.notes
        )
        for voice, items in sounding
    }
    ordered = sorted(centers, key=lambda voice: (centers[voice], -voice), reverse=True)
    return {
        voice: "up" if index == 0 else "down"
        for index, voice in enumerate(ordered)
    }


def _rest_items(
    onset: int, duration: int, measure_length: int, meter: Meter
) -> list[VoiceItem]:
    return [
        VoiceItem(
            onset=piece_onset,
            duration=piece_duration,
            spec=SPEC_BY_VALUE.get(piece_duration, DurationSpec(piece_duration, "64th")),
            notes=[],
            is_rest=True,
        )
        for piece_onset, piece_duration in _split_readable_span(
            onset, duration, measure_length, meter
        )
    ]


def _complete_tuplet_groups(items: list[VoiceItem], meter: Meter) -> list[VoiceItem]:
    """Bracket tuplet groups, completing them by absorbing padding rests.

    MuseScore's importer hangs on a time-modified item whose measure-mates do
    not fill a complete ratio span, so no unbracketed ratio member may ever
    reach the output.  Complete spans are the ratios' natural power-of-two
    totals (16th triplets close at 240, sextuplets only at 480+, and so on).
    Short groups pull in adjacent padding rests — split into member units when
    necessary, before the group as well as after it — until the span closes
    exactly; absorbed rests keep their hidden/invisible role, so the bracket
    simply spans padding the voice already owned.
    """

    expanded: list[VoiceItem] = []
    for item in items:
        expanded.extend(_split_odd_triplet_member(item))

    work = list(expanded)
    result: list[VoiceItem] = []
    group: list[VoiceItem] = []
    group_mod: tuple[int, int] | None = None
    group_sum = 0

    def complete_spans() -> tuple[int, ...]:
        return (480, 960) if group_mod == (6, 4) else (240, 480, 960)

    def is_complete() -> bool:
        return bool(group) and group_sum in complete_spans()

    def absorb_backward() -> None:
        nonlocal group_sum
        while not is_complete() and result and result[-1].is_rest:
            tail = result[-1]
            # Never cannibalize rests that already belong to a closed bracket.
            if tail.in_tuplet or tail.tuplet_start or tail.tuplet_stop:
                return
            target = next((span for span in complete_spans() if span > group_sum), None)
            if target is None:
                return
            needed = target - group_sum
            if tail.duration in TUPLET_MEMBER_SPECS[group_mod] and tail.duration <= needed:
                result.pop()
                group.insert(0, tail)
                if tail.spec.time_modification != group_mod:
                    tail.spec = TUPLET_MEMBER_SPECS[group_mod][tail.duration]
                group_sum += tail.duration
                continue
            absorbed = _member_unit_split(min(needed, tail.duration), group_mod)
            if absorbed is None:
                return
            onset = tail.onset
            pieces = []
            for part in absorbed:
                pieces.append(
                    VoiceItem(
                        onset=onset,
                        duration=part,
                        spec=TUPLET_MEMBER_SPECS[group_mod][part],
                        notes=[],
                        is_rest=True,
                        hidden_rest=tail.hidden_rest,
                    )
                )
                onset += part
            remainder = tail.duration - sum(absorbed)
            if remainder:
                result[-1] = replace(
                    tail,
                    duration=remainder,
                    spec=SPEC_BY_VALUE.get(remainder, DurationSpec(remainder, "64th")),
                )
            else:
                result.pop()
            group[:0] = pieces
            group_sum += sum(absorbed)

    def try_alternate_ratios() -> None:
        """Re-spec a stalled group under a different complete ratio.

        Three sextuplet sixteenths stalling at 240 ticks are, read as 16th
        triplets, a complete 3:2 group — the written durations are identical,
        only the ratio label changes.  Trying the aliases rescues groups that
        absorption could not complete.
        """

        nonlocal group_mod
        durations = [member.duration for member in group]
        for alternative in ((3, 2), (6, 4), (5, 4)):
            if alternative == group_mod:
                continue
            table = TUPLET_MEMBER_SPECS[alternative]
            if not all(duration in table for duration in durations):
                continue
            previous = group_mod
            group_mod = alternative
            if is_complete():
                for member in group:
                    member.spec = table[member.duration]
                return
            group_mod = previous

    def close_group() -> None:
        nonlocal group, group_mod, group_sum
        if group and not is_complete():
            absorb_backward()
        if group and not is_complete():
            try_alternate_ratios()
        if is_complete() and len(group) >= 2:
            group[0].tuplet_start = True
            group[-1].tuplet_stop = True
            for member in group:
                member.in_tuplet = True
        result.extend(group)
        group = []
        group_mod = None
        group_sum = 0

    index = 0
    while index < len(work):
        item = work[index]
        if group:
            contiguous = group[-1].onset + group[-1].duration == item.onset
            if contiguous and not is_complete():
                target = next((span for span in complete_spans() if span > group_sum), None)
                needed = (target - group_sum) if target else 0
                if item.duration in TUPLET_MEMBER_SPECS[group_mod]:
                    if item.spec.time_modification != group_mod:
                        item.spec = TUPLET_MEMBER_SPECS[group_mod][item.duration]
                    group.append(item)
                    group_sum += item.duration
                    index += 1
                    continue
                if item.is_rest and needed > 0:
                    absorbed = _member_unit_split(min(needed, item.duration), group_mod)
                    if absorbed is not None:
                        onset = item.onset
                        for part in absorbed:
                            group.append(
                                VoiceItem(
                                    onset=onset,
                                    duration=part,
                                    spec=TUPLET_MEMBER_SPECS[group_mod][part],
                                    notes=[],
                                    is_rest=True,
                                    hidden_rest=item.hidden_rest,
                                )
                            )
                            onset += part
                        remainder = item.duration - sum(absorbed)
                        if remainder:
                            work[index] = replace(
                                item,
                                onset=item.onset + sum(absorbed),
                                duration=remainder,
                                spec=SPEC_BY_VALUE.get(
                                    remainder, DurationSpec(remainder, "64th")
                                ),
                            )
                        else:
                            work.pop(index)
                        group_sum += sum(absorbed)
                        continue
            close_group()
            continue

        mod = item.spec.time_modification
        if mod is not None and item.duration in TUPLET_MEMBER_SPECS.get(mod, {}):
            group = [item]
            group_mod = mod
            group_sum = item.duration
            index += 1
            continue
        result.append(item)
        index += 1

    close_group()
    return result


def _member_unit_split(duration: int, mod: tuple[int, int]) -> list[int] | None:
    """Split *duration* into member units of the given tuplet ratio."""

    members = sorted(TUPLET_MEMBER_SPECS[mod], reverse=True)
    pieces: list[int] = []
    remaining = duration
    for unit in members:
        while remaining >= unit:
            pieces.append(unit)
            remaining -= unit
    if remaining:
        return None
    return pieces


def _split_odd_triplet_member(item: VoiceItem) -> list[VoiceItem]:
    """Split a triplet-grid value with no single notehead (400 = 320+80)."""

    if item.is_rest or item.spec.time_modification or item.duration != 400:
        return [item]
    if any(note.tie_start or note.tie_stop for note in item.notes):
        # Already part of a tie chain; leave the exact value untouched rather
        # than disturbing the surrounding notation.
        return [item]
    first_spec = TUPLET_MEMBER_SPECS[(3, 2)][320]
    second_spec = TUPLET_MEMBER_SPECS[(3, 2)][80]
    first_notes = [replace(note, tie_start=True) for note in item.notes]
    second_notes = [replace(note, tie_stop=True) for note in item.notes]
    return [
        VoiceItem(item.onset, 320, first_spec, first_notes),
        VoiceItem(item.onset + 320, 80, second_spec, second_notes),
    ]


def _mark_beams(items: list[VoiceItem], meter: Meter) -> None:
    run: list[VoiceItem] = []
    for item in items + [VoiceItem(0, 0, DurationSpec(0, "quarter"), [], True)]:
        beamable = not item.is_rest and item.duration <= CANONICAL_DIVISIONS // 2
        same_beat = bool(run) and _metric_group_index(
            item.onset, meter
        ) == _metric_group_index(run[0].onset, meter)
        contiguous = bool(run) and run[-1].onset + run[-1].duration == item.onset
        if beamable and (not run or (same_beat and contiguous)):
            run.append(item)
            continue
        if len(run) >= 2:
            run[0].beam = "begin"
            for middle in run[1:-1]:
                middle.beam = "continue"
            run[-1].beam = "end"
        run = [item] if beamable else []


def _uses_additive_groups(meter: Meter) -> bool:
    return any(group != meter.beat_length for group in meter.beat_groups)


def _distance_to_next_group(relative: int, measure_length: int, meter: Meter) -> int:
    for boundary in meter.beat_group_boundaries[1:]:
        if boundary > relative:
            return max(1, min(boundary, measure_length) - relative)
    return max(1, measure_length - relative)


def _metric_group_index(onset: int, meter: Meter) -> int:
    for index, boundary in enumerate(meter.beat_group_boundaries[1:]):
        if onset < boundary:
            return index
    return len(meter.beat_groups) - 1


def _write_forward(measure: ET.Element, duration: int, staff: Staff, voice: int) -> None:
    forward = ET.SubElement(measure, "forward")
    ET.SubElement(forward, "duration").text = str(duration)
    ET.SubElement(forward, "voice").text = str(voice)
    ET.SubElement(forward, "staff").text = str(int(staff))


def _write_item(
    measure: ET.Element,
    item: VoiceItem,
    staff: Staff,
    voice: int,
    fifths: int,
    stem_direction: str | None,
) -> None:
    if item.is_rest:
        note_attributes = {"print-object": "no"} if item.hidden_rest else {}
        note_element = ET.SubElement(measure, "note", note_attributes)
        rest_attributes = {"measure": "yes"} if item.measure_rest else {}
        ET.SubElement(note_element, "rest", rest_attributes)
        _write_duration_details(
            note_element,
            item,
            staff,
            voice,
            stem_direction,
            first_in_chord=True,
        )
        # Rests are full tuplet members in MusicXML; without the bracket a
        # triplet group starting or ending on a rest corrupts the measure for
        # stricter importers such as MuseScore.
        if item.tuplet_start or item.tuplet_stop:
            notations = ET.SubElement(note_element, "notations")
            if item.tuplet_start:
                ET.SubElement(notations, "tuplet", type="start", bracket="auto")
            if item.tuplet_stop:
                ET.SubElement(notations, "tuplet", type="stop")
        return

    for grace in item.grace_notes:
        grace_element = ET.SubElement(measure, "note")
        ET.SubElement(grace_element, "grace", slash="yes")
        _write_pitch(grace_element, grace, fifths)
        ET.SubElement(grace_element, "voice").text = str(voice)
        ET.SubElement(grace_element, "type").text = "eighth"
        if stem_direction:
            ET.SubElement(grace_element, "stem").text = stem_direction
        ET.SubElement(grace_element, "staff").text = str(int(staff))

    for note_index, notation_note in enumerate(item.notes):
        note_element = ET.SubElement(measure, "note")
        if note_index:
            ET.SubElement(note_element, "chord")
        _write_pitch(note_element, notation_note, fifths)
        _write_duration_details(
            note_element,
            item,
            staff,
            voice,
            stem_direction,
            first_in_chord=note_index == 0,
            tie_stop=notation_note.tie_stop,
            tie_start=notation_note.tie_start,
        )

        if (
            notation_note.tie_stop
            or notation_note.tie_start
            or item.tuplet_start
            or item.tuplet_stop
            or notation_note.arpeggiated
            or notation_note.trill
            or notation_note.staccato
            or notation_note.tremolo_start
            or notation_note.tremolo_stop
        ):
            notations = ET.SubElement(note_element, "notations")
            if notation_note.tie_stop:
                ET.SubElement(notations, "tied", type="stop")
            if notation_note.tie_start:
                ET.SubElement(notations, "tied", type="start")
            if note_index == 0 and item.tuplet_start:
                ET.SubElement(notations, "tuplet", type="start", bracket="auto")
            if note_index == 0 and item.tuplet_stop:
                ET.SubElement(notations, "tuplet", type="stop")
            if notation_note.arpeggiated:
                ET.SubElement(notations, "arpeggiate", number="1")
            if notation_note.trill:
                ornaments = ET.SubElement(notations, "ornaments")
                ET.SubElement(ornaments, "trill-mark")
            if notation_note.staccato:
                articulations = ET.SubElement(notations, "articulations")
                ET.SubElement(articulations, "staccato")
            if notation_note.tremolo_start or notation_note.tremolo_stop:
                ornaments = ET.SubElement(notations, "ornaments")
                tremolo_type = "start" if notation_note.tremolo_start else "stop"
                ET.SubElement(ornaments, "tremolo", type=tremolo_type).text = "3"


def _write_pitch(
    note_element: ET.Element,
    notation_note: NotationNote,
    fifths: int,
) -> None:
    pitch = ET.SubElement(note_element, "pitch")
    if notation_note.pitch_step is not None and notation_note.pitch_octave is not None:
        step = notation_note.pitch_step
        alter = notation_note.pitch_alter
        octave = notation_note.pitch_octave
    else:
        spelling = FLAT_PITCHES if fifths < 0 else SHARP_PITCHES
        step, alter = spelling[notation_note.pitch % 12]
        octave = notation_note.pitch // 12 - 1
    ET.SubElement(pitch, "step").text = step
    if alter:
        ET.SubElement(pitch, "alter").text = str(alter)
    ET.SubElement(pitch, "octave").text = str(octave)


def _write_duration_details(
    note_element: ET.Element,
    item: VoiceItem,
    staff: Staff,
    voice: int,
    stem_direction: str | None,
    first_in_chord: bool,
    tie_stop: bool = False,
    tie_start: bool = False,
) -> None:
    ET.SubElement(note_element, "duration").text = str(item.duration)
    if tie_stop:
        ET.SubElement(note_element, "tie", type="stop")
    if tie_start:
        ET.SubElement(note_element, "tie", type="start")
    ET.SubElement(note_element, "voice").text = str(voice)
    ET.SubElement(note_element, "type").text = "whole" if item.measure_rest else item.spec.note_type
    if not item.measure_rest:
        for _ in range(item.spec.dots):
            ET.SubElement(note_element, "dot")
        if item.spec.time_modification:
            actual, normal = item.spec.time_modification
            time_modification = ET.SubElement(note_element, "time-modification")
            ET.SubElement(time_modification, "actual-notes").text = str(actual)
            ET.SubElement(time_modification, "normal-notes").text = str(normal)
    if stem_direction and not item.is_rest:
        ET.SubElement(note_element, "stem").text = stem_direction
    if first_in_chord and item.beam:
        ET.SubElement(note_element, "beam", number="1").text = item.beam
    ET.SubElement(note_element, "staff").text = str(int(staff))


def _ottava_directions_by_measure(
    score: ScoreModel,
    spans: list[OttavaSpan],
) -> dict[int, list[tuple[int, Staff, str, int, str]]]:
    result: dict[int, list[tuple[int, Staff, str, int, str]]] = defaultdict(list)
    for span in spans:
        placement = "above" if span.direction == "down" else "below"
        start_measure, start_offset = _score_time_location(score, span.start)
        stop_measure, stop_offset = _score_time_location(score, span.end)
        result[start_measure].append(
            (start_offset, span.staff, span.direction, span.size, placement)
        )
        result[stop_measure].append(
            (stop_offset, span.staff, "stop", span.size, placement)
        )
    for directions in result.values():
        directions.sort(key=lambda item: (item[0], item[2] != "stop", int(item[1])))
    return result


def _score_time_location(score: ScoreModel, tick: int) -> tuple[int, int]:
    for index, measure in enumerate(score.measures):
        if tick == measure.start:
            return index, 0
        if measure.start < tick < measure.end:
            return index, tick - measure.start
        if tick == measure.end:
            if index + 1 < len(score.measures):
                return index + 1, 0
            return index, measure.duration
    raise ValueError(f"八度线位置 {tick} 超出乐谱时间轴")


def _pedals_by_measure(score: ScoreModel) -> dict[int, list[tuple[int, bool]]]:
    result: dict[int, list[tuple[int, bool]]] = defaultdict(list)
    previous: bool | None = None
    score_end = score.measures[-1].end
    for pedal in sorted(score.pedals, key=lambda item: item.tick):
        if pedal.down == previous or pedal.tick < 0:
            continue
        if pedal.tick > score_end:
            continue
        if pedal.tick == score_end:
            measure_index = score.measure_count - 1
            offset = score.measures[-1].duration
        else:
            measure_index = measure_index_at(score.measures, pedal.tick)
            offset = pedal.tick - score.measures[measure_index].start
        result[measure_index].append((offset, pedal.down))
        previous = pedal.down
    return result


def musicxml_readability_metrics(musicxml: str) -> dict[str, object]:
    root = ET.fromstring(musicxml)
    rests = [note for note in root.findall(".//note") if note.find("rest") is not None]
    octave_shifts = root.findall(".//direction-type/octave-shift")
    starts = [
        shift
        for shift in octave_shifts
        if shift.get("type") in {"up", "down"}
    ]
    tuplet_starts = root.findall(".//notations/tuplet[@type='start']")
    tuplet_stops = root.findall(".//notations/tuplet[@type='stop']")
    return {
        "hidden_padding_rests": sum(note.get("print-object") == "no" for note in rests),
        "visible_rests": sum(note.get("print-object") != "no" for note in rests),
        "voice_time_skips": len(root.findall(".//forward")),
        "measure_rests": sum(
            (rest := note.find("rest")) is not None and rest.get("measure") == "yes"
            for note in rests
        ),
        "ottava_spans": len(starts),
        "ottava_sizes": dict(
            sorted(Counter(int(shift.get("size", "8")) for shift in starts).items())
        ),
        "arpeggiated_noteheads": len(root.findall(".//notations/arpeggiate")),
        "tuplet_spans": len(tuplet_starts),
        "unbalanced_tuplet_brackets": abs(len(tuplet_starts) - len(tuplet_stops)),
    }
