from __future__ import annotations

from textwrap import dedent

import pytest

from app.core.score_omr import normalize_omr_musicxml, parse_omr_notes


def _score(measures: str) -> str:
    return dedent(
        f"""
        <score-partwise version="4.0">
          <part-list>
            <score-part id="P1"><part-name>Piano</part-name></score-part>
          </part-list>
          <part id="P1">
            {measures}
          </part>
        </score-partwise>
        """
    )


def _attributes() -> str:
    return """
      <attributes>
        <divisions>1</divisions>
        <time><beats>4</beats><beat-type>4</beat-type></time>
      </attributes>
    """


def _note(
    step: str,
    octave: int,
    duration: int,
    *,
    voice: int = 1,
    staff: int = 1,
    extra: str = "",
) -> str:
    return f"""
      <note>
        {extra}
        <pitch><step>{step}</step><octave>{octave}</octave></pitch>
        <duration>{duration}</duration><voice>{voice}</voice><staff>{staff}</staff>
      </note>
    """


def test_backup_uses_one_measure_cursor_without_shifting_later_bars() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("C", 4, 4)}
          <backup><duration>4</duration></backup>
          {_note("C", 3, 4, voice=2, staff=2)}
        </measure>
        <measure number="2">
          {_note("D", 4, 4)}
          <backup><duration>4</duration></backup>
          {_note("D", 3, 4, voice=2, staff=2)}
        </measure>
        """
    )

    notes = parse_omr_notes(musicxml)

    second_measure = [note for note in notes if note.pitch in {50, 62}]
    assert len(second_measure) == 2
    assert {note.onset for note in second_measure} == {4.0}


def test_chord_notes_share_the_base_note_onset() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("C", 4, 1)}
          {_note("E", 4, 1, extra="<chord/>")}
          <note><rest/><duration>3</duration><voice>1</voice><staff>1</staff></note>
        </measure>
        """
    )

    notes = parse_omr_notes(musicxml)

    assert [note.pitch for note in notes] == [60, 64]
    assert [note.onset for note in notes] == [0.0, 0.0]


def test_ties_merge_across_measure_boundaries() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("G", 4, 4, extra='<tie type="start"/>')}
        </measure>
        <measure number="2">
          {_note("G", 4, 2, extra='<tie type="stop"/>')}
          <note><rest/><duration>2</duration><voice>1</voice><staff>1</staff></note>
        </measure>
        """
    )

    notes = parse_omr_notes(musicxml)

    assert len(notes) == 1
    assert notes[0].onset == 0.0
    assert notes[0].duration == pytest.approx(6.0)


def test_orphan_ending_is_removed_when_the_part_has_no_repeat() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("C", 4, 4)}
          <barline location="right"><ending number="1" type="start"/></barline>
        </measure>
        """
    )

    normalized, analysis = normalize_omr_musicxml(musicxml)

    assert "<ending" not in normalized
    assert analysis["removed_orphan_endings"] == 1
    assert analysis["semantic_rebuild_recommended"] is True


def test_severe_measure_overflow_requests_semantic_rebuild() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("C", 4, 6)}
        </measure>
        """
    )

    normalized, analysis = normalize_omr_musicxml(musicxml)

    assert normalized == musicxml
    assert analysis["severe_measure_timing_anomalies"] == 1
    assert analysis["semantic_rebuild_recommended"] is True


def test_healthy_musicxml_is_left_untouched() -> None:
    musicxml = _score(
        f"""
        <measure number="1">
          {_attributes()}
          {_note("C", 4, 4)}
          <barline location="right"><repeat direction="backward"/></barline>
        </measure>
        """
    )

    normalized, analysis = normalize_omr_musicxml(musicxml)

    assert normalized == musicxml
    assert analysis == {
        "removed_orphan_endings": 0,
        "measure_timing_anomalies": 0,
        "severe_measure_timing_anomalies": 0,
        "semantic_rebuild_recommended": False,
    }
