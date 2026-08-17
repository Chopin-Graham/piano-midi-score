import type {
  ConversionOptions,
  ConversionResponse,
  TranscriptionOptions,
} from "./types";

export async function convertMidi(
  file: File,
  options: ConversionOptions,
): Promise<ConversionResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("options_json", JSON.stringify(options));

  return requestConversion("/api/convert", form);
}

export async function convertMedia(
  file: File,
  options: ConversionOptions,
  transcriptionOptions: TranscriptionOptions,
): Promise<ConversionResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("options_json", JSON.stringify(options));
  form.append("transcription_options_json", JSON.stringify(transcriptionOptions));

  return requestConversion("/api/convert-media", form);
}

async function requestConversion(url: string, form: FormData): Promise<ConversionResponse> {
  const response = await fetch(url, { method: "POST", body: form });

  if (!response.ok) {
    let detail = `转换失败（HTTP ${response.status}）`;
    try {
      const payload = (await response.json()) as { detail?: unknown };
      if (typeof payload.detail === "string") {
        detail = payload.detail;
      } else if (payload.detail) {
        detail = JSON.stringify(payload.detail);
      }
    } catch {
      // Keep the status-based message when the server did not return JSON.
    }
    throw new Error(detail);
  }

  return (await response.json()) as ConversionResponse;
}

export async function convertDemo(options: ConversionOptions): Promise<ConversionResponse> {
  const response = await fetch("/api/demo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options),
  });
  if (!response.ok) {
    throw new Error(`示例转换失败（HTTP ${response.status}）`);
  }
  return (await response.json()) as ConversionResponse;
}
