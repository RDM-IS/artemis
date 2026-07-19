// ---------------------------------------------------------------------------
// format.js — small display helpers (dates, durations, relative time)
// ---------------------------------------------------------------------------

const EM_DASH = "—";

// Matches a bare calendar date with no time component: "2026-08-08".
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

// parseLocalDate — turn a bare `YYYY-MM-DD` string from the API into a Date at
// LOCAL midnight.
//
// D1 (off-by-one): `new Date("2026-08-08")` parses as UTC midnight, so in any
// negative-UTC-offset zone (America/Chicago) it renders/diffs a day early
// ("Aug 7"). Splitting the components and constructing `new Date(y, m-1, d)`
// pins the date to the local day. Use this EVERYWHERE a bare date string from
// the API is formatted or diffed.
//
// Returns null for anything that is not a bare date string (empty, null, a full
// ISO timestamp) — timestamps carry their own time/offset and must go through
// `new Date()` unchanged.
export function parseLocalDate(ymd) {
  if (typeof ymd !== "string") return null;
  const m = DATE_ONLY.exec(ymd.trim());
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return isNaN(d.getTime()) ? null : d;
}

// Relative time from an ISO timestamp, e.g. "6h ago". Returns em dash if null.
// Only ever called with full timestamps (last_capture, last_brief, created_at),
// which carry their own time/offset — bare dates go through parseLocalDate.
export function relativeTime(iso) {
  if (!iso) return EM_DASH;
  const d = new Date(iso);
  if (isNaN(d)) return EM_DASH;
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 0) return "in the future";
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

// Absolute short date, e.g. "Jul 19". Returns em dash if null.
//
// Accepts both bare date strings (due dates, entry dates, hard dates) and full
// ISO timestamps. Bare dates go through parseLocalDate so they don't shift a day
// early (D1); timestamps fall through to `new Date()`, which honours their own
// time/offset.
export function shortDate(value) {
  if (!value) return EM_DASH;
  const d = parseLocalDate(value) || new Date(value);
  if (isNaN(d.getTime())) return EM_DASH;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

// Human duration from seconds, e.g. "3d 4h", "5m". Returns em dash if null.
export function humanDuration(seconds) {
  if (seconds == null || isNaN(seconds)) return EM_DASH;
  const s = Math.floor(seconds);
  const days = Math.floor(s / 86400);
  const hrs = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  if (days > 0) return `${days}d ${hrs}h`;
  if (hrs > 0) return `${hrs}h ${mins}m`;
  if (mins > 0) return `${mins}m`;
  return `${s}s`;
}

// Value or em dash for null/undefined/empty.
export function orDash(v) {
  if (v == null || v === "") return EM_DASH;
  return v;
}

export { EM_DASH };
