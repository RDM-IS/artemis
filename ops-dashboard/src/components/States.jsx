import { C, FONT_MONO, FONT_BODY } from "../theme";

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

// ErrorState renders a readable message + retry. If the error is a 401/403
// (Cloudflare Access not passed), it shows the auth-specific guidance instead.
export function ErrorState({ error, onRetry }) {
  const status = error && error.status;
  const isAuth = status === 401 || status === 403;

  return (
    <div
      style={{
        background: C.SHADOW,
        border: `1px solid ${C.SIGNAL}`,
        borderRadius: 4,
        padding: "18px 20px",
        display: "flex",
        flexDirection: "column",
        gap: 10,
        maxWidth: 640,
      }}
    >
      <div
        style={{
          fontFamily: FONT_MONO,
          fontSize: 11,
          letterSpacing: 2,
          textTransform: "uppercase",
          color: C.SIGNAL,
        }}
      >
        {isAuth ? "Not authenticated" : "Error"}
      </div>
      <div style={{ fontFamily: FONT_BODY, fontSize: 14, color: C.ARROW }}>
        {isAuth
          ? "Not authenticated — reload to sign in via Cloudflare Access."
          : (error && error.message) || "Something went wrong."}
      </div>
      <div style={{ display: "flex", gap: 10 }}>
        {isAuth ? (
          <button
            onClick={() => window.location.reload()}
            style={btnStyle(C.SIGNAL)}
          >
            Reload
          </button>
        ) : (
          onRetry && (
            <button onClick={onRetry} style={btnStyle(C.MIST)}>
              Retry
            </button>
          )
        )}
      </div>
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
