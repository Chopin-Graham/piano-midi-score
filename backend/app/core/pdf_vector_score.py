"""Vector-layer score extraction for digitally engraved PDFs.

Digitally engraved PDFs (LilyPond, and to a lesser extent other notation
programs) carry the score twice: once as visible ink and once as a text layer
of font glyph names with exact positions (``/noteheads.s2``,
``/scripts.trill``, ``/accidentals.sharp`` ...).  Reading that layer skips
image-based OMR entirely — there is no recognition error, only geometry.

This module parses the glyph layer plus the vector drawing stream (staff
lines, stems, beams, barlines) into note/rest events, then synthesizes a
standard MIDI file whose onsets follow the engraved x positions.  The
synthetic MIDI enters the regular notation pipeline, so quantization, voice
separation, spelling, and engraving all reuse the battle-tested path.

PDFs without a glyph layer (scans, commercial prints with subset fonts)
report :data:`VectorScoreError` and the caller falls back to image OMR.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from io import BytesIO

from pypdf import PdfReader
from pypdf.generic import ContentStream

# ---------------------------------------------------------------------------
# Glyph vocabulary (LilyPond/Emmentaler names)
# ---------------------------------------------------------------------------

NOTEHEAD_CLASS = {
    "noteheads.s0": 0,  # breve/whole family (open, no stem)
    "noteheads.s1": 1,  # half (open, stemmed)
    "noteheads.s2": 2,  # quarter and shorter (filled, stemmed)
    "noteheads.sM1": -1,  # mensural variants — rare, treat as unknown
}

REST_CLASS = {
    "rests.0": 4.0,  # whole rest (measure rest semantics handled downstream)
    "rests.1": 2.0,  # half
    "rests.2": 1.0,  # quarter
    "rests.3": 0.5,  # eighth
    "rests.4": 0.25,  # 16th
    "rests.5": 0.125,  # 32nd
    "rests.6": 0.0625,  # 64th
    "rests.7": 0.03125,
}

ACCIDENTAL_ALTER = {
    "accidentals.sharp": 1,
    "accidentals.flat": -1,
    "accidentals.natural": 0,
    "accidentals.doublesharp": 2,
    "accidentals.flatflat": -2,
    # Older Emmentaler names seen in the Mutopia corpus ("M" reads as the
    # minus sign): verified against key signatures and in-measure accidentals.
    "accidentals.2": 1,    # sharp (C# in the D-minor corpus fugue)
    "accidentals.1": 2,    # double sharp
    "accidentals.0": 0,    # natural
    "accidentals.M1": -2,  # double flat
    "accidentals.M2": -1,  # flat (1-flat key signatures print one M2)
}

SCRIPT_MARK = {
    "scripts.staccato": "staccato",
    "scripts.ustaccatissimo": "staccatissimo",
    "scripts.dstaccatissimo": "staccatissimo",
    "scripts.trill": "trill",
    "scripts.turn": "turn",
    "scripts.mordent": "mordent",
    "scripts.prall": "mordent",
    "scripts.fermata": "fermata",
    "scripts.ufermata": "fermata",
    "scripts.dfermata": "fermata",
    "scripts.accent": "accent",
    "scripts.tenuto": "tenuto",
    "scripts.arpeggio": "arpeggio",
}

DYNAMIC_GLYPHS = {"scripts.p", "scripts.f", "scripts.m", "scripts.r", "scripts.s", "scripts.z", "scripts.n"}


class VectorScoreError(RuntimeError):
    """The PDF does not expose a parseable engraved-glyph layer."""


@dataclass(slots=True)
class Glyph:
    name: str
    x: float
    y: float
    size: float


@dataclass(slots=True)
class StaffLines:
    """One staff: five line y-positions (bottom-up) and its x extent."""

    ys: list[float]
    x0: float
    x1: float

    @property
    def spacing(self) -> float:
        return (self.ys[-1] - self.ys[0]) / 4 if len(self.ys) >= 2 else 5.0

    @property
    def top(self) -> float:
        return self.ys[-1]

    @property
    def bottom(self) -> float:
        return self.ys[0]


@dataclass(slots=True)
class Beam:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(slots=True)
class PageGeometry:
    glyphs: list[Glyph]
    staves: list[StaffLines]
    beams: list[Beam]          # filled beam rectangles/polygons
    stems: list[Beam]          # thin vertical filled rects (note stems)
    barlines: list[Beam]       # tall connectors (initial/final/system bars)
    bar_segments: list[Beam]   # per-staff barline segments (span one staff)
    verticals: list[Beam]      # every stem/barline-height vertical rect
    page_height: float


# ---------------------------------------------------------------------------
# Low-level content stream parsing
# ---------------------------------------------------------------------------


def _mat_mul(m1: list[float], m2: list[float]) -> list[float]:
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    ]


def _apply(m: list[float], x: float, y: float) -> tuple[float, float]:
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _page_drawings(page) -> tuple[list[tuple[float, float, float, float]], list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    """Filled rects, filled polygons, stroked polylines — all in user space."""

    content = ContentStream(page.get_contents(), page.pdf)
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: list[list[float]] = []
    rects: list[tuple[float, float, float, float]] = []
    polys: list[list[tuple[float, float]]] = []
    strokes: list[list[tuple[float, float]]] = []
    path: list[tuple[float, float] | str] = []

    for operands, op in content.operations:
        if op == b"q":
            stack.append(list(ctm))
        elif op == b"Q":
            if stack:
                ctm = stack.pop()
        elif op == b"cm":
            ctm = _mat_mul(ctm, [float(v) for v in operands[:6]])
        elif op == b"re":
            x, y, w, h = (float(v) for v in operands[:4])
            p1 = _apply(ctm, x, y)
            p2 = _apply(ctm, x + w, y + h)
            rects.append(
                (min(p1[0], p2[0]), min(p1[1], p2[1]), abs(p2[0] - p1[0]), abs(p2[1] - p1[1]))
            )
        elif op == b"m":
            path = [(float(operands[0]), float(operands[1]))]
        elif op == b"l":
            path.append((float(operands[0]), float(operands[1])))
        elif op in (b"c", b"v", b"y"):
            path.append("C")  # curve: geometry not needed for beams/staff lines
        elif op == b"h":
            pass
        elif op in (b"f", b"f*", b"F"):
            points = [_apply(ctm, p[0], p[1]) for p in path if p != "C"]
            if len(points) >= 3:
                polys.append(points)
            path = []
        elif op in (b"S", b"s"):
            points = [_apply(ctm, p[0], p[1]) for p in path if p != "C"]
            if len(points) >= 2:
                strokes.append(points)
            path = []
    return rects, polys, strokes


def _font_tables(page) -> dict[str, tuple[dict[int, str], dict[int, float]]]:
    """Per-font byte→glyph-name and byte→advance(pt at 1pt size) tables."""

    tables: dict[str, tuple[dict[int, str], dict[int, float]]] = {}
    resources = page.get("/Resources")
    if resources is None:
        return tables
    fonts = resources.get("/Font")
    if fonts is None:
        return tables
    for name, ref in fonts.items():
        font = ref.get_object()
        names: dict[int, str] = {}
        widths: dict[int, float] = {}
        encoding = font.get("/Encoding")
        if encoding is not None:
            encoding = encoding.get_object() if hasattr(encoding, "get_object") else encoding
            differences = encoding.get("/Differences") if hasattr(encoding, "get") else None
            if differences:
                code = 0
                for entry in differences:
                    if isinstance(entry, int):
                        code = entry
                    else:
                        names[code] = str(entry).lstrip("/")
                        code += 1
        raw_widths = font.get("/Widths")
        first_char = font.get("/FirstChar")
        if raw_widths is not None and first_char is not None:
            first = int(first_char)
            for offset, value in enumerate(raw_widths):
                widths[first + offset] = float(value) / 1000.0
        tables[str(name)] = (names, widths)
    return tables


def _page_glyphs(page) -> list[Glyph]:
    """Per-`Tj` extraction with the text matrix tracked by hand.

    pypdf's text visitor merges adjacent text-showing operations into one
    call and reports a single position for the merge — which loses the
    geometry of accidentals and chord noteheads drawn seconds apart.  Walking
    the content stream ourselves keeps every glyph's own position.
    """

    fonts = _font_tables(page)
    content = ContentStream(page.get_contents(), page.pdf)
    ctm = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: list[list[float]] = []
    text_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    line_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    font_name = ""
    font_size = 12.0
    items: list[Glyph] = []

    def position() -> tuple[float, float]:
        return _apply(ctm, text_matrix[4], text_matrix[5])

    for operands, op in content.operations:
        if op == b"q":
            stack.append(list(ctm))
        elif op == b"Q":
            if stack:
                ctm = stack.pop()
        elif op == b"cm":
            ctm = _mat_mul(ctm, [float(v) for v in operands[:6]])
        elif op == b"BT":
            text_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
            line_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif op == b"Tm":
            text_matrix = [float(v) for v in operands[:6]]
            line_matrix = list(text_matrix)
        elif op == b"Td":
            dx, dy = float(operands[0]), float(operands[1])
            line_matrix = _mat_mul(line_matrix, [1.0, 0.0, 0.0, 1.0, dx, dy])
            text_matrix = list(line_matrix)
        elif op == b"Tf":
            font_name = str(operands[0])
            font_size = float(operands[1])
        elif op in (b"Tj", b"'", b'"'):
            raw = bytes(operands[0]) if not isinstance(operands[0], str) else operands[0].encode("latin-1", "replace")
            names, widths = fonts.get(font_name, ({}, {}))
            x, y = position()
            advance = 0.0
            for byte in raw:
                glyph_name = names.get(byte)
                if glyph_name:
                    items.append(Glyph(glyph_name, round(x + advance, 2), round(y, 2), font_size))
                advance += widths.get(byte, 0.5) * font_size
        elif op == b"TJ":
            names, widths = fonts.get(font_name, ({}, {}))
            x, y = position()
            advance = 0.0
            for piece in operands[0]:
                if isinstance(piece, (int, float)):
                    advance -= float(piece) / 1000.0 * font_size
                    continue
                raw = bytes(piece) if not isinstance(piece, str) else piece.encode("latin-1", "replace")
                for byte in raw:
                    glyph_name = names.get(byte)
                    if glyph_name:
                        items.append(Glyph(glyph_name, round(x + advance, 2), round(y, 2), font_size))
                    advance += widths.get(byte, 0.5) * font_size

    return items


# ---------------------------------------------------------------------------
# Geometry assembly
# ---------------------------------------------------------------------------


def _build_geometry(page) -> PageGeometry:
    raw_glyphs = _page_glyphs(page)
    rects, polys, strokes = _page_drawings(page)

    # Staff lines: long horizontal stroked lines.
    line_items: list[tuple[float, float, float]] = []  # (y, x0, x1)
    for points in strokes:
        if len(points) != 2:
            continue
        (x1, y1), (x2, y2) = points
        if abs(y2 - y1) < 0.6 and abs(x2 - x1) > 150:
            line_items.append(((y1 + y2) / 2, min(x1, x2), max(x1, x2)))
    line_items.sort()
    # A line redrawn with a hair of offset is one staff line, not two.
    deduped_lines: list[tuple[float, float, float]] = []
    for item in line_items:
        if deduped_lines and abs(item[0] - deduped_lines[-1][0]) < 1.6:
            continue
        deduped_lines.append(item)
    staves: list[StaffLines] = []
    current: list[tuple[float, float, float]] = []
    for item in deduped_lines:
        if current and item[0] - current[-1][0] > 15:
            if len(current) >= 4:
                staves.append(
                    StaffLines(
                        ys=[round(y, 2) for y, _, _ in current],
                        x0=min(x0 for _, x0, _ in current),
                        x1=max(x1 for _, _, x1 in current),
                    )
                )
            current = []
        current.append(item)
    if len(current) >= 4:
        staves.append(
            StaffLines(
                ys=[round(y, 2) for y, _, _ in current],
                x0=min(x0 for _, x0, _ in current),
                x1=max(x1 for _, _, x1 in current),
            )
        )

    spacing = statistics.median([staff.spacing for staff in staves]) if staves else 5.0

    beams: list[Beam] = []
    verticals: list[Beam] = []
    for x, y, w, h in rects:
        if w <= 0 or h <= 0:
            continue
        if h <= spacing * 0.9 and w > spacing * 1.5:
            beams.append(Beam(x, y, x + w, y + h))  # beam or ledger line
        elif w <= spacing * 0.45 and h >= spacing * 2.2:
            verticals.append(Beam(x, y, x + w, y + h))  # stem or barline segment
    # Dedupe double-drawn verticals (same span, same x).
    verticals.sort(key=lambda b: (b.x0, b.y0, b.y1))
    deduped: list[Beam] = []
    for bar in verticals:
        if (
            deduped
            and abs(deduped[-1].x0 - bar.x0) <= 1.2
            and abs(deduped[-1].y0 - bar.y0) <= 1.2
            and abs(deduped[-1].y1 - bar.y1) <= 1.2
        ):
            continue
        deduped.append(bar)
    # Tall connectors (system brackets, grand-staff initial/final bars) are
    # unambiguous barlines.  A vertical spanning exactly one staff's height
    # is a per-staff barline segment, never a stem.  Anything shorter is a
    # note stem.
    true_barlines = [b for b in deduped if (b.y1 - b.y0) >= spacing * 6.0]
    bar_segments: list[Beam] = []
    true_stems: list[Beam] = []
    for bar in deduped:
        height = bar.y1 - bar.y0
        if height >= spacing * 6.0:
            continue
        near_staff_edge = any(
            abs(bar.y0 - staff.bottom) <= 1.5 and abs(bar.y1 - staff.top) <= 1.5
            for staff in staves
        )
        if near_staff_edge:
            # A full-staff-height vertical is a barline segment when a
            # sibling vertical sits at the same x on another staff; a lone
            # one is a beamed group's stem (those can span a whole staff).
            has_sibling = any(
                other is not bar
                and abs(other.x0 - bar.x0) <= 1.2
                and not (abs(other.y0 - bar.y0) <= 1.5 and abs(other.y1 - bar.y1) <= 1.5)
                and any(
                    abs(other.y0 - staff.bottom) <= 2.0 and abs(other.y1 - staff.top) <= 2.0
                    for staff in staves
                )
                for other in deduped
            )
            if has_sibling:
                bar_segments.append(bar)
            else:
                true_stems.append(bar)
        else:
            true_stems.append(bar)

    # Beams are mostly slanted quadrilateral polygons, not axis-aligned rects.
    seen_beams = {(round(b.x0, 1), round(b.y0, 1)) for b in beams}
    for poly in polys:
        if len(poly) < 4:
            continue
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        width, height = max(xs) - min(xs), max(ys) - min(ys)
        if width > spacing * 1.5 and height <= spacing * 1.6:
            key = (round(min(xs), 1), round(min(ys), 1))
            if key not in seen_beams:
                seen_beams.add(key)
                beams.append(Beam(min(xs), min(ys), max(xs), max(ys)))

    height = float(page.mediabox.height)
    return PageGeometry(
        glyphs=raw_glyphs,
        staves=staves,
        beams=beams,
        stems=true_stems,
        barlines=true_barlines,
        bar_segments=bar_segments,
        verticals=deduped,
        page_height=height,
    )


# ---------------------------------------------------------------------------
# Public probe
# ---------------------------------------------------------------------------


def probe_pdf(data: bytes) -> dict[str, object]:
    """Quick structural probe: does this PDF expose an engraved glyph layer?"""

    reader = PdfReader(BytesIO(data))
    page = reader.pages[0]
    glyphs = _page_glyphs(page)
    names = {glyph.name for glyph in glyphs}
    noteheads = sum(1 for glyph in glyphs if glyph.name in NOTEHEAD_CLASS)
    return {
        "glyph_items": len(glyphs),
        "noteheads": noteheads,
        "has_noteheads": noteheads > 10,
        "sample_names": sorted(names)[:20],
    }


# ---------------------------------------------------------------------------
# Assembly: geometry -> note events
# ---------------------------------------------------------------------------

_LETTER_TO_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_DIATONIC = "CDEFGAB"

# Steps from the staff's bottom line to the clef's reference: treble bottom
# line is E4, bass bottom line is G2, alto/tenor centre line is C4.
_CLEF_BASE = {
    "clefs.G": (4, "E"),   # bottom line = E4
    "clefs.F": (2, "G"),   # bottom line = G2
    "clefs.C": (3, "C"),   # centre line = C4 (resolved per staff position)
}

_FLAG_BEATS = {
    "flags.u3": 0.25, "flags.d3": 0.25,
    "flags.u4": 0.125, "flags.d4": 0.125,
    "flags.u5": 0.0625, "flags.d5": 0.0625,
    "flags.u6": 0.03125, "flags.d6": 0.03125,
    "flags.u7": 0.015625, "flags.d7": 0.015625,
}

# Time-signature digit text occasionally mis-decodes; validate and fall back.
_VALID_DENOMINATORS = {1, 2, 4, 8, 16}


@dataclass(slots=True)
class VectorNote:
    pitch: int
    alter: int
    measure: int            # global measure index across the score
    fraction: float         # 0..1 position inside the measure
    beats: float            # nominal duration in quarter notes
    onset_beats: float      # absolute onset in quarter notes
    staff_role: str         # 'treble' | 'bass'
    voice: int
    marks: frozenset[str]
    tie_start: bool
    x: float
    y: float


@dataclass(slots=True)
class VectorScore:
    notes: list[VectorNote]
    time_numerator: int
    time_denominator: int
    key_fifths: int
    measure_count: int
    title: str
    tempo_words: list[str]
    warnings: list[str] = field(default_factory=list)


def _clean_items(geometry: PageGeometry) -> list[Glyph]:
    """Drop transform-less Form XObject items (they sit at the origin)."""

    return [
        glyph
        for glyph in geometry.glyphs
        if not (abs(glyph.x) < 0.5 and abs(glyph.y) < 0.5)
    ]


def _staff_pitch(clef: str, staff: StaffLines, y: float) -> tuple[int, int, int]:
    """Map a y position to (midi pitch class base, octave, diatonic steps).

    Returns the natural (unaltered) midi pitch plus the diatonic step count,
    which accidental application needs.
    """

    spacing = staff.spacing
    steps = round((y - staff.ys[0]) / (spacing / 2))
    octave, letter = _CLEF_BASE.get(clef, (4, "E"))
    base_index = _DIATONIC.index(letter)
    total = base_index + steps
    out_letter = _DIATONIC[total % 7]
    out_octave = octave + total // 7
    midi = (out_octave + 1) * 12 + _LETTER_TO_PC[out_letter]
    return midi, out_octave, steps


def _detect_key_fifths(items: list[Glyph], staves: list[StaffLines]) -> int:
    """Count key-signature accidentals at the first system's start."""

    if not staves:
        return 0
    first = max(staves, key=lambda staff: staff.top)  # topmost staff
    candidates = [
        item
        for item in items
        if item.name in ACCIDENTAL_ALTER
        and first.x0 - 30 <= item.x <= first.x0 + 50
        and first.bottom - 20 <= item.y <= first.top + 20
    ]
    alters = [ACCIDENTAL_ALTER[item.name] for item in candidates]
    if not alters:
        return 0
    if all(alter == -1 for alter in alters):
        return -min(len(alters), 7)
    if all(alter == 1 for alter in alters):
        return min(len(alters), 7)
    return 0


def _detect_time_signature(items: list[Glyph], staves: list[StaffLines]) -> tuple[int, int]:
    if not staves:
        return 4, 4
    first = max(staves, key=lambda staff: staff.top)
    for item in sorted(items, key=lambda glyph: glyph.x):
        if not (first.x0 <= item.x <= first.x0 + 80):
            continue
        if not (first.bottom - 5 <= item.y <= first.top + 20):
            continue
        if re.fullmatch(r"\d{2}", item.name):
            numerator, denominator = int(item.name[0]), int(item.name[1])
            if denominator in _VALID_DENOMINATORS and 1 <= numerator <= 16:
                return numerator, denominator
            # Stacked digits mis-decode in either order; trust the common
            # meters only, otherwise the safe default applies below.
            if denominator == 4 and numerator in (2, 3, 4, 6):
                return numerator, denominator
    return 4, 4


def _nearest_staff(staves: list[StaffLines], y: float) -> StaffLines | None:
    best = None
    best_distance = 1e9
    for staff in staves:
        centre = (staff.top + staff.bottom) / 2
        distance = abs(y - centre) - (staff.top - staff.bottom) / 2
        if distance < best_distance:
            best_distance = distance
            best = staff
    return best


def _assemble(data: bytes) -> VectorScore:
    reader = PdfReader(BytesIO(data))
    all_notes: list[VectorNote] = []
    time_signature = (4, 4)
    key_fifths = 0
    measure_offset = 0
    title = ""
    tempo_words: list[str] = []
    global_measure_count = 0

    voice_cursors: dict[str, dict[int, float | None]] = {}
    for page_index, page in enumerate(reader.pages):
        geometry = _build_geometry(page)
        items = _clean_items(geometry)
        if not geometry.staves:
            continue
        spacing = statistics.median([staff.spacing for staff in geometry.staves])

        if page_index == 0:
            key_fifths = _detect_key_fifths(items, geometry.staves)
            time_signature = _detect_time_signature(items, geometry.staves)
            text_items = [
                glyph for glyph in items if not glyph.name.startswith(
                    tuple(NOTEHEAD_CLASS) + tuple(REST_CLASS)
                )
                and not glyph.name.startswith("accidentals")
                and not glyph.name.startswith("clefs")
                and not glyph.name.startswith("scripts")
                and not glyph.name.startswith("flags")
                and not glyph.name.startswith("dots")
                and not glyph.name.startswith("rests")
                and not glyph.name.startswith("brace")
                and not glyph.name.startswith("noteheads")
            ]
            words = [glyph for glyph in text_items if any(c.isalpha() for c in glyph.name)]
            words.sort(key=lambda glyph: (-glyph.y, glyph.x))
            title = next((w.name for w in words if len(w.name) > 4), "")
            tempo_words = [
                w.name for w in words
                if re.search(r"(?i)(allegro|adagio|andante|presto|largo|moderato|tempo|vivace|lento|grave|rit|accel)", w.name)
            ]

        # Systems: pair staves top-down (treble above bass).
        staves_sorted = sorted(geometry.staves, key=lambda staff: -staff.top)
        systems: list[tuple[StaffLines, StaffLines | None]] = []
        used: set[int] = set()
        for index, staff in enumerate(staves_sorted):
            if index in used:
                continue
            partner = None
            for other_index in range(index + 1, len(staves_sorted)):
                if other_index in used:
                    continue
                candidate = staves_sorted[other_index]
                gap = staff.bottom - candidate.top
                if 8 <= gap <= 130:
                    partner = candidate
                    used.add(other_index)
                    break
            systems.append((staff, partner))
            used.add(index)

        noteheads = [item for item in items if item.name in NOTEHEAD_CLASS]
        rest_glyphs = [item for item in items if item.name in REST_CLASS]
        accidentals = [item for item in items if item.name in ACCIDENTAL_ALTER]
        dots = [item for item in items if item.name == "dots.dot"]
        scripts = [item for item in items if item.name in SCRIPT_MARK]
        flags = [item for item in items if item.name in _FLAG_BEATS]
        clef_items = [item for item in items if item.name in _CLEF_BASE]

        for treble, bass in systems:
            system_staves = [("treble", treble)] + ([("bass", bass)] if bass else [])
            left = min(staff.x0 for _, staff in system_staves)
            right = max(staff.x1 for _, staff in system_staves)
            span_bottom = bass.bottom if bass else treble.bottom
            span_top = treble.top
            # Barlines belonging to this system: tall connectors, plus x
            # positions where shorter vertical segments jointly cover the
            # system's two staves (per-staff barline segments).  Other
            # systems' barlines may share the same x, so membership is
            # decided by the vertical span.
            system_bars: set[float] = {
                round(b.x0, 1)
                for b in geometry.barlines
                if b.y0 >= span_bottom - 5 and b.y1 <= span_top + 5
                and left - 8 <= b.x0 <= right + 8
            }
            # Per-staff barline segments share their x across the system's
            # staves; stems never do.  Cluster every vertical at barline
            # height by x and keep clusters whose y-union covers the system.
            segment_clusters: dict[int, list[Beam]] = {}
            for seg in geometry.verticals:
                if not (left - 8 <= seg.x0 <= right + 8):
                    continue
                if seg.y1 - seg.y0 < spacing * 3.4:
                    continue
                key = round(seg.x0 / 1.5)
                segment_clusters.setdefault(key, []).append(seg)
            span = span_top - span_bottom
            for group in segment_clusters.values():
                group.sort(key=lambda b: b.y0)
                covered = 0.0
                cur0 = cur1 = None
                for seg in group:
                    s0 = max(seg.y0, span_bottom)
                    s1 = min(seg.y1, span_top)
                    if cur0 is None:
                        cur0, cur1 = s0, s1
                        continue
                    if s0 <= cur1 + spacing * 1.6:
                        cur1 = max(cur1, s1)
                    else:
                        covered += max(0.0, cur1 - cur0)
                        cur0, cur1 = s0, s1
                if cur0 is not None:
                    covered += max(0.0, cur1 - cur0)
                if span > 0 and covered >= span * 0.75:
                    system_bars.add(round(statistics.median(seg.x0 for seg in group), 1))
            bounds = sorted({round(left, 1), round(right, 1)} | system_bars)
            # Collapse near-duplicate edges (the staff's left edge and its
            # initial barline differ by a hair).
            tight: list[float] = []
            for bound in bounds:
                if tight and bound - tight[-1] <= 3.0:
                    continue
                tight.append(bound)
            bounds = tight
            if len(bounds) < 2:
                continue
            measures_in_system = len(bounds) - 1

            for role, staff in system_staves:
                staff_clefs = sorted(
                    (
                        clef
                        for clef in clef_items
                        if staff.bottom - 30 <= clef.y <= staff.top + 30
                        and clef.x <= right + 5
                    ),
                    key=lambda glyph: glyph.x,
                )
                staff_notes = [
                    item for item in noteheads
                    if _nearest_staff([s for _, s in system_staves], item.y) is staff
                    and min(
                        abs(item.y - edge)
                        for edge in (staff.bottom, staff.top)
                    ) <= spacing * 9
                    and left - 2 <= item.x <= right + 2
                ]
                staff_rests = [
                    item for item in rest_glyphs
                    if _nearest_staff([s for _, s in system_staves], item.y) is staff
                    and staff.bottom - spacing * 2 <= item.y <= staff.top + spacing * 2
                    and left - 2 <= item.x <= right + 2
                ]
                if not staff_notes:
                    continue
                staff_notes.sort(key=lambda glyph: glyph.x)

                columns: list[list[Glyph]] = []
                for note in staff_notes:
                    if columns and note.x - columns[-1][-1].x <= spacing * 1.4:
                        columns[-1].append(note)
                    else:
                        columns.append([note])

                # Every event on this staff: note columns and rests, each
                # with a voice hint.  Onsets are then reconstructed by
                # cumulative durations per voice — engraving x positions only
                # hint at time and drift far too much to use directly.
                events: list[dict[str, object]] = []
                for column in columns:
                    col_x = statistics.median(glyph.x for glyph in column)
                    column_ys = [glyph.y for glyph in column]
                    cy0, cy1 = min(column_ys), max(column_ys)
                    stem = None
                    stem_distance = 1e9
                    for candidate in geometry.stems:
                        if abs(candidate.x0 - col_x) > spacing * 2.2 and abs(candidate.x1 - col_x) > spacing * 2.2:
                            continue
                        if candidate.y1 >= cy0 - spacing and candidate.y0 <= cy1 + spacing:
                            distance = min(abs(candidate.x0 - col_x), abs(candidate.x1 - col_x))
                            if distance < stem_distance:
                                stem_distance = distance
                                stem = candidate
                    voice = 1
                    if stem is not None:
                        stem_mid = (stem.y0 + stem.y1) / 2
                        note_mid = statistics.median(column_ys)
                        voice = 1 if stem_mid >= note_mid else 2

                    clef = "clefs.G" if role == "treble" else "clefs.F"
                    for staff_clef in staff_clefs:
                        if staff_clef.x <= col_x + 2:
                            clef = staff_clef.name

                    notes_payload = []
                    for glyph in column:
                        base_midi, _octave, _steps = _staff_pitch(clef, staff, glyph.y)
                        alter = None
                        best_distance = 1e9
                        for acc in accidentals:
                            # An accidental centres on its own staff step; a
                            # looser window would leak onto the neighbour step.
                            if abs(acc.y - glyph.y) > spacing * 0.45:
                                continue
                            if acc.x > glyph.x + spacing * 0.6 or glyph.x - acc.x > spacing * 5:
                                continue
                            distance = abs(acc.x - glyph.x) + abs(acc.y - glyph.y)
                            if distance < best_distance:
                                best_distance = distance
                                alter = ACCIDENTAL_ALTER[acc.name]
                        if alter is None:
                            alter = _key_alter_for_pitch(key_fifths, base_midi)
                        duration = _glyph_duration(glyph, geometry, spacing, flags, dots, stem)
                        marks = {
                            SCRIPT_MARK[script.name]
                            for script in scripts
                            if abs(script.x - glyph.x) <= spacing * 2.5
                            and 0 < abs(script.y - glyph.y) <= spacing * 5
                        }
                        notes_payload.append(
                            {
                                "pitch": base_midi + alter,
                                "alter": alter,
                                "beats": duration,
                                "marks": frozenset(marks),
                                "y": glyph.y,
                            }
                        )
                    events.append(
                        {
                            "x": col_x,
                            "voice": voice,
                            "rest": False,
                            "beats": max(p["beats"] for p in notes_payload),
                            "notes": notes_payload,
                        }
                    )
                staff_middle = staff.ys[2]
                for rest in staff_rests:
                    events.append(
                        {
                            "x": rest.x,
                            "voice": 1 if rest.y >= staff_middle else 2,
                            "rest": True,
                            "beats": REST_CLASS[rest.name],
                            "notes": [],
                        }
                    )

                events.sort(key=lambda event: event["x"])
                # Cumulative onsets per voice, continuous across measures and
                # systems: incomplete (pickup) measures then just work.  The
                # cursor softly resyncs to a barline when it lands within a
                # beat of one, which bounds the damage of a misread duration.
                beats_per_measure = time_signature[0] * 4.0 / time_signature[1]
                cursors = voice_cursors.setdefault(
                    role, {1: None, 2: None}
                )
                last_measure_seen: dict[int, int] = {-1: -1}
                for event in events:
                    voice = int(event["voice"])
                    x = float(event["x"])
                    measure_index = measures_in_system - 1
                    for i in range(measures_in_system):
                        if x < bounds[i + 1]:
                            measure_index = i
                            break
                    if x < bounds[0]:
                        measure_index = 0
                    global_measure = measure_offset + measure_index
                    cursor = cursors[voice]
                    measure_start = global_measure * beats_per_measure
                    if cursor is None:
                        # A voice entering late starts at its engraved x
                        # position, not at the barline — but the very first
                        # voice event of the score anchors at the barline.
                        if any(c is not None for role_cursors in voice_cursors.values() for c in role_cursors.values()):
                            m_x0, m_x1 = bounds[measure_index], bounds[measure_index + 1]
                            fraction = (x - m_x0) / max(m_x1 - m_x0, 1.0)
                            cursor = measure_start + max(0.0, min(0.999, fraction)) * beats_per_measure
                        else:
                            cursor = measure_start
                    elif (
                        last_measure_seen.get(voice, -1) != global_measure
                        and abs(cursor - measure_start) <= 0.6
                        and global_measure > 0
                    ):
                        # Resync once when the voice crosses into a new
                        # measure — never per event, or the whole measure
                        # collapses onto the barline.
                        cursor = measure_start
                    last_measure_seen[voice] = global_measure
                    absolute = cursor
                    cursors[voice] = cursor + float(event["beats"])
                    if event["rest"]:
                        continue
                    for payload in event["notes"]:
                        all_notes.append(
                            VectorNote(
                                pitch=payload["pitch"],
                                alter=payload["alter"],
                                measure=global_measure,
                                fraction=(absolute - measure_start) / beats_per_measure,
                                beats=float(payload["beats"]),
                                onset_beats=absolute,
                                staff_role=role,
                                voice=voice,
                                marks=payload["marks"],
                                tie_start=False,
                                x=x,
                                y=payload["y"],
                            )
                        )
            global_measure_count = max(global_measure_count, measure_offset + measures_in_system)
            measure_offset += measures_in_system

    if not all_notes:
        raise VectorScoreError("PDF 未包含可解析的矢量谱面（非数字制谱或字体子集化）")

    return VectorScore(
        notes=all_notes,
        time_numerator=time_signature[0],
        time_denominator=time_signature[1],
        key_fifths=key_fifths,
        measure_count=max(global_measure_count, 1),
        title=title,
        tempo_words=tempo_words,
    )


def _key_alter_for_pitch(fifths: int, midi: int) -> int:
    """Default alter from the key signature for a natural-mapped pitch."""

    sharp_order = [5, 0, 7, 2, 9, 4, 11]  # F C G D A E B (pitch classes)
    flat_order = [11, 4, 9, 2, 7, 0, 5]   # B E A D G C F
    pc = midi % 12
    if fifths > 0 and pc in sharp_order[:fifths]:
        return 1
    if fifths < 0 and pc in flat_order[:-fifths]:
        return -1
    return 0


def _glyph_duration(
    glyph: Glyph,
    geometry: PageGeometry,
    spacing: float,
    flags: list[Glyph],
    dots: list[Glyph],
    stem: Beam | None = None,
) -> float:
    cls = NOTEHEAD_CLASS[glyph.name]
    if cls == 0:
        beats = 4.0
    elif cls == 1:
        beats = 2.0
    elif cls == -1:
        beats = 1.0
    else:
        # Count beams at the stem's free end (the end away from the note);
        # beams join stems, not noteheads, so testing the notehead's own
        # position misses short groups.
        beam_hits = 0
        if stem is not None:
            head_near_top = abs(glyph.y - stem.y1) < abs(glyph.y - stem.y0)
            free_y = stem.y0 if head_near_top else stem.y1
            for beam in geometry.beams:
                if (
                    beam.x0 - spacing <= stem.x0 <= beam.x1 + spacing
                    or beam.x0 - spacing <= stem.x1 <= beam.x1 + spacing
                ) and min(abs(beam.y0 - free_y), abs(beam.y1 - free_y)) <= spacing * 1.8:
                    beam_hits += 1
        else:
            for beam in geometry.beams:
                if beam.x0 - spacing <= glyph.x <= beam.x1 + spacing and abs(beam.y0 - glyph.y) <= spacing * 9:
                    beam_hits += 1
        if beam_hits == 0:
            flag = next(
                (flag for flag in flags if abs(flag.x - glyph.x) <= spacing * 3),
                None,
            )
            beats = _FLAG_BEATS[flag.name] if flag else 1.0
        elif beam_hits == 1:
            beats = 0.5
        elif beam_hits == 2:
            beats = 0.25
        elif beam_hits == 3:
            beats = 0.125
        else:
            beats = 0.0625
    dot = next(
        (dot for dot in dots if 0 < dot.x - glyph.x <= spacing * 3 and abs(dot.y - glyph.y) <= spacing),
        None,
    )
    if dot is not None:
        beats *= 1.5
    return beats


# ---------------------------------------------------------------------------
# Synthetic MIDI + pipeline integration
# ---------------------------------------------------------------------------


def vector_score_to_midi_bytes(score: VectorScore) -> bytes:
    """Render the assembled events as a simple two-track MIDI file.

    Onsets come from the engraved x positions inside each measure; the
    notation pipeline's quantizer snaps them onto the rhythmic grid.  Nominal
    durations come from the glyphs, which are exact.
    """

    import mido

    ticks_per_beat = 480
    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    meta = mido.MidiTrack()
    meta.append(mido.MetaMessage("set_tempo", tempo=500000, time=0))
    meta.append(
        mido.MetaMessage(
            "time_signature",
            numerator=score.time_numerator,
            denominator=score.time_denominator,
            time=0,
        )
    )
    if -7 <= score.key_fifths <= 7:
        fifths = score.key_fifths
        mode = "minor" if fifths < 0 else "major"
        meta.append(mido.MetaMessage("key_signature", key=_KEY_NAMES.get((fifths, mode), "C"), time=0))
    meta.append(mido.MetaMessage("track_name", name="VectorScore", time=0))
    meta.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(meta)

    for role, track_name in (("treble", "Right"), ("bass", "Left")):
        events: list[tuple[int, int, int, int]] = []  # (tick, kind, pitch, velocity)
        for note in score.notes:
            if note.staff_role != role:
                continue
            tick = round(note.onset_beats * ticks_per_beat)
            duration = max(1, round(note.beats * ticks_per_beat))
            events.append((tick, 1, note.pitch, 80))
            events.append((tick + duration, 0, note.pitch, 0))
        events.sort(key=lambda event: (event[0], event[1], event[2]))
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=track_name, time=0))
        last = 0
        for tick, kind, pitch, velocity in events:
            pitch = max(0, min(127, pitch))
            if kind:
                track.append(mido.Message("note_on", note=pitch, velocity=velocity, time=max(0, tick - last)))
            else:
                track.append(mido.Message("note_off", note=pitch, velocity=0, time=max(0, tick - last)))
            last = max(last, tick)
        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)

    buffer = BytesIO()
    mid.save(file=buffer)
    return buffer.getvalue()


_KEY_NAMES = {
    (0, "major"): "C", (1, "major"): "G", (2, "major"): "D", (3, "major"): "A",
    (4, "major"): "E", (5, "major"): "B", (6, "major"): "F#", (7, "major"): "C#",
    (-1, "minor"): "D", (-2, "minor"): "G", (-3, "minor"): "C", (-4, "minor"): "F",
    (-5, "minor"): "Bb", (-6, "minor"): "Eb", (-7, "minor"): "Ab",
    (-1, "major"): "F", (-2, "major"): "Bb", (-3, "major"): "Eb", (-4, "major"): "Ab",
    (-5, "major"): "Db", (-6, "major"): "Gb", (-7, "major"): "Cb",
    (0, "minor"): "A", (1, "minor"): "E", (2, "minor"): "B", (3, "minor"): "F#",
    (4, "minor"): "C#", (5, "minor"): "G#", (6, "minor"): "D#", (7, "minor"): "A#",
}


def extract_score(data: bytes) -> VectorScore:
    """Public entry: parse a digitally engraved PDF into note events."""

    return _assemble(data)
