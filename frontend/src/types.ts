export type ScoreStyle = "clean" | "balanced" | "faithful";
export type EngravingStyle = "classic" | "modern" | "compact";
export type MinimumNote = "auto" | "eighth" | "sixteenth" | "thirty_second";
export type TranscriptionBackend = "auto" | "transkun" | "basic_pitch";
export type TranscriptionDevice = "auto" | "cpu" | "cuda";

export interface ConversionOptions {
  style: ScoreStyle;
  engraving_style: EngravingStyle;
  minimum_note: MinimumNote;
  allow_triplets: boolean;
  hand_split: "auto" | number;
  prefer_track_hints: boolean;
  max_voices_per_staff: 1 | 2;
  include_pedal: boolean;
  infer_key: boolean;
  time_numerator: number | null;
  time_denominator: 2 | 4 | 8 | 16 | null;
  title: string | null;
}

export interface TranscriptionOptions {
  backend: TranscriptionBackend;
  device: TranscriptionDevice;
  align_beats: boolean;
  minimum_note_ms: number;
  onset_threshold: number;
  frame_threshold: number;
}

export interface ConversionResponse {
  filename: string;
  musicxml: string;
  midi_filename: string | null;
  midi_base64: string | null;
  pdf_filename: string | null;
  pdf_base64: string | null;
  preview_png_base64: string | null;
  analysis: {
    title: string;
    note_count: number;
    measure_count: number;
    duration_quarters: number;
    meter: string;
    tempo_bpm: number;
    key: {
      tonic_pitch_class: number;
      mode: "major" | "minor";
      fifths: number;
      confidence: number;
    };
    hands: {
      right: number;
      left: number;
      method: string;
      split_pitch?: number | string;
    };
    staves: {
      treble: number;
      bass: number;
      method: string;
      cross_staff_hand_notes: number;
      ledger_pressure_notes: number;
    };
    voices: Record<string, number>;
    quality: {
      status: "excellent" | "playable_but_demanding" | "needs_review";
      note_count_preserved: boolean;
      voice_overlap_count: number;
      extreme_staff_misplacements: Record<string, number>;
    };
    engraving?: {
      available: boolean;
      engine: string;
      style?: EngravingStyle;
      page_count?: number;
      page_size?: string;
      a4?: boolean;
      measures_per_system?: number[];
      singleton_systems?: number;
      systems?: Array<{ page: number; y: number; measures: number[] }>;
      processing_ms?: number;
    };
    quantization_grids: Record<string, number>;
    complexity_score: number;
    processing_ms: number;
    source: Record<string, unknown>;
    transcription?: {
      backend: string;
      device: string;
      duration_seconds: number;
      beat_alignment: boolean;
      beat_count: number;
      estimated_tempo_bpm: number;
      raw_note_count: number;
      clean_note_count: number;
      processing_ms: number;
      [key: string]: unknown;
    };
  };
  warnings: string[];
}
