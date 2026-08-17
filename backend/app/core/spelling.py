from __future__ import annotations

import numpy as np
from partitura.musicanalysis import estimate_spelling

from .meter_map import measure_index_at
from .models import CANONICAL_DIVISIONS, KeyChange, KeyEstimate, MeasureSpan, QuantizedNote

_STEP_PITCH_CLASSES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_SHARP_ORDER = ("F", "C", "G", "D", "A", "E", "B")
_FLAT_ORDER = ("B", "E", "A", "D", "G", "C", "F")


def apply_pitch_spelling(
    notes: list[QuantizedNote],
    key: KeyEstimate,
    key_changes: list[KeyChange] | None = None,
    measures: list[MeasureSpan] | None = None,
) -> tuple[list[QuantizedNote], dict[str, object], list[str]]:
    """Spell pitches with PS13, then normalize clear enharmonics to the active key."""

    if not notes:
        return notes, {"method": "partitura_ps13", "note_count": 0}, []

    note_array = np.zeros(
        len(notes),
        dtype=[("pitch", "i4"), ("onset_beat", "f8"), ("duration_beat", "f8")],
    )
    note_array["pitch"] = [note.pitch for note in notes]
    note_array["onset_beat"] = [note.onset / CANONICAL_DIVISIONS for note in notes]
    note_array["duration_beat"] = [note.duration / CANONICAL_DIVISIONS for note in notes]

    try:
        spellings = estimate_spelling(note_array, method="ps13s1")
    except (TypeError, ValueError, ArithmeticError) as exc:
        return (
            notes,
            {"method": "key_signature_fallback", "note_count": len(notes)},
            [f"PS13 音高拼写未能完成，已按调号回退：{exc}"],
        )

    spelled: list[QuantizedNote] = []
    enharmonic_adjustments = 0
    active_keys: list[KeyEstimate] = []
    for note, spelling in zip(notes, spellings, strict=True):
        active_key = _active_key(note, key, key_changes or [], measures or [])
        active_keys.append(active_key)
        original = (
            str(spelling["step"]),
            int(spelling["alter"]),
            int(spelling["octave"]),
        )
        normalized = _key_aware_spelling(note.pitch, original, active_key)
        enharmonic_adjustments += normalized != original
        spelled.append(note.with_spelling(*normalized))

    altered = sum(note.pitch_alter != 0 for note in spelled)
    double_accidentals = sum(abs(note.pitch_alter) > 1 for note in spelled)
    opposing_accidentals = sum(
        (active_key.fifths < 0 and note.pitch_alter > 0)
        or (active_key.fifths > 0 and note.pitch_alter < 0)
        for note, active_key in zip(spelled, active_keys, strict=True)
    )
    warnings: list[str] = []
    if double_accidentals:
        warnings.append(
            f"仍有 {double_accidentals} 个双升降音无法在当前调号下安全简化，请复核和声语义"
        )
    return (
        spelled,
        {
            "method": "partitura_ps13",
            "note_count": len(spelled),
            "altered_note_count": altered,
            "enharmonic_adjustments": enharmonic_adjustments,
            "double_accidental_notes": double_accidentals,
            "opposing_accidental_notes": opposing_accidentals,
        },
        warnings,
    )


def _active_key(
    note: QuantizedNote,
    default: KeyEstimate,
    changes: list[KeyChange],
    measures: list[MeasureSpan],
) -> KeyEstimate:
    if not changes or not measures:
        return default
    measure_index = measure_index_at(measures, note.onset)
    selected = default
    for change in changes:
        if change.measure_index > measure_index:
            break
        selected = change.key
    return selected


def _key_aware_spelling(
    pitch: int,
    original: tuple[str, int, int],
    key: KeyEstimate,
) -> tuple[str, int, int]:
    key_alters = _key_signature_alters(key.fifths)
    original_step, original_alter, _ = original
    candidates: list[tuple[float, str, int, int]] = []
    for step, pitch_class in _STEP_PITCH_CLASSES.items():
        for alter in range(-2, 3):
            if (pitch_class + alter) % 12 != pitch % 12:
                continue
            octave_numerator = pitch - pitch_class - alter
            if octave_numerator % 12:
                continue
            octave = octave_numerator // 12 - 1
            key_alter = key_alters[step]
            cost = abs(alter - key_alter) * 2.0
            cost += max(0, abs(alter) - 1) * 8.0
            if (key.fifths < 0 and alter > 0) or (key.fifths > 0 and alter < 0):
                cost += 0.7
            if step == original_step and alter == original_alter:
                cost -= 1.3
            elif step == original_step:
                cost -= 0.2
            candidates.append((cost, step, alter, octave))
    if not candidates:
        return original
    _, step, alter, octave = min(candidates)
    return step, alter, octave


def _key_signature_alters(fifths: int) -> dict[str, int]:
    result = {step: 0 for step in _STEP_PITCH_CLASSES}
    if fifths > 0:
        for step in _SHARP_ORDER[:fifths]:
            result[step] = 1
    elif fifths < 0:
        for step in _FLAT_ORDER[: -fifths]:
            result[step] = -1
    return result
