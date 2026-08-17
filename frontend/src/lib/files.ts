export type InputKind = "midi" | "media" | "score";

export const MAX_MIDI_BYTES = 10 * 1024 * 1024;
export const MAX_MEDIA_BYTES = 250 * 1024 * 1024;

const MIDI_EXTENSIONS = new Set(["mid", "midi"]);
const SCORE_EXTENSIONS = new Set(["musicxml", "xml", "mxl"]);
const MEDIA_EXTENSIONS = new Set([
  "aac",
  "flac",
  "m4a",
  "mkv",
  "mov",
  "mp3",
  "mp4",
  "ogg",
  "opus",
  "wav",
  "webm",
]);

export const FILE_INPUT_ACCEPT = [
  ".mid",
  ".midi",
  ...[...SCORE_EXTENSIONS].map((extension) => `.${extension}`),
  ...[...MEDIA_EXTENSIONS].map((extension) => `.${extension}`),
].join(",");

export function classifyInputFilename(filename: string): InputKind | null {
  const extension = filename.toLowerCase().split(".").pop() ?? "";
  if (MIDI_EXTENSIONS.has(extension)) return "midi";
  if (SCORE_EXTENSIONS.has(extension)) return "score";
  if (MEDIA_EXTENSIONS.has(extension)) return "media";
  return null;
}

export function uploadLimitBytes(kind: InputKind): number {
  return kind === "media" ? MAX_MEDIA_BYTES : MAX_MIDI_BYTES;
}

export function defaultScoreTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}
