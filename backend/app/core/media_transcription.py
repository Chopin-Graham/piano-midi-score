from __future__ import annotations

import bisect
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from math import log2
from pathlib import Path
from statistics import median

import mido

from .models import CANONICAL_DIVISIONS
from .options import TranscriptionOptions

PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEDIA_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
}
FFMPEG_ENV = "PIANO_MIDI_SCORE_FFMPEG"
AUDIO_PYTHON_ENV = "PIANO_MIDI_SCORE_AUDIO_PYTHON"
TRANSCRIPTION_TIMEOUT_SECONDS = 60 * 60


class MediaTranscriptionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MediaTranscriptionResult:
    midi_bytes: bytes
    raw_midi_bytes: bytes
    analysis: dict[str, object]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class _TimedNote:
    pitch: int
    start: float
    end: float
    velocity: int


@dataclass(frozen=True, slots=True)
class _TimedPedal:
    time: float
    down: bool


@dataclass(frozen=True, slots=True)
class _BeatCandidate:
    method: str
    beat_times: tuple[float, ...]


def transcribe_media(
    data: bytes,
    filename: str,
    options: TranscriptionOptions | None = None,
) -> MediaTranscriptionResult:
    options = options or TranscriptionOptions()
    extension = Path(filename).suffix.lower()
    if extension not in MEDIA_EXTENSIONS:
        raise MediaTranscriptionError(f"Unsupported audio/video extension: {extension or '(none)'}")
    if not data:
        raise MediaTranscriptionError("The uploaded media file is empty")

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        raise MediaTranscriptionError(
            "FFmpeg was not found; install FFmpeg or set PIANO_MIDI_SCORE_FFMPEG"
        )
    audio_python = find_audio_python()
    backend = _select_backend(options.backend, audio_python)
    device = _select_device(options.device, backend, audio_python)
    warnings: list[str] = []
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="piano-media-transcription-") as temporary:
        workdir = Path(temporary)
        source_path = workdir / f"source{extension}"
        audio_path = workdir / "piano-mono-44100.wav"
        raw_midi_path = workdir / "raw-transcription.mid"
        source_path.write_bytes(data)
        _prepare_audio(ffmpeg, source_path, audio_path)
        duration_seconds = _wav_duration(audio_path)

        _run_backend(
            backend,
            audio_python,
            audio_path,
            raw_midi_path,
            device,
            options,
        )
        raw_midi_bytes = raw_midi_path.read_bytes()
        if not raw_midi_bytes.startswith(b"MThd"):
            raise MediaTranscriptionError(f"{backend} did not produce a valid MIDI file")

        beat_times: list[float] = []
        beat_tempo: float | None = None
        beat_candidates: list[_BeatCandidate] = []
        if options.align_beats:
            try:
                beat_times, beat_tempo, beat_candidates = _estimate_beats(
                    audio_python,
                    audio_path,
                    workdir,
                )
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                warnings.append(
                    f"Beat alignment was unavailable ({exc}); retained a constant-tempo MIDI timeline"
                )

        midi_bytes, postprocess, postprocess_warnings = _postprocess_midi(
            raw_midi_bytes,
            beat_times,
            options.minimum_note_ms,
            backend,
            beat_candidates=beat_candidates,
        )
        warnings.extend(postprocess_warnings)

    analysis = {
        "backend": backend,
        "device": device,
        "audio_python": str(audio_python),
        "ffmpeg": str(ffmpeg),
        "source_extension": extension,
        "source_bytes": len(data),
        "duration_seconds": round(duration_seconds, 3),
        "beat_detection": bool(beat_times),
        "beat_alignment": str(postprocess["alignment_method"]).startswith("librosa_"),
        "beat_count": len(beat_times),
        "beat_candidate_count": (1 + len(beat_candidates)) if beat_times else 0,
        "detected_beat_tempo_bpm": (
            round(beat_tempo, 3) if beat_tempo is not None else None
        ),
        "estimated_tempo_bpm": postprocess["tempo_bpm"],
        "processing_ms": round((time.perf_counter() - started) * 1000, 2),
        **postprocess,
    }
    return MediaTranscriptionResult(
        midi_bytes=midi_bytes,
        raw_midi_bytes=raw_midi_bytes,
        analysis=analysis,
        warnings=list(dict.fromkeys(warnings)),
    )


@lru_cache(maxsize=1)
def find_ffmpeg() -> Path | None:
    configured = os.environ.get(FFMPEG_ENV)
    candidates = [
        configured,
        shutil.which("ffmpeg"),
        (
            Path.home()
            / "AppData/Local/Microsoft/WinGet/Packages"
            / "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            / "ffmpeg-8.1.2-full_build/bin/ffmpeg.exe"
        ),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return None


@lru_cache(maxsize=1)
def find_audio_python() -> Path:
    configured = os.environ.get(AUDIO_PYTHON_ENV)
    candidates = [
        configured,
        PROJECT_ROOT / ".venv-audio/Scripts/python.exe",
        sys.executable,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file():
            return path.resolve()
    return Path(sys.executable).resolve()


@lru_cache(maxsize=1)
def transcription_status() -> dict[str, object]:
    ffmpeg = find_ffmpeg()
    audio_python = find_audio_python()
    transkun = _python_module_available(audio_python, "transkun")
    basic_pitch = _python_module_available(audio_python, "basic_pitch")
    return {
        "available": ffmpeg is not None and (transkun or basic_pitch),
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "audio_python": str(audio_python),
        "preferred_backend": "transkun" if transkun else "basic_pitch" if basic_pitch else None,
        "backends": {
            "transkun": {
                "available": transkun,
                "purpose": "piano-specific expressive transcription",
                "license": "MIT",
            },
            "basic_pitch": {
                "available": basic_pitch,
                "purpose": "lightweight cross-platform fallback",
                "license": "Apache-2.0",
            },
        },
    }


def _python_module_available(python: Path, module: str) -> bool:
    if python == Path(sys.executable).resolve():
        return importlib.util.find_spec(module) is not None
    completed = subprocess.run(
        [
            str(python),
            "-c",
            f"import importlib.util; raise SystemExit(0 if importlib.util.find_spec('{module}') else 1)",
        ],
        check=False,
        capture_output=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode == 0


def _select_backend(requested: str, audio_python: Path) -> str:
    available = {
        "transkun": _python_module_available(audio_python, "transkun"),
        "basic_pitch": _python_module_available(audio_python, "basic_pitch"),
    }
    if requested != "auto":
        if not available[requested]:
            raise MediaTranscriptionError(
                f"Requested transcription backend '{requested}' is not installed in {audio_python}"
            )
        return requested
    for candidate in ("transkun", "basic_pitch"):
        if available[candidate]:
            return candidate
    raise MediaTranscriptionError(
        "No transcription backend is installed; install the audio-transkun or audio-basic-pitch extra"
    )


def _select_device(requested: str, backend: str, audio_python: Path) -> str:
    if backend == "basic_pitch":
        return "cpu"
    cuda_available = _torch_cuda_available(audio_python)
    if requested == "cuda" and not cuda_available:
        raise MediaTranscriptionError("CUDA was requested but is not available to the audio Python")
    if requested == "cpu":
        return "cpu"
    return "cuda" if cuda_available else "cpu"


def _torch_cuda_available(audio_python: Path) -> bool:
    completed = subprocess.run(
        [
            str(audio_python),
            "-c",
            "import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)",
        ],
        check=False,
        capture_output=True,
        timeout=90,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode == 0


def _prepare_audio(ffmpeg: Path, source_path: Path, audio_path: Path) -> None:
    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map",
        "0:a:0",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        str(audio_path),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=600,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not audio_path.is_file():
        detail = (completed.stderr or completed.stdout or "unknown FFmpeg error").strip()
        raise MediaTranscriptionError(f"FFmpeg could not extract audio: {detail[:500]}")


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def _run_backend(
    backend: str,
    audio_python: Path,
    audio_path: Path,
    raw_midi_path: Path,
    device: str,
    options: TranscriptionOptions,
) -> None:
    if backend == "transkun":
        command = [
            str(audio_python),
            "-m",
            "transkun.transcribe",
            str(audio_path),
            str(raw_midi_path),
            "--device",
            device,
        ]
        output_dir = None
    else:
        output_dir = raw_midi_path.parent / "basic-pitch-output"
        output_dir.mkdir()
        command = [
            str(audio_python),
            "-m",
            "basic_pitch.predict",
            str(output_dir),
            str(audio_path),
            "--model-serialization",
            "onnx",
            "--onset-threshold",
            str(options.onset_threshold),
            "--frame-threshold",
            str(options.frame_threshold),
            "--minimum-note-length",
            str(options.minimum_note_ms),
            "--minimum-frequency",
            "27.5",
            "--maximum-frequency",
            "4186.01",
        ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "unknown backend error").strip()
        raise MediaTranscriptionError(f"{backend} transcription failed: {detail[-1500:]}")
    if output_dir is not None:
        candidates = sorted(output_dir.glob("*.mid")) + sorted(output_dir.glob("*.midi"))
        if not candidates:
            raise MediaTranscriptionError("Basic Pitch completed without a MIDI output")
        shutil.copyfile(candidates[0], raw_midi_path)
    if not raw_midi_path.is_file():
        raise MediaTranscriptionError(f"{backend} completed without a MIDI output")


def _estimate_beats(
    audio_python: Path,
    audio_path: Path,
    workdir: Path,
) -> tuple[list[float], float | None, list[_BeatCandidate]]:
    output = workdir / "beats.json"
    worker = PROJECT_ROOT / "backend/app/audio_worker.py"
    completed = subprocess.run(
        [str(audio_python), str(worker), "beats", str(audio_path), str(output)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0 or not output.is_file():
        detail = (completed.stderr or completed.stdout or "beat worker failed").strip()
        raise RuntimeError(detail[-800:])
    payload = json.loads(output.read_text(encoding="utf-8"))
    beats = sorted({float(value) for value in payload.get("beat_times", []) if value >= 0})
    if len(beats) < 4:
        raise ValueError("fewer than four reliable beats were detected")
    alternatives: list[_BeatCandidate] = []
    primary = tuple(beats)
    for item in payload.get("beat_candidates", []):
        if not isinstance(item, dict):
            continue
        method = str(item.get("method", "")).strip()
        candidate = tuple(
            sorted(
                {
                    float(value)
                    for value in item.get("beat_times", [])
                    if float(value) >= 0
                }
            )
        )
        if (
            not method
            or not method.startswith("librosa_")
            or not method.endswith("_warp")
            or method == "librosa_dynamic_beat_warp"
            or len(candidate) < 4
            or candidate == primary
        ):
            continue
        alternatives.append(_BeatCandidate(method, candidate))
    return beats, float(payload.get("tempo_bpm", 0.0)) or None, alternatives


def _postprocess_midi(
    raw_midi_bytes: bytes,
    beat_times: list[float],
    minimum_note_ms: float,
    backend: str,
    *,
    beat_candidates: list[_BeatCandidate] | None = None,
) -> tuple[bytes, dict[str, object], list[str]]:
    notes, pedals, source_tempo = _timed_events(raw_midi_bytes)
    cleaned, cleaning = _clean_notes(notes, minimum_note_ms / 1000)
    if not cleaned:
        raise MediaTranscriptionError("The transcription contained no usable piano notes")

    aligned, attack_analysis = _align_attack_columns(
        cleaned,
        _attack_column_window(cleaned, source_tempo),
    )
    mapper, tempo_bpm, alignment_method, tempo_analysis = _select_timeline_mapper(
        beat_times,
        aligned,
        source_tempo,
        beat_candidates=beat_candidates,
    )
    meter_numerator, meter_denominator, downbeat_phase = _estimate_meter_and_downbeat(
        aligned, mapper
    )
    if meter_denominator == 8:
        # The tracker locked to the dotted-quarter beat: rescale into
        # quarter-note units (one tracked beat = 1.5 quarters) and express the
        # tempo per quarter so real-time seconds survive the rewrite.
        base_mapper = mapper

        def mapper(seconds: float) -> float:
            return base_mapper(seconds) * 1.5

        tempo_bpm *= 1.5
        downbeat_phase *= 1.5
    measure_quarters = meter_numerator * 4.0 / meter_denominator
    if downbeat_phase:
        phase_mapper = mapper

        def mapper(seconds: float) -> float:
            # Shift barlines onto the detected downbeats; the extra measure
            # keeps positions positive so pickup material lands in bar one and
            # the notation pipeline can reframe it as an anacrusis.
            return max(
                0.0,
                phase_mapper(seconds) - downbeat_phase + measure_quarters,
            )

    tempo_events = _mapper_tempo_events(mapper, aligned, tempo_bpm)
    midi_bytes = _write_aligned_midi(
        aligned,
        pedals,
        mapper,
        tempo_bpm,
        backend,
        meter_numerator=meter_numerator,
        meter_denominator=meter_denominator,
        tempo_events=tempo_events or None,
    )
    duration = max(note.end for note in aligned)
    warnings: list[str] = []
    note_density = len(aligned) / max(duration, 1.0)
    if note_density > 18:
        warnings.append(
            f"The transcription is unusually dense ({note_density:.1f} notes/s); review for false positives"
        )
    if beat_times and not alignment_method.startswith("librosa_"):
        warnings.append(
            "The detected beat grid did not improve the transcription attack alignment; "
            f"used a stable {tempo_bpm:.1f} BPM timeline instead"
        )
    if (meter_numerator, meter_denominator) != (4, 4):
        warnings.append(
            f"Accent analysis suggests a {meter_numerator}/{meter_denominator} meter; "
            "barlines follow the detected downbeats"
        )
    elif downbeat_phase:
        warnings.append(
            "Barlines were shifted onto the detected downbeats; the opening bar becomes a pickup"
        )
    if cleaning["removed_short_notes"] > max(20, round(len(notes) * 0.08)):
        warnings.append(
            f"Removed {cleaning['removed_short_notes']} sub-frame or very low-velocity artifacts"
        )
    return (
        midi_bytes,
        {
            "raw_note_count": len(notes),
            "clean_note_count": len(aligned),
            "pitch_min": min(note.pitch for note in aligned),
            "pitch_max": max(note.pitch for note in aligned),
            "tempo_bpm": round(tempo_bpm, 3),
            "alignment_method": alignment_method,
            "detected_meter": f"{meter_numerator}/{meter_denominator}",
            "downbeat_phase_beats": downbeat_phase,
            **cleaning,
            **attack_analysis,
            **tempo_analysis,
        },
        warnings,
    )


def _timed_events(raw_midi_bytes: bytes) -> tuple[list[_TimedNote], list[_TimedPedal], float]:
    midi = mido.MidiFile(file=BytesIO(raw_midi_bytes))
    tempo = 500_000
    initial_tempo = tempo
    seconds = 0.0
    active: dict[tuple[int, int], list[tuple[float, int]]] = {}
    notes: list[_TimedNote] = []
    pedals: list[_TimedPedal] = []
    for message in mido.merge_tracks(midi.tracks):
        seconds += mido.tick2second(message.time, midi.ticks_per_beat, tempo)
        if message.is_meta and message.type == "set_tempo":
            tempo = int(message.tempo)
            if not notes:
                initial_tempo = tempo
            continue
        if message.is_meta:
            continue
        if message.type == "note_on" and message.velocity > 0:
            active.setdefault((message.channel, message.note), []).append(
                (seconds, int(message.velocity))
            )
        elif message.type in {"note_off", "note_on"}:
            stack = active.get((message.channel, message.note), [])
            if not stack:
                continue
            start, velocity = stack.pop(0)
            if seconds > start:
                notes.append(_TimedNote(message.note, start, seconds, velocity))
        elif message.type == "control_change" and message.control == 64:
            pedals.append(_TimedPedal(seconds, message.value >= 64))
    return notes, pedals, mido.tempo2bpm(initial_tempo)


def _clean_notes(
    notes: list[_TimedNote],
    minimum_seconds: float,
) -> tuple[list[_TimedNote], dict[str, int]]:
    removed_out_of_range = sum(not 21 <= note.pitch <= 108 for note in notes)
    in_range = [note for note in notes if 21 <= note.pitch <= 108]
    hard_artifact_floor = min(0.020, minimum_seconds * 0.45)
    removed_short = 0
    normalized_short = 0
    candidates: list[_TimedNote] = []
    for note in in_range:
        duration = note.end - note.start
        # Transcription offset estimates are much less reliable than attacks. A
        # strong 30–50 ms staccato can be a real note, especially in virtuoso
        # piano, so preserve its onset and only normalize the release. Delete
        # only sub-frame blips or extremely soft short detections.
        if duration < hard_artifact_floor or (
            duration < minimum_seconds and note.velocity <= 32
        ):
            removed_short += 1
            continue
        if duration < minimum_seconds:
            candidates.append(
                _TimedNote(
                    pitch=note.pitch,
                    start=note.start,
                    end=note.start + minimum_seconds,
                    velocity=note.velocity,
                )
            )
            normalized_short += 1
            continue
        candidates.append(note)

    deduplicated: list[_TimedNote] = []
    duplicate_count = 0
    for note in sorted(candidates, key=lambda item: (item.pitch, item.start, item.end)):
        if (
            deduplicated
            and deduplicated[-1].pitch == note.pitch
            and abs(deduplicated[-1].start - note.start)
            <= max(0.025, minimum_seconds * 0.75)
        ):
            previous = deduplicated[-1]
            deduplicated[-1] = _TimedNote(
                pitch=note.pitch,
                start=min(previous.start, note.start),
                end=max(previous.end, note.end),
                velocity=max(previous.velocity, note.velocity),
            )
            duplicate_count += 1
        else:
            deduplicated.append(note)

    by_pitch: dict[int, list[_TimedNote]] = {}
    for note in deduplicated:
        by_pitch.setdefault(note.pitch, []).append(note)
    result: list[_TimedNote] = []
    overlap_repairs = 0
    for pitch_notes in by_pitch.values():
        previous: _TimedNote | None = None
        for note in pitch_notes:
            if previous is not None and note.start < previous.end:
                if note.start - previous.start < minimum_seconds:
                    merged = _TimedNote(
                        previous.pitch,
                        previous.start,
                        max(previous.end, note.end),
                        max(previous.velocity, note.velocity),
                    )
                    result[-1] = merged
                    previous = merged
                    overlap_repairs += 1
                    continue
                shortened_end = note.start - 0.005
                result[-1] = _TimedNote(
                    previous.pitch,
                    previous.start,
                    shortened_end,
                    previous.velocity,
                )
                overlap_repairs += 1
            result.append(note)
            previous = note
    return (
        sorted(result, key=lambda item: (item.start, item.pitch, item.end)),
        {
            "removed_out_of_range_notes": removed_out_of_range,
            "removed_short_notes": removed_short,
            "normalized_short_notes": normalized_short,
            "merged_duplicate_notes": duplicate_count,
            "same_pitch_overlap_repairs": overlap_repairs,
        },
    )


def _attack_column_window(notes: list[_TimedNote], source_tempo: float) -> float:
    """Tempo-aware chord-jitter window in seconds.

    Onset-interval histograms of model output show chord jitter below ~30 ms
    and real successions from ~45 ms up.  A fixed 60 ms window merges genuine
    thirty-second notes at brisk tempos (a 32nd is ~52 ms at 144 BPM), so the
    window scales with the estimated beat period and stays inside the valley.
    """

    tempo = _estimate_note_tempo(notes, source_tempo)
    return min(0.045, max(0.025, (60.0 / tempo) / 12))


def _align_attack_columns(
    notes: list[_TimedNote],
    window_seconds: float = 0.060,
) -> tuple[list[_TimedNote], dict[str, int | float]]:
    """Collapse transcription onset jitter without flattening real arpeggios.

    The legacy Just Music rearranger grouped neighbouring attacks recursively,
    which could turn a long arpeggio into one chord. Here every column is capped
    relative to its first attack. Notes move as complete gestures, preserving
    their original durations instead of extending releases into extra voices.
    """

    if not notes:
        return [], {
            "attack_window_ms": round(window_seconds * 1000, 3),
            "attack_columns_before": 0,
            "attack_columns_after": 0,
            "aligned_attack_notes": 0,
            "merged_duplicate_attacks": 0,
        }

    ordered = sorted(notes, key=lambda note: (note.start, note.pitch, note.end))
    melodic_attack_starts = _fast_melodic_attack_starts(
        ordered,
        window_seconds,
    )
    before = len({round(note.start, 6) for note in ordered})
    aligned: list[_TimedNote] = []
    shifted = 0
    duplicates = 0
    index = 0
    while index < len(ordered):
        anchor = ordered[index].start
        stop = index + 1
        while (
            stop < len(ordered)
            and ordered[stop].start - anchor <= window_seconds
        ):
            stop += 1

        cluster = ordered[index:stop]
        cluster_starts = {note.start for note in cluster}
        preserve_melody = (
            len(cluster_starts & melodic_attack_starts) >= 2
        )
        by_attack: dict[tuple[float, int], _TimedNote] = {}
        for note in ordered[index:stop]:
            duration = note.end - note.start
            target_start = note.start if preserve_melody else anchor
            if target_start != note.start:
                shifted += 1
            moved = _TimedNote(
                pitch=note.pitch,
                start=target_start,
                end=target_start + duration,
                velocity=note.velocity,
            )
            key = (target_start, note.pitch)
            previous = by_attack.get(key)
            if previous is None:
                by_attack[key] = moved
            else:
                by_attack[key] = _TimedNote(
                    pitch=note.pitch,
                    start=target_start,
                    end=max(previous.end, moved.end),
                    velocity=max(previous.velocity, moved.velocity),
                )
                duplicates += 1
        aligned.extend(by_attack.values())
        index = stop

    aligned = sorted(aligned, key=lambda note: (note.start, note.pitch, note.end))
    return (
        aligned,
        {
            "attack_window_ms": round(window_seconds * 1000, 3),
            "attack_columns_before": before,
            "attack_columns_after": len(
                {round(note.start, 6) for note in aligned}
            ),
            "aligned_attack_notes": shifted,
            "merged_duplicate_attacks": duplicates,
        },
    )


def _fast_melodic_attack_starts(
    notes: list[_TimedNote],
    window_seconds: float,
) -> set[float]:
    """Find attacks that belong to a rapid single-line figure.

    A four-note run supplies musical context that a two-note onset cluster
    cannot: successive close pitches continuing for longer than the chord
    jitter window are a melody, even when two neighbouring detections happen
    to fall inside that window.  Keeping their raw order here prevents the
    later quantizer from treating the manufactured simultaneity as a true
    dyad.  Very compact rolled chords remain eligible for alignment because
    their whole gesture fits inside the jitter window.
    """

    columns: dict[float, set[int]] = {}
    for note in notes:
        columns.setdefault(note.start, set()).add(note.pitch)
    ordered = sorted(columns.items())
    if len(ordered) < 4:
        return set()

    minimum_gap = max(0.008, window_seconds * 0.18)
    rapid_gap = max(0.075, window_seconds * 2.4)
    minimum_span = window_seconds * 1.35
    protected: set[float] = set()
    for index in range(len(ordered) - 3):
        run = ordered[index : index + 4]
        gaps = [
            right[0] - left[0]
            for left, right in zip(run, run[1:], strict=False)
        ]
        if (
            any(gap < minimum_gap or gap > rapid_gap for gap in gaps)
            or run[-1][0] - run[0][0] < minimum_span
            or not _has_close_pitch_path(run)
        ):
            continue
        protected.update(start for start, _ in run)
    return protected


def _has_close_pitch_path(
    columns: list[tuple[float, set[int]]],
) -> bool:
    reachable = set(columns[0][1])
    for _, pitches in columns[1:]:
        reachable = {
            pitch
            for pitch in pitches
            if any(1 <= abs(pitch - previous) <= 5 for previous in reachable)
        }
        if not reachable:
            return False
    return True


def _select_timeline_mapper(
    beat_times: list[float],
    notes: list[_TimedNote],
    source_tempo: float,
    *,
    beat_candidates: list[_BeatCandidate] | None = None,
) -> tuple[Callable[[float], float], float, str, dict[str, object]]:
    """Choose the beat layer whose rhythmic grid best matches note attacks.

    Beat trackers often lock to eighth-note accompaniment instead of the
    notated quarter note, while dense note intervals can produce a false fast
    tempo. We compare those hypotheses against the source MIDI tempo and keep a
    small source prior so a marginal estimate cannot rewrite the whole score.
    """

    source_tempo = max(40.0, min(220.0, source_tempo))
    attack_times = sorted({round(note.start, 6) for note in notes})

    candidates: list[
        tuple[
            str,
            float,
            Callable[[float], float],
            bool,
            tuple[float, float, float, float],
        ]
    ] = []
    constant_tempos: list[tuple[str, float]] = [
        ("constant_tempo_source", source_tempo),
        ("constant_tempo_notes", _estimate_note_tempo(notes, source_tempo)),
    ]
    if len(beat_times) >= 2:
        dynamic_mapper, beat_tempo = _beat_mapper(beat_times, notes)
        candidates.append(
            (
                "librosa_dynamic_beat_warp",
                beat_tempo,
                dynamic_mapper,
                True,
                _beat_continuity_analysis(beat_times),
            )
        )
        constant_tempos.append(("constant_tempo_beats", beat_tempo))
        if beat_tempo / 2 >= 40:
            constant_tempos.append(("constant_tempo_beats_half", beat_tempo / 2))
        if beat_tempo * 2 <= 220:
            constant_tempos.append(("constant_tempo_beats_double", beat_tempo * 2))

    known_methods = {candidate[0] for candidate in candidates}
    for beat_candidate in beat_candidates or []:
        if beat_candidate.method in known_methods or len(beat_candidate.beat_times) < 2:
            continue
        adaptive_mapper, adaptive_tempo = _beat_mapper(
            list(beat_candidate.beat_times),
            notes,
        )
        candidates.append(
            (
                beat_candidate.method,
                adaptive_tempo,
                adaptive_mapper,
                True,
                _beat_continuity_analysis(list(beat_candidate.beat_times)),
            )
        )
        known_methods.add(beat_candidate.method)

    seen_tempos: list[float] = []
    for method, tempo in constant_tempos:
        tempo = max(40.0, min(220.0, tempo))
        if any(abs(tempo - previous) < 0.5 for previous in seen_tempos):
            continue
        seen_tempos.append(tempo)
        candidates.append(
            (
                method,
                tempo,
                lambda seconds, current=tempo: seconds * current / 60,
                False,
                (0.0, 0.0, 0.0, 0.0),
            )
        )

    scored: list[dict[str, object]] = []
    selected_mappers: dict[str, Callable[[float], float]] = {}
    for method, tempo, base_mapper, dynamic, continuity in candidates:
        phase, grid_name, grid_error, hit_rate, rhythm_score = _best_rhythm_phase(
            base_mapper,
            attack_times,
            preserve_origin=method == "constant_tempo_source",
        )
        def mapper(
            seconds: float,
            base: Callable[[float], float] = base_mapper,
            offset: float = phase,
        ) -> float:
            return max(0.0, base(seconds) - offset)

        positions = [mapper(value) for value in attack_times]
        duration_quarters = max(1.0, max(positions) - min(positions))
        columns_per_quarter = len(positions) / duration_quarters
        density_penalty = 0.0
        if columns_per_quarter > 4.5:
            density_penalty += (columns_per_quarter - 4.5) * 0.02
        elif columns_per_quarter < 0.65:
            density_penalty += (0.65 - columns_per_quarter) * 0.02
        # Transcription backends commonly write an arbitrary 120 BPM tempo into
        # their output MIDI.  It is useful as a weak tie-breaker, but must not
        # overrule a clearly better audio-derived beat grid.
        source_prior = abs(log2(tempo / source_tempo)) * 0.006
        continuity_penalty, jump_rate, reversal_rate, severe_jump_rate = continuity
        dynamic_penalty = (0.003 + continuity_penalty) if dynamic else 0.0
        total_score = rhythm_score + density_penalty + source_prior + dynamic_penalty
        selected_mappers[method] = mapper
        scored.append(
            {
                "method": method,
                "tempo_bpm": round(tempo, 3),
                "phase_quarters": round(phase, 5),
                "grid": grid_name,
                "grid_error": round(grid_error, 5),
                "grid_hit_rate": round(hit_rate, 4),
                "attack_columns_per_quarter": round(columns_per_quarter, 4),
                "tempo_jump_rate": round(jump_rate, 4),
                "tempo_reversal_rate": round(reversal_rate, 4),
                "tempo_severe_jump_rate": round(severe_jump_rate, 4),
                "tempo_continuity_penalty": round(continuity_penalty, 6),
                "score": round(total_score, 6),
            }
        )

    best = min(scored, key=lambda item: float(item["score"]))
    source_candidate = next(
        item for item in scored if item["method"] == "constant_tempo_source"
    )
    # Retain the source only for a genuine statistical tie.  Earlier versions
    # treated any merely readable 120 BPM source grid as authoritative; for the
    # Avid video this compressed roughly 810 written quarter notes to 653 and
    # made every notated duration too short.
    if float(source_candidate["score"]) <= float(best["score"]) + 0.003:
        best = source_candidate
        selection_reason = "source_statistical_tie"
    else:
        selection_reason = "best_rhythm_alignment"

    selected_method = str(best["method"])
    selected_tempo = float(best["tempo_bpm"])
    for item in scored:
        item["selected"] = item is best
    return (
        selected_mappers[selected_method],
        selected_tempo,
        selected_method,
        {
            "source_tempo_bpm": round(source_tempo, 3),
            "tempo_selection_reason": selection_reason,
            "tempo_candidates": sorted(scored, key=lambda item: float(item["score"])),
        },
    )


def _beat_continuity_analysis(
    beat_times: list[float],
) -> tuple[float, float, float, float]:
    """Penalize beat-layer switches while preserving smooth tempo curves.

    Genuine accelerando and rubato change neighboring beat periods gradually.
    Audio trackers instead tend to jump between tactus levels (for example
    quarter, dotted-quarter, or half-note pulses) and often reverse that jump
    a beat later.  This compact state-continuity prior mirrors the role of a
    transition model in DBN beat tracking: it never rejects a candidate by
    itself, but a jittery curve must improve the transcription grid by enough
    to pay for its implausible tempo motion.
    """

    periods = [
        right - left
        for left, right in zip(beat_times, beat_times[1:], strict=False)
        if right > left
    ]
    if len(periods) < 3:
        return 0.0, 0.0, 0.0, 0.0

    changes = [
        log2(current / previous)
        for previous, current in zip(periods, periods[1:], strict=False)
    ]
    jump_threshold = log2(1.12)
    reversal_threshold = log2(1.06)
    severe_threshold = log2(1.25)
    jump_rate = sum(abs(change) >= jump_threshold for change in changes) / len(changes)
    severe_jump_rate = (
        sum(abs(change) >= severe_threshold for change in changes) / len(changes)
    )
    reversal_pairs = list(zip(changes, changes[1:], strict=False))
    reversal_rate = (
        sum(
            left * right < 0
            and min(abs(left), abs(right)) >= reversal_threshold
            for left, right in reversal_pairs
        )
        / len(reversal_pairs)
        if reversal_pairs
        else 0.0
    )
    penalty = min(
        0.02,
        0.03 * jump_rate + 0.02 * reversal_rate + 0.04 * severe_jump_rate,
    )
    return penalty, jump_rate, reversal_rate, severe_jump_rate


def _best_rhythm_phase(
    mapper: Callable[[float], float],
    attack_times: list[float],
    *,
    preserve_origin: bool = False,
) -> tuple[float, str, float, float, float]:
    positions = [mapper(value) for value in attack_times]
    phase_candidates = [0.0]
    if not preserve_origin:
        phase_candidates = sorted(
            {
                0.0,
                *(index * 0.25 / 48 for index in range(48)),
                *(index * (1 / 3) / 48 for index in range(48)),
            }
        )
    best: tuple[float, float, str, float, float] | None = None
    for phase in phase_candidates:
        shifted = [position - phase for position in positions]
        sixteenth_errors = [
            abs(position - round(position / 0.25) * 0.25)
            for position in shifted
        ]
        triplet_step = 1 / 3
        triplet_errors = [
            abs(position - round(position / triplet_step) * triplet_step)
            for position in shifted
        ]
        sixteenth_error = median(sixteenth_errors)
        triplet_error = median(triplet_errors)
        sixteenth_hit = sum(error <= 0.055 for error in sixteenth_errors) / len(
            sixteenth_errors
        )
        triplet_hit = sum(error <= 0.055 for error in triplet_errors) / len(
            triplet_errors
        )
        sixteenth_score = sixteenth_error + (1 - sixteenth_hit) * 0.015
        triplet_score = triplet_error + 0.006 + (1 - triplet_hit) * 0.015
        if sixteenth_score <= triplet_score:
            candidate = (
                sixteenth_score,
                phase,
                "sixteenth",
                sixteenth_error,
                sixteenth_hit,
            )
        else:
            candidate = (
                triplet_score,
                phase,
                "eighth_triplet",
                triplet_error,
                triplet_hit,
            )
        if best is None or candidate < best:
            best = candidate

    assert best is not None
    score, phase, grid_name, error, hit_rate = best
    return phase, grid_name, error, hit_rate, score


def _beat_mapper(
    beat_times: list[float],
    notes: list[_TimedNote],
):
    periods = [
        right - left
        for left, right in zip(beat_times, beat_times[1:], strict=False)
        if right > left
    ]
    period = median(periods)
    earliest = min(note.start for note in notes)
    latest = max(note.end for note in notes)
    extended = list(beat_times)
    while extended[0] > earliest:
        extended.insert(0, extended[0] - period)
    while extended[-1] < latest:
        extended.append(extended[-1] + period)
    origin = max(0, bisect.bisect_right(extended, earliest) - 1)

    def mapper(seconds: float) -> float:
        index = bisect.bisect_right(extended, seconds) - 1
        index = max(0, min(index, len(extended) - 2))
        left = extended[index]
        right = extended[index + 1]
        fraction = 0.0 if right <= left else (seconds - left) / (right - left)
        return max(0.0, index - origin + fraction)

    return mapper, 60 / period


def _estimate_meter_and_downbeat(
    notes: list[_TimedNote],
    mapper: Callable[[float], float],
) -> tuple[int, int, float]:
    """Choose the measure length, subdivision family, and downbeat phase.

    Transcription MIDIs would otherwise always be written 4/4, which mis-bars
    waltzes and compound-meter pieces.  Accent evidence (weighted attack mass
    on barlines, low-bass presence, long-note starts) scores 2/3/4-beat
    hypotheses at every phase; 4/4 with zero phase is the incumbent and only
    loses by a clear margin.  A ternary-subdivision test then distinguishes
    compound meters (6/8, 9/8, 12/8) from simple ones — swing feels occupy
    only the 2/3 position and therefore stay binary.
    """

    columns: dict[float, list[_TimedNote]] = {}
    for note in notes:
        key = round(mapper(note.start) * 24) / 24
        columns.setdefault(key, []).append(note)
    if len(columns) < 8:
        return 4, 4, 0.0

    total_weight = 0.0
    scores: dict[tuple[int, float], float] = {}
    opening_weight = 0.0
    # Beat trackers can lock onto a sixteenth off-beat: every onset then sits
    # a sub-beat fraction off the true quarter grid, and integer-beat phase
    # hypotheses can never pull it back.  Search downbeats at sixteenth
    # resolution so a one-sixteenth grid slip still finds the real barline.
    phase_steps = [index / 4 for index in range(16)]
    hypotheses = (
        [(4, phase) for phase in phase_steps]
        + [(3, phase) for phase in phase_steps[:12]]
        + [(2, phase) for phase in phase_steps[:8]]
    )
    for hypothesis in hypotheses:
        scores[hypothesis] = 0.0
    opening_scores = dict.fromkeys(hypotheses, 0.0)
    first_onset = min(columns)
    opening_end = first_onset + 64.0
    for onset, column in columns.items():
        # One column, one vote: taking the loudest attack instead of summing
        # the column keeps a dense off-beat chord from outvoting the bass.
        weight = max(note.velocity for note in column) / 127
        lowest = min(note.pitch for note in column)
        longest = max(mapper(note.end) - mapper(note.start) for note in column)
        total_weight += weight
        in_opening = onset < opening_end
        if in_opening:
            opening_weight += weight
        is_bass = lowest <= 45
        is_long = longest >= 1.5
        for meter, phase in hypotheses:
            position = (onset - phase) % meter
            if min(position, meter - position) < 0.13:
                accent = weight
                if is_bass:
                    accent += 0.5 * weight
                if is_long:
                    accent += 0.5 * weight
                scores[(meter, phase)] += accent
                if in_opening:
                    opening_scores[(meter, phase)] += accent

    reference = max(total_weight, 1.0)
    margin = 0.06 * reference

    def family_winner(meter: int) -> tuple[int, float]:
        return max(
            ((m, phase) for m, phase in scores if m == meter),
            key=lambda hypothesis: scores[hypothesis],
        )

    best4 = family_winner(4)
    best3 = family_winner(3)
    best2 = family_winner(2)

    # In 4/4, beat three often carries the lowest/longest bass note and can
    # narrowly outscore the real downbeat.  That is a half-bar ambiguity, not
    # reliable evidence for a three-beat pickup.  When the two opposite phase
    # candidates are globally close, let the opening phrase decide — but only
    # when it clearly supports the opposite phase and produces a materially
    # shorter pickup.  This preserves genuine strong phase shifts while fixing
    # arrangements whose beat-three accompaniment is heavier than beat one.
    opposite4 = (4, (best4[1] + 2.0) % 4.0)
    resolved_half_bar_ambiguity = False

    def pickup_length(phase: float) -> float:
        length = (phase - first_onset) % 4.0
        return 0.0 if min(length, 4.0 - length) < 0.13 else length

    if (
        scores[best4] - scores[opposite4] <= 0.02 * reference
        and opening_scores[opposite4] - opening_scores[best4]
        >= 0.035 * max(opening_weight, 1.0)
        and pickup_length(opposite4[1]) + 0.5 < pickup_length(best4[1])
    ):
        best4 = opposite4
        resolved_half_bar_ambiguity = True

    # A two-beat hypothesis places a candidate barline twice as often as 4/4
    # and 50% more often than 3/4, so its raw accumulated accent score is not
    # directly comparable with the other meter families.  It is meaningful
    # only together with ternary subdivision evidence, where it represents the
    # two dotted-quarter pulses of 6/8.  Letting that denser sampling veto 3/4
    # made waltzes with a bass note on every beat fall through to the default
    # 4/4 even when the three-beat accent cycle was otherwise unambiguous.
    compound_two_beat = (
        scores[best2] > scores[best4] + margin
        and _ternary_subdivision_dominant(notes, mapper, best2[1])
    )
    if compound_two_beat:
        return 6, 8, float(best2[1])
    if scores[best3] > scores[best4] + margin:
        numerator, phase = 3, best3[1]
    elif resolved_half_bar_ambiguity or scores[best4] > scores[(4, 0)] + margin:
        numerator, phase = 4, best4[1]
    else:
        numerator, phase = 4, 0

    if _ternary_subdivision_dominant(notes, mapper, float(phase)):
        return numerator * 3, 8, float(phase)
    return numerator, 4, float(phase)


def _ternary_subdivision_dominant(
    notes: list[_TimedNote],
    mapper: Callable[[float], float],
    phase: float,
) -> bool:
    """Whether sub-beat onsets live on the ternary grid (both third slots).

    Swing pairs occupy only the 2/3 slot, so requiring evidence on both third
    positions keeps swung simple meter from being rebranded as compound.
    """

    third_hits = 0
    first_slot = 0
    binary_hits = 0
    for note in notes:
        fraction = (mapper(note.start) - phase) % 1.0
        if fraction < 0.06 or fraction > 0.94:
            continue
        third_distance = min(abs(fraction - 1 / 3), abs(fraction - 2 / 3))
        binary_distance = min(
            abs(fraction - 0.25), abs(fraction - 0.5), abs(fraction - 0.75)
        )
        if third_distance <= 0.06 and third_distance < binary_distance:
            third_hits += 1
            if abs(fraction - 1 / 3) < abs(fraction - 2 / 3):
                first_slot += 1
        elif binary_distance <= 0.06:
            binary_hits += 1
    total = third_hits + binary_hits
    if total < 6:
        return False
    return (
        third_hits / total >= 0.6
        and first_slot >= 2
        and third_hits - first_slot >= 2
    )


def _estimate_note_tempo(notes: list[_TimedNote], source_tempo: float) -> float:
    onsets = sorted({round(note.start, 4) for note in notes})
    intervals = [
        right - left
        for left, right in zip(onsets, onsets[1:], strict=False)
        if 0.08 <= right - left <= 1.5
    ]
    if not intervals:
        return max(40.0, min(220.0, source_tempo))
    normalized: list[float] = []
    for interval in intervals:
        while interval < 0.30:
            interval *= 2
        while interval > 0.90:
            interval /= 2
        normalized.append(interval)
    return max(40.0, min(220.0, 60 / median(normalized)))


def _mapper_tempo_events(
    mapper: Callable[[float], float],
    notes: list[_TimedNote],
    tempo_bpm: float,
) -> list[tuple[float, float]]:
    """Turn the chosen timeline's own tempo bends into score tempo events.

    A rubato-aware mapper maps seconds to a curvy beat line; its local slope
    is the performed tempo.  Emitting the sustained bends as tempo events
    lets the score print rit./accel. and plateau metronome marks, so a slow
    opening or a ritardando is written as one instead of flattened away.
    """

    onsets = sorted(note.start for note in notes)
    if len(onsets) < 24:
        return []
    step = max(1, len(onsets) // 60)
    slopes: list[tuple[float, float]] = []
    for index in range(0, len(onsets) - step, step):
        t0, t1 = onsets[index], onsets[index + step]
        b0, b1 = mapper(t0), mapper(t1)
        if t1 > t0:
            slopes.append(((t0 + t1) / 2, (b1 - b0) / (t1 - t0) * 60.0))
    if len(slopes) < 6:
        return []
    base = median(slope for _, slope in slopes)
    if base <= 0:
        return []

    events: list[tuple[float, float]] = []
    index = 0
    count = len(slopes)
    while index < count:
        ratio = slopes[index][1] / base
        if abs(ratio - 1.0) >= 0.18:
            stop = index + 1
            while stop < count and abs(slopes[stop][1] / base - 1.0) >= 0.12:
                stop += 1
            if stop - index >= 3:
                region_bpm = median(slopes[k][1] for k in range(index, stop))
                region_bpm = max(tempo_bpm * 0.3, min(tempo_bpm * 2.2, region_bpm))
                events.append((mapper(slopes[index][0]), region_bpm))
                events.append((mapper(slopes[min(stop, count - 1)][0]), base))
            index = max(stop, index + 1)
        else:
            index += 1
    return events


def _write_aligned_midi(
    notes: list[_TimedNote],
    pedals: list[_TimedPedal],
    mapper,
    tempo_bpm: float,
    backend: str,
    *,
    meter_numerator: int = 4,
    meter_denominator: int = 4,
    tempo_events: list[tuple[float, float]] | None = None,
) -> bytes:
    midi = mido.MidiFile(type=1, ticks_per_beat=CANONICAL_DIVISIONS)
    meta_track = mido.MidiTrack()
    note_track = mido.MidiTrack()
    midi.tracks.extend([meta_track, note_track])
    # A tempo region may replace the initial mark only when it genuinely starts
    # in the opening measure.  tempo_events stores quarter-note positions, not
    # ticks; comparing it with CANONICAL_DIVISIONS used to treat practically
    # every later event as an opening event and printed the wrong initial BPM.
    opening_bpm = tempo_bpm
    if tempo_events:
        first_quarters, first_bpm = tempo_events[0]
        opening_measure_quarters = meter_numerator * 4.0 / meter_denominator
        if first_quarters < opening_measure_quarters:
            opening_bpm = max(20.0, first_bpm)
    meta_track.append(mido.MetaMessage("track_name", name="Tempo and meter", time=0))
    meta_track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(opening_bpm), time=0))
    meta_track.append(
        mido.MetaMessage(
            "time_signature",
            numerator=meter_numerator,
            denominator=meter_denominator,
            clocks_per_click=24 if meter_denominator == 4 else 36,
            notated_32nd_notes_per_beat=8,
            time=0,
        )
    )
    if tempo_events:
        # Performed rit./accel. curve: the notation pipeline turns sustained
        # monotone runs into rit./accel. text marks.  Tick 0 keeps the nominal
        # tempo so the marks always reference the piece's base speed.  The
        # time signature is already written at tick 0 above, so the deltas
        # below only ever move forward.
        previous_tick = 0
        for quarters, bpm in tempo_events:
            tick = max(0, round(quarters * CANONICAL_DIVISIONS))
            if tick <= 0:
                continue
            meta_track.append(
                mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(max(20.0, bpm)), time=tick - previous_tick)
            )
            previous_tick = tick
    note_track.append(
        mido.MetaMessage("track_name", name=f"Piano transcription ({backend})", time=0)
    )
    note_track.append(mido.Message("program_change", program=0, channel=0, time=0))

    events: list[tuple[int, int, mido.Message]] = []
    for note in notes:
        start_tick = max(0, round(mapper(note.start) * CANONICAL_DIVISIONS))
        end_tick = max(start_tick + 15, round(mapper(note.end) * CANONICAL_DIVISIONS))
        events.append(
            (
                start_tick,
                2,
                mido.Message(
                    "note_on",
                    note=note.pitch,
                    velocity=max(1, min(127, note.velocity)),
                    channel=0,
                    time=0,
                ),
            )
        )
        events.append(
            (
                end_tick,
                0,
                mido.Message("note_off", note=note.pitch, velocity=0, channel=0, time=0),
            )
        )
    previous_pedal: bool | None = None
    for pedal in pedals:
        if pedal.down == previous_pedal:
            continue
        events.append(
            (
                max(0, round(mapper(pedal.time) * CANONICAL_DIVISIONS)),
                1,
                mido.Message(
                    "control_change",
                    control=64,
                    value=127 if pedal.down else 0,
                    channel=0,
                    time=0,
                ),
            )
        )
        previous_pedal = pedal.down

    previous_tick = 0
    for tick, _, message in sorted(events, key=lambda item: (item[0], item[1])):
        message.time = tick - previous_tick
        note_track.append(message)
        previous_tick = tick
    note_track.append(mido.MetaMessage("end_of_track", time=0))

    output = BytesIO()
    midi.save(file=output)
    return output.getvalue()
