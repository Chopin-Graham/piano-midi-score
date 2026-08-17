from app.core.models import KeyEstimate, QuantizedNote
from app.core.spelling import apply_pitch_spelling


def _note(pitch: int) -> QuantizedNote:
    return QuantizedNote(1, pitch, 0, 480, 80, 0, 0)


def test_flat_key_prefers_d_flat_over_c_sharp() -> None:
    notes, analysis, _ = apply_pitch_spelling(
        [_note(61)],
        KeyEstimate(tonic_pc=8, mode="major", fifths=-4, confidence=1.0),
    )

    assert (notes[0].pitch_step, notes[0].pitch_alter) == ("D", -1)
    assert analysis["double_accidental_notes"] == 0


def test_context_can_keep_c_sharp_in_d_minor() -> None:
    notes, _, _ = apply_pitch_spelling(
        [_note(61), _note(62), _note(61), _note(62)],
        KeyEstimate(tonic_pc=2, mode="minor", fifths=-1, confidence=1.0),
    )

    assert all(
        (note.pitch_step, note.pitch_alter) == ("C", 1)
        for note in notes
        if note.pitch == 61
    )


def test_sharp_key_uses_key_signature_spelling() -> None:
    notes, _, _ = apply_pitch_spelling(
        [_note(63), _note(66), _note(68)],
        KeyEstimate(tonic_pc=4, mode="major", fifths=4, confidence=1.0),
    )

    assert [(note.pitch_step, note.pitch_alter) for note in notes] == [
        ("D", 1),
        ("F", 1),
        ("G", 1),
    ]
