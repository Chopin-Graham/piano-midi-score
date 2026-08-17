from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Optional audio-analysis worker")
    subparsers = parser.add_subparsers(dest="command", required=True)
    beats = subparsers.add_parser("beats", help="Estimate beat locations with librosa")
    beats.add_argument("audio", type=Path)
    beats.add_argument("output", type=Path)
    similarity = subparsers.add_parser(
        "similarity",
        help="Chroma/onset similarity between two audio files",
    )
    similarity.add_argument("reference", type=Path)
    similarity.add_argument("candidate", type=Path)
    similarity.add_argument("output", type=Path)
    args = parser.parse_args()

    if args.command == "beats":
        _write_beats(args.audio, args.output)
    elif args.command == "similarity":
        _write_similarity(args.reference, args.candidate, args.output)


def _write_beats(audio_path: Path, output_path: Path) -> None:
    import librosa
    import numpy as np

    audio, sample_rate = librosa.load(audio_path, sr=22_050, mono=True)
    onset_envelope = librosa.onset.onset_strength(y=audio, sr=sample_rate)
    tempo, beat_times = librosa.beat.beat_track(
        onset_envelope=onset_envelope,
        sr=sample_rate,
        units="time",
        trim=False,
    )
    tempo_value = float(np.asarray(tempo).reshape(-1)[0])
    output_path.write_text(
        json.dumps(
            {
                "tempo_bpm": tempo_value,
                "beat_times": [float(value) for value in beat_times],
                "sample_rate": sample_rate,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _synthesize_midi(midi_path: Path, sample_rate: int = 22_050):
    """Minimal deterministic synth for similarity comparisons.

    Both sides of a round-trip comparison are rendered with the same simple
    tone (sine fundamentals plus two harmonics, exponential decay), so timbre
    cancels out and only pitch/onset/duration differences remain.  This avoids
    depending on MuseScore's MIDI-import tempo quirks for audio-level metrics.
    """

    import mido
    import numpy as np

    midi = mido.MidiFile(str(midi_path))
    tempo = 500_000
    seconds = 0.0
    active: dict[tuple[int, int], list[tuple[float, int]]] = {}
    notes: list[tuple[float, float, int, int]] = []
    for message in mido.merge_tracks(midi.tracks):
        seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if message.is_meta:
            if message.type == "set_tempo":
                tempo = int(message.tempo)
            continue
        if message.type == "note_on" and message.velocity > 0:
            active.setdefault((message.channel, message.note), []).append(
                (seconds, int(message.velocity))
            )
        elif message.type in {"note_off", "note_on"}:
            stack = active.get((message.channel, message.note), [])
            if stack:
                start, velocity = stack.pop(0)
                if seconds > start:
                    notes.append((start, seconds, message.note, velocity))

    if not notes:
        return np.zeros(sample_rate, dtype=np.float32)
    total_seconds = max(end for _, end, _, _ in notes) + 0.5
    audio = np.zeros(int(total_seconds * sample_rate), dtype=np.float32)
    for start, end, pitch, velocity in notes:
        frequency = 440.0 * 2 ** ((pitch - 69) / 12)
        amplitude = (velocity / 127) * 0.3
        begin = int(start * sample_rate)
        stop = min(len(audio), int((end + 0.25) * sample_rate))
        if stop <= begin:
            continue
        time_axis = np.arange(stop - begin) / sample_rate
        held = min(end - start, 2.0)
        envelope = np.minimum(time_axis / 0.01, 1.0) * np.exp(
            -time_axis / max(held * 0.9, 0.08)
        )
        tone = np.sin(2 * np.pi * frequency * time_axis)
        tone += 0.4 * np.sin(2 * np.pi * frequency * 2 * time_axis)
        tone += 0.15 * np.sin(2 * np.pi * frequency * 3 * time_axis)
        audio[begin:stop] += amplitude * envelope * tone.astype(np.float32)
    return audio


def _write_similarity(reference_path: Path, candidate_path: Path, output_path: Path) -> None:
    """Perceptual similarity between two renderings of the same music.

    Both files are loaded mono at 22.05 kHz, loudness-normalized, and aligned
    by cross-correlating their onset envelopes (this absorbs different leading
    silences and small global offsets).  We then report:

    - ``chroma_cosine``: mean per-frame cosine similarity of CENS chromagrams,
      which captures pitch-class content over time while ignoring timbre;
    - ``onset_correlation``: Pearson correlation of the aligned onset
      envelopes, which captures rhythmic placement of attacks.
    """

    import librosa
    import numpy as np

    sample_rate = 22_050

    def load(path: Path):
        if path.suffix.lower() in {".mid", ".midi"}:
            return _synthesize_midi(path, sample_rate)
        audio, _ = librosa.load(path, sr=sample_rate, mono=True)
        return audio

    reference = load(reference_path)
    candidate = load(candidate_path)

    def normalize(audio):
        peak = float(np.max(np.abs(audio))) if audio.size else 0.0
        return audio / peak if peak > 0 else audio

    reference = normalize(reference)
    candidate = normalize(candidate)

    reference_onset = librosa.onset.onset_strength(y=reference, sr=sample_rate)
    candidate_onset = librosa.onset.onset_strength(y=candidate, sr=sample_rate)

    # Coarse alignment: full cross-correlation of onset envelopes.
    correlation = np.correlate(reference_onset, candidate_onset, mode="full")
    lag = int(np.argmax(correlation) - (len(candidate_onset) - 1))
    if lag > 0:
        aligned_reference = reference_onset[lag:]
        aligned_candidate = candidate_onset[: len(aligned_reference)]
    elif lag < 0:
        aligned_candidate = candidate_onset[-lag:]
        aligned_reference = reference_onset[: len(aligned_candidate)]
    else:
        aligned_reference = reference_onset
        aligned_candidate = candidate_onset
    frames = min(len(aligned_reference), len(aligned_candidate))
    aligned_reference = aligned_reference[:frames]
    aligned_candidate = aligned_candidate[:frames]

    def pearson(left, right):
        if left.size < 2 or not np.any(left) or not np.any(right):
            return 0.0
        return float(np.corrcoef(left, right)[0, 1])

    onset_correlation = pearson(aligned_reference, aligned_candidate)

    reference_chroma = librosa.feature.chroma_cens(y=reference, sr=sample_rate)
    candidate_chroma = librosa.feature.chroma_cens(y=candidate, sr=sample_rate)
    if lag > 0:
        reference_chroma = reference_chroma[:, lag:]
    elif lag < 0:
        candidate_chroma = candidate_chroma[:, -lag:]
    frames = min(reference_chroma.shape[1], candidate_chroma.shape[1])
    reference_chroma = reference_chroma[:, :frames]
    candidate_chroma = candidate_chroma[:, :frames]
    norms = np.linalg.norm(reference_chroma, axis=0) * np.linalg.norm(
        candidate_chroma, axis=0
    )
    valid = norms > 1e-6
    chroma_cosine = (
        float(
            np.mean(
                np.sum(reference_chroma[:, valid] * candidate_chroma[:, valid], axis=0)
                / norms[valid]
            )
        )
        if np.any(valid)
        else 0.0
    )

    output_path.write_text(
        json.dumps(
            {
                "chroma_cosine": round(chroma_cosine, 4),
                "onset_correlation": round(onset_correlation, 4),
                "alignment_lag_frames": lag,
                "reference_duration_s": round(len(reference) / sample_rate, 3),
                "candidate_duration_s": round(len(candidate) / sample_rate, 3),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
