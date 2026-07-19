// ---------------------------------------------------------------------------
// format.js — small display helpers (dates, durations, relative time)
// ---------------------------------------------------------------------------

const EM_DASH = "—";

// Relative time from an ISO timestamp, e.g. "6h ago". Returns em dash if null.
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
export function shortDate(iso) {
  if (!iso) return EM_DASH;
  const d = new Date(iso);
  if (isNaN(d)) return EM_DASH;
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
