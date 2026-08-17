from __future__ import annotations

import math

from .models import KeyChange, KeyEstimate, MeasureSpan, QuantizedNote

MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]

MAJOR_FIFTHS = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}
MINOR_FIFTHS = {9: 0, 4: 1, 11: 2, 6: 3, 1: 4, 8: 5, 3: -6, 10: -5, 5: -4, 0: -3, 7: -2, 2: -1}


def estimate_key(notes: list[QuantizedNote]) -> KeyEstimate:
    return _estimate_from_histogram(_note_histogram(notes))


def estimate_key_timeline(
    notes: list[QuantizedNote],
    measures: list[MeasureSpan],
    *,
    window_measures: int = 4,
) -> list[KeyChange]:
    """Estimate a measure-anchored key timeline for modulating music.

    Audio transcriptions and many downloaded MIDI files carry no key-signature
    events at all, so a single global estimate floods modulating passages with
    avoidable accidentals.  Each measure gets a duration/velocity weighted
    pitch-class histogram; a short sliding window keeps the estimate local,
    and a Viterbi pass with a switch cost finds the globally cheapest key
    path, which suppresses the constant flicker between neighbouring keys
    (tonic/dominant and relative major/minor share six of seven pitch classes)
    while still committing to genuine, sustained modulations.
    """

    if not notes or not measures:
        return []

    global_key = estimate_key(notes)
    if len(measures) <= max(2, window_measures):
        return [KeyChange(0, global_key)]

    histograms = [[0.0] * 12 for _ in measures]
    for note in notes:
        index = _measure_index(measures, note.onset)
        histograms[index][note.pitch % 12] += max(1, note.duration) * (
            0.65 + note.velocity / 254
        )

    # Roughly three quarter-notes of weighted material: enough pitch-class
    # signal for a stable correlation, low enough that sparse codas still
    # produce an estimate instead of leaving a pending modulation unresolved.
    minimum_mass = 1440.0
    windows: list[list[float] | None] = []
    candidates: list[KeyEstimate | None] = []
    for index in range(len(measures)):
        window = [0.0] * 12
        for offset in range(window_measures):
            if index + offset < len(measures):
                for pitch_class in range(12):
                    window[pitch_class] += histograms[index + offset][pitch_class]
        if sum(window) < minimum_mass:
            windows.append(None)
            candidates.append(None)
            continue
        windows.append(window)
        candidates.append(_estimate_from_histogram(window))

    first_valid = next((key for key in candidates if key is not None), None)
    current = first_valid or global_key
    # Neighbouring keys share six of seven pitch classes, so windowed voting
    # flickers between e.g. the tonic and its dominant side every few measures.
    # Viterbi smoothing with a switch cost finds the globally cheapest key
    # path: short dominant excursions lose to staying, genuine modulations
    # (which keep paying off for many windows) win.
    path = _viterbi_key_path(windows, fallback=current)
    opening = next((key for key in path if key is not None), None) or current
    changes: list[KeyChange] = [KeyChange(0, opening)]
    active_fifths = opening.fifths
    for index, key in enumerate(path):
        if key is None or key.fifths == active_fifths:
            continue
        changes.append(KeyChange(index, key))
        active_fifths = key.fifths

    # Mixed transition windows can win as a compromise key (half C-major plus
    # half F#-major material looks like F major), and dominant-side excursions
    # of a few measures are harmony, not modulation.  Collapse any segment
    # shorter than the minimum into its predecessor; this also removes the
    # classic "A -> B -> A" flicker and transition artifacts in one pass.
    minimum_segment = max(2, min(8, len(measures) // 6))
    boundaries = [change.measure_index for change in changes] + [len(measures)]
    kept = changes[:1]
    for position, change in enumerate(changes[1:], start=1):
        segment_length = boundaries[position + 1] - boundaries[position]
        if segment_length < minimum_segment:
            continue
        if kept[-1].key.fifths == change.key.fifths:
            continue
        kept.append(change)
    changes = kept

    if len(changes) == 1:
        return [KeyChange(0, global_key)]
    return changes


_KEY_SWITCH_COST = 0.30


def _viterbi_key_path(
    windows: list[list[float] | None],
    *,
    fallback: KeyEstimate,
) -> list[KeyEstimate | None]:
    states = [(tonic, mode) for tonic in range(12) for mode in ("major", "minor")]
    vectors = [
        _rotate(MAJOR_PROFILE if mode == "major" else MINOR_PROFILE, tonic)
        for tonic, mode in states
    ]
    fallback_state = states.index((fallback.tonic_pc, fallback.mode))
    scores: list[list[float] | None] = []
    for window in windows:
        if window is None:
            scores.append(None)
            continue
        scores.append([_correlation(window, vector) for vector in vectors])

    first = next((index for index, item in enumerate(scores) if item is not None), None)
    if first is None:
        return [None] * len(windows)

    count = len(states)
    dp = [float("-inf")] * count
    dp[fallback_state] = 0.0
    back: list[list[int]] = []
    for index in range(first, len(scores)):
        emission = scores[index]
        if emission is None:
            back.append(list(range(count)))
            continue
        back.append(
            [
                max(
                    range(count),
                    key=lambda previous: dp[previous]
                    - (_KEY_SWITCH_COST if previous != state else 0.0),
                )
                for state in range(count)
            ]
        )
        dp = [
            dp[back[-1][state]] - (_KEY_SWITCH_COST if back[-1][state] != state else 0.0)
            + emission[state]
            for state in range(count)
        ]

    state = max(range(count), key=lambda item: dp[item])
    path: list[KeyEstimate | None] = [None] * len(windows)
    for index in range(len(windows) - 1, first - 1, -1):
        if scores[index] is not None:
            tonic, mode = states[state]
            fifths = (MAJOR_FIFTHS if mode == "major" else MINOR_FIFTHS)[tonic]
            path[index] = KeyEstimate(tonic, mode, fifths, 1.0)
        if index > first:
            state = back[index - first][state]
    return path


def _measure_index(measures: list[MeasureSpan], onset: int) -> int:
    low = 0
    high = len(measures)
    while low < high:
        middle = (low + high) // 2
        measure = measures[middle]
        if onset < measure.start:
            high = middle
        elif onset >= measure.end:
            low = middle + 1
        else:
            return middle
    return max(0, min(len(measures) - 1, low))


def _note_histogram(notes: list[QuantizedNote]) -> list[float]:
    histogram = [0.0] * 12
    for note in notes:
        histogram[note.pitch % 12] += max(1, note.duration) * (0.65 + note.velocity / 254)
    return histogram


def _estimate_from_histogram(histogram: list[float]) -> KeyEstimate:
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
