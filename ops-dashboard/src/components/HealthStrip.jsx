import { C, FONT_MONO } from "../theme";
import { relativeTime, shortDate, humanDuration, orDash } from "../format";

// ---------------------------------------------------------------------------
// HealthStrip — global ACOS health line rendered from <HEALTH>.
//
// Renders HONESTLY: any null field shows "—", never invented. Version shown as
// given; jobs as count; uptime as human duration; last_brief as relative +
// absolute.
// ---------------------------------------------------------------------------

export default function HealthStrip({ health }) {
  if (!health) return null;

  const online = health.status === "online";
  const jobs =
    health.scheduler_jobs == null ? "—" : `${health.scheduler_jobs}`;
  const uptime = humanDuration(health.uptime_seconds);
  const brief =
    health.last_brief == null
      ? "—"
      : `${relativeTime(health.last_brief)} (${shortDate(health.last_brief)})`;

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 14,
        flexWrap: "wrap",
        fontFamily: FONT_MONO,
        fontSize: 11,
        color: C.MOONSTONE,
        background: C.SHADOW,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        padding: "8px 12px",
      }}
    >
      <span style={{ display: "flex", alignItems: "center", gap: 7 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: online ? C.GREEN : C.SIGNAL,
            boxShadow: `0 0 6px ${online ? C.GREEN : C.SIGNAL}`,
            display: "inline-block",
          }}
        />
        <span
          style={{
            color: online ? C.GREEN : C.SIGNAL,
            textTransform: "uppercase",
            letterSpacing: 1.5,
          }}
        >
          ACOS {orDash(health.status)}
        </span>
      </span>
      <Field label="ver" value={orDash(health.version)} />
      <Field label="jobs" value={jobs} />
      <Field label="uptime" value={uptime} />
      <Field label="last brief" value={brief} />
    </div>
  );
}

function Field({ label, value }) {
  return (
    <span style={{ whiteSpace: "nowrap" }}>
      <span style={{ opacity: 0.7 }}>{label} </span>
      <span style={{ color: C.ARROW }}>{value}</span>
    </span>
  );
}
