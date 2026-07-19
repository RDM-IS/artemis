import { C, FONT_MONO, FONT_BODY } from "../theme";

// ---------------------------------------------------------------------------
// CommitmentRow — one <COMMITMENT>. Renders (#id), title, due_label
// (relative+absolute, from the server), a Close button → close endpoint.
// Overdue due_labels are burnt-orange (attention).
// ---------------------------------------------------------------------------

export default function CommitmentRow({ commitment, busy, onClose }) {
  const overdue =
    commitment.due_label && /overdue/i.test(commitment.due_label);

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "10px 12px",
        background: C.VOID,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        opacity: busy ? 0.55 : 1,
        flexWrap: "wrap",
      }}
    >
      <div style={{ flex: 1, minWidth: 180, display: "flex", flexDirection: "column", gap: 4 }}>
        <div style={{ fontFamily: FONT_BODY, fontSize: 15, color: C.ARROW }}>
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}>
            #{commitment.id}{" "}
          </span>
          {commitment.title}
        </div>
        <div
          style={{
            display: "flex",
            gap: 12,
            flexWrap: "wrap",
            fontFamily: FONT_MONO,
            fontSize: 11,
            color: C.MOONSTONE,
          }}
        >
          {commitment.due_label && (
            <span style={{ color: overdue ? C.BURNT_ORANGE : C.ORACLE }}>
              {commitment.due_label}
            </span>
          )}
          {commitment.client && <span>{commitment.client}</span>}
          {commitment.status && <span style={{ opacity: 0.8 }}>{commitment.status}</span>}
        </div>
        {commitment.context && (
          <div style={{ fontFamily: FONT_BODY, fontSize: 12, color: C.MOONSTONE, opacity: 0.8 }}>
            {commitment.context}
          </div>
        )}
      </div>

      <button
        onClick={() => onClose(commitment.id)}
        disabled={busy}
        style={{
          fontFamily: FONT_MONO,
          fontSize: 11,
          letterSpacing: 1,
          textTransform: "uppercase",
          padding: "7px 14px",
          borderRadius: 4,
          border: `1px solid ${C.GREEN}`,
          background: "transparent",
          color: C.GREEN,
          cursor: busy ? "not-allowed" : "pointer",
          minHeight: 36,
        }}
      >
        Close
      </button>
    </div>
  );
}
