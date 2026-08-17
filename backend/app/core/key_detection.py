from __future__ import annotations

import math

from .models import KeyEstimate, QuantizedNote

MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

MAJOR_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}
MINOR_FIFTHS = {9: 0, 4: 1, 11: 2, 6: 3, 1: 4, 8: 5, 3: -6, 10: -5, 5: -4, 0: -3, 7: -2, 2: -1}


def estimate_key(notes: list[QuantizedNote]) -> KeyEstimate:
    histogram = [0.0] * 12
    for note in notes:
        histogram[note.pitch % 12] += max(1, note.duration) * (0.65 + note.velocity / 254)

    candidates: list[tuple[float, int, str]] = []
    for tonic in range(12):
        candidates.append((_correlation(histogram, _rotate(MAJOR_PROFILE, tonic)), tonic, "major"))
        candidates.append((_correlation(histogram, _rotate(MINOR_PROFILE, tonic)), tonic, "minor"))
    candidates.sort(reverse=True)
    best_score, tonic, mode = candidates[0]
    runner_up = candidates[1][0]
    confidence = max(0.0, min(1.0, (best_score - runner_up + 0.05) / 0.30))
    fifths = (MAJOR_FIFTHS if mode == "major" else MINOR_FIFTHS)[tonic]
    return KeyEstimate(tonic, mode, fifths, round(confidence, 3))


def key_from_midi_name(name: str) -> KeyEstimate | None:
    normalized = name.strip().replace("♭", "b").replace("♯", "#")
    minor = normalized.endswith("m")
    tonic_name = normalized[:-1] if minor else normalized
    names = {
        "C": 0,
        "C#": 1,
        "Db": 1,
        "D": 2,
        "D#": 3,
        "Eb": 3,
        "E": 4,
        "F": 5,
        "F#": 6,
        "Gb": 6,
        "G": 7,
        "G#": 8,
        "Ab": 8,
        "A": 9,
        "A#": 10,
        "Bb": 10,
        "B": 11,
    }
    tonic = names.get(tonic_name)
    if tonic is None:
        return None
    mode = "minor" if minor else "major"
    fifths = (MINOR_FIFTHS if minor else MAJOR_FIFTHS)[tonic]
    return KeyEstimate(tonic, mode, fifths, 1.0)


def _rotate(profile: list[float], tonic: int) -> list[float]:
    return [profile[(pitch_class - tonic) % 12] for pitch_class in range(12)]


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum(
        (a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True)
    )
    left_norm = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_norm = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return numerator / (left_norm * right_norm)
