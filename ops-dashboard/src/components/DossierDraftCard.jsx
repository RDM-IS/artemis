import { C, FONT_MONO, FONT_BODY } from "../theme";
import Badge from "./Badge";

// ---------------------------------------------------------------------------
// DossierDraftCard — a <DRAFT> in the approval queue. v1: approve/reject only,
// NO edit. Shows provenance / label. Evidence may be absent for some draft
// types — the UI notes that rather than implying it was suppressed.
// ---------------------------------------------------------------------------

export default function DossierDraftCard({ draft, busy, onApprove, onReject }) {
  return (
    <div
      style={{
        background: C.VOID,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        padding: "12px 14px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
        opacity: busy ? 0.55 : 1,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <Badge bg={C.EMBER}>{draft.draft_type}</Badge>
        {draft.dossier_name && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}>
            {draft.dossier_name}
          </span>
        )}
        {draft.label && (
          <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.ORACLE }}>
            {draft.label}
          </span>
        )}
      </div>

      <div style={{ fontFamily: FONT_BODY, fontSize: 15, color: C.ARROW }}>
        {draft.text || <span style={{ color: C.MOONSTONE }}>(no text)</span>}
      </div>

      {draft.evidence ? (
        <div
          style={{
            fontFamily: FONT_BODY,
            fontStyle: "italic",
            fontSize: 13,
            color: C.MOONSTONE,
            opacity: 0.85,
          }}
        >
          “{draft.evidence}”
        </div>
      ) : (
        <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE, opacity: 0.6 }}>
          no evidence for this draft type
        </div>
      )}

      {draft.provenance && (
        <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE, opacity: 0.75 }}>
          {draft.provenance}
        </div>
      )}

      <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 2 }}>
        <button
          onClick={() => onApprove(draft.draft_type, draft.id)}
          disabled={busy}
          style={btn(C.AVOCADO, C.AVOCADO)}
        >
          Approve
        </button>
        <button
          onClick={() => onReject(draft.draft_type, draft.id)}
          disabled={busy}
          style={btn(C.MIST, C.SIGNAL)}
        >
          Reject
        </button>
      </div>
    </div>
  );
}

function btn(border, color) {
  return {
    fontFamily: FONT_MONO,
    fontSize: 11,
    letterSpacing: 1,
    textTransform: "uppercase",
    padding: "7px 14px",
    borderRadius: 4,
    border: `1px solid ${border}`,
    background: "transparent",
    color,
    cursor: "pointer",
    minHeight: 36,
  };
}
