import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  classifyInputFilename,
  defaultScoreTitle,
  MAX_MEDIA_BYTES,
  MAX_MIDI_BYTES,
  uploadLimitBytes,
} from "./files";

describe("input file helpers", () => {
  it("classifies MIDI, audio, and video extensions case-insensitively", () => {
    assert.equal(classifyInputFilename("piece.MIDI"), "midi");
    assert.equal(classifyInputFilename("piano.flac"), "media");
    assert.equal(classifyInputFilename("performance.MP4"), "media");
    assert.equal(classifyInputFilename("notes.pdf"), null);
  });

  it("uses separate safe upload limits", () => {
    assert.equal(uploadLimitBytes("midi"), MAX_MIDI_BYTES);
    assert.equal(uploadLimitBytes("media"), MAX_MEDIA_BYTES);
    assert.ok(MAX_MEDIA_BYTES > MAX_MIDI_BYTES);
  });

  it("derives a readable title from the final extension", () => {
    assert.equal(defaultScoreTitle("golden.hour.mp3"), "golden.hour");
  });
});
