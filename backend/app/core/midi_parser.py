from __future__ import annotations

from collections import defaultdict
from io import BytesIO

import mido

from .models import (
    KeySignatureEvent,
    ParsedMidi,
    PedalEvent,
    RawNote,
    TempoEvent,
    TimeSignatureEvent,
)


class MidiParseError(ValueError):
    """Raised when a MIDI file cannot be interpreted safely."""


PIANO_PROGRAMS = frozenset(range(8))


def parse_midi(data: bytes) -> ParsedMidi:
    if not data:
        raise MidiParseError("MIDI 文件为空")

    try:
        midi = mido.MidiFile(file=BytesIO(data))
    except (OSError, EOFError, ValueError) as exc:
        raise MidiParseError(f"无法读取 MIDI 文件：{exc}") from exc

    if midi.type == 2:
        raise MidiParseError("暂不支持异步的 MIDI Type 2 文件")
    if midi.ticks_per_beat <= 0:
        raise MidiParseError("MIDI 的 ticks_per_beat 无效")

    notes: list[RawNote] = []
    tempos: list[TempoEvent] = []
    time_signatures: list[TimeSignatureEvent] = []
    key_signatures: list[KeySignatureEvent] = []
    pedals: list[PedalEvent] = []
    track_names: dict[int, str] = {}
    warnings: list[str] = []
    source_id = 0
    piano_note_on_count = 0
    excluded_non_piano_note_count = 0
    excluded_percussion_note_count = 0
    programs: set[int] = set()

    for track_index, track in enumerate(midi.tracks):
        absolute_tick = 0
        active: dict[tuple[int, int], list[tuple[int, int, int]]] = defaultdict(list)
        current_program: dict[int, int] = defaultdict(int)

        for message in track:
            absolute_tick += int(message.time)

            if message.is_meta:
                if message.type == "track_name" and message.name.strip():
                    track_names[track_index] = message.name.strip()
                elif message.type == "set_tempo":
                    tempos.append(TempoEvent(absolute_tick, int(message.tempo)))
                elif message.type == "time_signature":
                    time_signatures.append(
                        TimeSignatureEvent(
                            absolute_tick,
                            int(message.numerator),
                            int(message.denominator),
                        )
                    )
                elif message.type == "key_signature":
                    key_signatures.append(KeySignatureEvent(absolute_tick, str(message.key)))
                continue

            channel = int(getattr(message, "channel", 0))
            if message.type == "program_change":
                program = int(message.program)
                current_program[channel] = program
                programs.add(program)
                continue
            if message.type == "control_change" and message.control == 64:
                if channel != 9 and current_program[channel] in PIANO_PROGRAMS:
                    pedals.append(PedalEvent(absolute_tick, channel, message.value >= 64))
                continue

            is_note_on = message.type == "note_on" and message.velocity > 0
            is_note_off = message.type == "note_off" or (
                message.type == "note_on" and message.velocity == 0
            )
            if not (is_note_on or is_note_off):
                continue

            key = (channel, int(message.note))
            if is_note_on:
                if channel == 9:
                    excluded_percussion_note_count += 1
                    continue
                program = current_program[channel]
                programs.add(program)
                if program not in PIANO_PROGRAMS:
                    excluded_non_piano_note_count += 1
                    continue
                piano_note_on_count += 1
                # A physical piano key cannot sustain two independent strikes. Close an
                # unmatched previous strike at the retrigger point instead of creating an
                # impossible overlap.
                if active[key]:
                    start_tick, velocity, previous_id = active[key].pop(0)
                    if absolute_tick > start_tick:
                        notes.append(
                            RawNote(
                                previous_id,
                                key[1],
                                start_tick,
                                absolute_tick,
                                velocity,
                                track_index,
                                channel,
                            )
                        )
                    warnings.append(
                        f"轨道 {track_index + 1} 的音高 {key[1]} 出现未闭合重触，"
                        "已按重触点结束前一音"
                    )
                active[key].append((absolute_tick, int(message.velocity), source_id))
                source_id += 1
            elif active[key]:
                start_tick, velocity, note_id = active[key].pop(0)
                end_tick = max(absolute_tick, start_tick + 1)
                notes.append(
                    RawNote(
                        note_id,
                        key[1],
                        start_tick,
                        end_tick,
                        velocity,
                        track_index,
                        channel,
                    )
                )

        for (channel, pitch), dangling in active.items():
            for start_tick, velocity, note_id in dangling:
                end_tick = max(absolute_tick, start_tick + midi.ticks_per_beat)
                notes.append(
                    RawNote(
                        note_id,
                        pitch,
                        start_tick,
                        end_tick,
                        velocity,
                        track_index,
                        channel,
                    )
                )
                warnings.append(
                    f"轨道 {track_index + 1} 的音高 {pitch} 缺少 Note Off，已按轨道末尾闭合"
                )

    notes.sort(key=lambda note: (note.start_tick, note.pitch, note.source_id))
    if not notes:
        if excluded_non_piano_note_count or excluded_percussion_note_count:
            raise MidiParseError(
                "MIDI 中未检测到钢琴声部；当前版本不会把乐队或打击乐自动压缩成钢琴改编"
            )
        raise MidiParseError("MIDI 中没有可转换的音符")

    excluded_ensemble_notes = (
        excluded_non_piano_note_count + excluded_percussion_note_count
    )
    if (
        excluded_ensemble_notes >= 64
        and excluded_ensemble_notes > piano_note_on_count * 0.5
    ):
        raise MidiParseError(
            "MIDI 主要是多乐器/打击乐总谱，仅抽取其中钢琴轨会得到不完整作品；"
            "请提供独奏钢琴 MIDI（当前版本不自动把乐队总谱缩编成钢琴谱）"
        )

    if excluded_non_piano_note_count:
        warnings.append(
            f"检测到 {excluded_non_piano_note_count} 个非钢琴声部音符，"
            "已仅转换 General MIDI 钢琴家族（程序 1-8）"
        )
    if excluded_percussion_note_count:
        warnings.append(
            f"检测到 {excluded_percussion_note_count} 个打击乐音符，已从钢琴谱中排除"
        )

    tempos = _deduplicate_by_tick(tempos)
    time_signatures = _deduplicate_by_tick(time_signatures)
    key_signatures = _deduplicate_by_tick(key_signatures)
    pedals.sort(key=lambda event: (event.tick, event.channel, not event.down))

    if not tempos or tempos[0].tick > 0:
        tempos.insert(0, TempoEvent(0, 500_000))
    if not time_signatures or time_signatures[0].tick > 0:
        time_signatures.insert(0, TimeSignatureEvent(0, 4, 4))

    return ParsedMidi(
        ticks_per_beat=midi.ticks_per_beat,
        notes=notes,
        tempos=tempos,
        time_signatures=time_signatures,
        key_signatures=key_signatures,
        pedals=pedals,
        track_names=track_names,
        warnings=warnings,
        piano_note_on_count=piano_note_on_count,
        excluded_non_piano_note_count=excluded_non_piano_note_count,
        excluded_percussion_note_count=excluded_percussion_note_count,
        programs=sorted(programs),
    )


def _deduplicate_by_tick(events):
    by_tick = {event.tick: event for event in events}
    return [by_tick[tick] for tick in sorted(by_tick)]
