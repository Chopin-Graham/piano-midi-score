export type InputKind = "midi" | "media" | "score" | "pdf";

export const MAX_MIDI_BYTES = 10 * 1024 * 1024;
export const MAX_MEDIA_BYTES = 250 * 1024 * 1024;
export const MAX_PDF_BYTES = 50 * 1024 * 1024;

const MIDI_EXTENSIONS = new Set(["mid", "midi"]);
const SCORE_EXTENSIONS = new Set(["musicxml", "xml", "mxl"]);
const SCORE_PDF_EXTENSIONS = new Set(["pdf"]);
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
  ...[...SCORE_PDF_EXTENSIONS].map((extension) => `.${extension}`),
  ...[...MEDIA_EXTENSIONS].map((extension) => `.${extension}`),
].join(",");

export function classifyInputFilename(filename: string): InputKind | null {
  const extension = filename.toLowerCase().split(".").pop() ?? "";
  if (MIDI_EXTENSIONS.has(extension)) return "midi";
  if (SCORE_EXTENSIONS.has(extension)) return "score";
  if (SCORE_PDF_EXTENSIONS.has(extension)) return "pdf";
  if (MEDIA_EXTENSIONS.has(extension)) return "media";
  return null;
}

export function uploadLimitBytes(kind: InputKind): number {
  if (kind === "media") return MAX_MEDIA_BYTES;
  if (kind === "pdf") return MAX_PDF_BYTES;
  return MAX_MIDI_BYTES;
}

export function defaultScoreTitle(filename: string): string {
  return filename.replace(/\.[^.]+$/, "");
}

export function defaultScoreMetadata(filename: string): {
  title: string;
  outputFilename: string;
} {
  const stem = defaultScoreTitle(filename);
  return { title: stem, outputFilename: stem };
}

export function normalizeOutputFilename(
  value: string | null | undefined,
  fallback = "score",
): string {
  const finalSegment = (value || fallback).split(/[\\/]/).pop()?.trim() || fallback;
  const withoutKnownExtension = finalSegment.replace(
    /\.(?:mid|midi|musicxml|xml|mxl|pdf|wav|mp3|flac|m4a|aac|ogg|opus|mp4|mkv|mov|webm)$/i,
    "",
  );
  const safe = withoutKnownExtension
    .replace(/[<>:"/\\|?*\u0000-\u001f]+/g, "_")
    .replace(/\s+/g, " ")
    .replace(/^[ .]+|[ .]+$/g, "")
    .slice(0, 100)
    .replace(/[ .]+$/g, "");
  return safe || fallback;
}
