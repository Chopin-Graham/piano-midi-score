import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { formatBytes, formatKey } from "./format";

describe("format helpers", () => {
  it("formats byte sizes", () => {
    assert.equal(formatBytes(500), "500 B");
    assert.equal(formatBytes(1536), "1.5 KB");
    assert.equal(formatBytes(2 * 1024 * 1024), "2.0 MB");
  });

  it("formats an estimated key", () => {
    assert.equal(formatKey(0, "major"), "C 大调");
    assert.equal(formatKey(9, "minor"), "A 小调");
  });
});
