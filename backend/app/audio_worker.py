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
    args = parser.parse_args()

    if args.command == "beats":
        _write_beats(args.audio, args.output)


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


if __name__ == "__main__":
    main()
