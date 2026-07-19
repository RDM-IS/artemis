import { C, FONT_MONO, FONT_BODY } from "../theme";
import { describeError, isAuthError } from "../api";

// ---------------------------------------------------------------------------
// Shared loading / error / empty states. Never a blank screen.
// ---------------------------------------------------------------------------

export function Loading({ label = "Loading…" }) {
  return (
    <div
      style={{
        fontFamily: FONT_MONO,
        color: C.MOONSTONE,
        padding: "24px 4px",
        fontSize: 13,
      }}
    >
      {label}
    </div>
  );
}

// ErrorStrip — D2's visible failure surface. A burnt-orange (#D86E2C) strip
// showing "endpoint — status" (e.g. "portfolio — 403", "approve — non-JSON
// response"). Used for read failures at page/panel level and mutation failures
// inline near the card. Empty-state copy is NEVER shown for a failure — only for
// a genuinely-empty successful response. Auth (401/403) failures get a Reload
// affordance (re-triggers the Cloudflare Access sign-in); others get Retry.
export function ErrorStrip({ error, onRetry, compact }) {
  if (!error) return null;
  const auth = isAuthError(error);
  return (
    <div
      style={{
        background: C.SHADOW,
        border: `1px solid ${C.BURNT_ORANGE}`,
        borderLeft: `4px solid ${C.BURNT_ORANGE}`,
        borderRadius: 4,
        padding: compact ? "8px 12px" : "12px 16px",
        display: "flex",
        alignItems: "center",
        gap: 12,
        flexWrap: "wrap",
      }}
    >
      <span
        style={{
          fontFamily: FONT_MONO,
          fontSize: compact ? 11 : 12,
          color: C.BURNT_ORANGE,
          letterSpacing: 0.5,
        }}
      >
        {describeError(error)}
      </span>
      {auth ? (
        <>
          <span style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.MOONSTONE }}>
            Access not passed — reload to sign in.
          </span>
          <button
            onClick={() => window.location.reload()}
            style={btnStyle(C.BURNT_ORANGE)}
          >
            Reload
          </button>
        </>
      ) : (
        onRetry && (
          <button onClick={onRetry} style={btnStyle(C.BURNT_ORANGE)}>
            Retry
          </button>
        )
      )}
    </div>
  );
}

export function EmptyLine({ children }) {
  return (
    <div
      style={{
        fontFamily: FONT_BODY,
        fontStyle: "italic",
        color: C.MOONSTONE,
        fontSize: 13,
        opacity: 0.8,
        padding: "4px 0",
      }}
    >
      {children}
    </div>
  );
}

function btnStyle(bg) {
  return {
    fontFamily: FONT_MONO,
    fontSize: 12,
    letterSpacing: 1,
    textTransform: "uppercase",
    padding: "7px 14px",
    borderRadius: 4,
    border: `1px solid ${bg}`,
    background: "transparent",
    color: C.ARROW,
    cursor: "pointer",
  };
}

export { btnStyle };
