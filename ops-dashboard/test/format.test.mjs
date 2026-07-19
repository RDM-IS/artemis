// ---------------------------------------------------------------------------
// D1 regression — bare date strings must render on their LOCAL calendar day, not
// a day early. Run with a negative-UTC-offset zone so the off-by-one can
// actually manifest: the `test` npm script sets TZ=America/Chicago.
//
//   node --test  (or: npm test, which pins TZ)
// ---------------------------------------------------------------------------

import test from "node:test";
import assert from "node:assert/strict";
import { parseLocalDate, shortDate } from "../src/format.js";

test("parseLocalDate builds a LOCAL date, no UTC shift", () => {
  const d = parseLocalDate("2026-08-08");
  assert.equal(d.getFullYear(), 2026);
  assert.equal(d.getMonth(), 7); // August, 0-based
  assert.equal(d.getDate(), 8);
});

test("parseLocalDate rejects non-bare-date inputs", () => {
  assert.equal(parseLocalDate("2026-08-08T00:00:00Z"), null); // timestamp
  assert.equal(parseLocalDate("2026-8-8"), null); // not zero-padded
  assert.equal(parseLocalDate("not-a-date"), null);
  assert.equal(parseLocalDate(""), null);
  assert.equal(parseLocalDate(null), null);
  assert.equal(parseLocalDate(undefined), null);
});

test("shortDate renders bare 2026-08-08 as Aug 8 in a negative-offset zone", () => {
  // getTimezoneOffset() > 0 means the runtime zone is behind UTC (the negative
  // offset that triggers the bug). Only assert the contrast when we're in one.
  const negativeOffset = new Date(2026, 7, 8).getTimezoneOffset() > 0;
  if (negativeOffset) {
    // The old code did `new Date("2026-08-08")` → UTC midnight → renders Aug 7.
    const buggy = new Date("2026-08-08").toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
    });
    assert.equal(buggy, "Aug 7", "expected the regression to manifest in this zone");
  }
  // The fix renders the intended day regardless of zone.
  assert.equal(shortDate("2026-08-08"), "Aug 8");
});

test("shortDate still formats full ISO timestamps (unchanged path)", () => {
  assert.notEqual(shortDate("2026-08-08T12:00:00Z"), "—");
});

test("shortDate returns em dash for empty / invalid", () => {
  assert.equal(shortDate(null), "—");
  assert.equal(shortDate(""), "—");
  assert.equal(shortDate("garbage"), "—");
});
