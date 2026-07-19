import { useState } from "react";
import { C, FONT_MONO, FONT_BODY } from "../theme";
import Badge from "./Badge";

// ---------------------------------------------------------------------------
// ProposalCard — one item in the approval queue.
//
// Shows: type_label badge, text, due_label, evidence (italic muted), provenance
// (note filename + source). Actions: Approve (avocado), Reject, Edit (inline
// editors for text / due_date / direction / body → Approve sends payload_final).
// Batch: a checkbox that participates in the sticky batch bar.
//
// When `edited` is true (or after a local edit) an "edited" marker shows and the
// original text (payload.text) is revealed alongside the effective text.
// ---------------------------------------------------------------------------

export default function ProposalCard({
  proposal,
  busy,
  selected,
  onToggleSelect,
  onApprove, // (id, payloadFinal|undefined)
  onReject, // (id)
}) {
  const [editing, setEditing] = useState(false);
  const eff = proposal.effective || proposal.payload || {};

  // Editable fields seeded from the effective payload.
  const [text, setText] = useState(eff.text || proposal.text || "");
  const [dueDate, setDueDate] = useState(eff.due_date || proposal.due_date || "");
  const [direction, setDirection] = useState(
    eff.direction || proposal.direction || ""
  );
  const [evidence, setEvidence] = useState(eff.evidence || "");

  const originalText = (proposal.payload && proposal.payload.text) || "";
  const effectiveText = proposal.text || eff.text || "";
  const isEdited = proposal.edited;

  function submitEdit() {
    const payloadFinal = {};
    if (text !== "") payloadFinal.text = text;
    if (dueDate !== "") payloadFinal.due_date = dueDate;
    if (direction !== "") payloadFinal.direction = direction;
    if (evidence !== "") payloadFinal.evidence = evidence;
    onApprove(proposal.id, payloadFinal);
  }

  const noteLine = [proposal.note, proposal.source]
    .filter(Boolean)
    .join(" · ");

  return (
    <div
      style={{
        background: C.VOID,
        border: `1px solid ${selected ? C.AVOCADO : C.MIST}`,
        borderRadius: 4,
        padding: "12px 14px",
        display: "flex",
        gap: 12,
        opacity: busy ? 0.55 : 1,
      }}
    >
      {/* Batch checkbox */}
      <input
        type="checkbox"
        checked={!!selected}
        onChange={() => onToggleSelect(proposal.id)}
        aria-label="Select proposal for batch action"
        style={{ marginTop: 4, width: 18, height: 18, flexShrink: 0, accentColor: C.AVOCADO }}
      />

      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 8 }}>
        {/* Header row: type badge + edited marker + due */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <Badge bg={C.EMBER}>{proposal.type_label || proposal.type}</Badge>
          {isEdited && (
            <Badge bg={C.MIST} color={C.ORACLE}>
              edited
            </Badge>
          )}
          {proposal.due_label && (
            <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.ORACLE }}>
              {proposal.due_label}
            </span>
          )}
          {proposal.person && (
            <span style={{ fontFamily: FONT_MONO, fontSize: 11, color: C.MOONSTONE }}>
              {proposal.person}
            </span>
          )}
        </div>

        {!editing ? (
          <>
            <div style={{ fontFamily: FONT_BODY, fontSize: 15, color: C.ARROW }}>
              {effectiveText || <span style={{ color: C.MOONSTONE }}>(no text)</span>}
            </div>

            {/* Original vs edited, when edited */}
            {isEdited && originalText && originalText !== effectiveText && (
              <div
                style={{
                  fontFamily: FONT_BODY,
                  fontSize: 12,
                  color: C.MOONSTONE,
                  borderLeft: `2px solid ${C.MIST}`,
                  paddingLeft: 8,
                }}
              >
                original: <span style={{ textDecoration: "line-through" }}>{originalText}</span>
              </div>
            )}

            {/* Evidence quote */}
            {proposal.evidence && (
              <div
                style={{
                  fontFamily: FONT_BODY,
                  fontStyle: "italic",
                  fontSize: 13,
                  color: C.MOONSTONE,
                  opacity: 0.85,
                }}
              >
                “{proposal.evidence}”
              </div>
            )}

            {/* Provenance */}
            {(noteLine || proposal.org || proposal.direction) && (
              <div style={{ fontFamily: FONT_MONO, fontSize: 10, color: C.MOONSTONE, opacity: 0.75 }}>
                {noteLine && <span>{noteLine}</span>}
                {proposal.org && <span>{noteLine ? " · " : ""}org: {proposal.org}</span>}
                {proposal.direction && (
                  <span>{noteLine || proposal.org ? " · " : ""}{proposal.direction}</span>
                )}
              </div>
            )}
          </>
        ) : (
          <EditForm
            text={text}
            setText={setText}
            dueDate={dueDate}
            setDueDate={setDueDate}
            direction={direction}
            setDirection={setDirection}
            evidence={evidence}
            setEvidence={setEvidence}
          />
        )}

        {/* Action row */}
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 2 }}>
          {!editing ? (
            <>
              <ActionBtn onClick={() => onApprove(proposal.id)} disabled={busy} tone="approve">
                Approve
              </ActionBtn>
              <ActionBtn onClick={() => onReject(proposal.id)} disabled={busy} tone="reject">
                Reject
              </ActionBtn>
              <ActionBtn onClick={() => setEditing(true)} disabled={busy} tone="neutral">
                Edit
              </ActionBtn>
            </>
          ) : (
            <>
              <ActionBtn onClick={submitEdit} disabled={busy} tone="approve">
                Approve edit
              </ActionBtn>
              <ActionBtn onClick={() => setEditing(false)} disabled={busy} tone="neutral">
                Cancel
              </ActionBtn>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function EditForm({
  text,
  setText,
  dueDate,
  setDueDate,
  direction,
  setDirection,
  evidence,
  setEvidence,
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <Field label="text">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          style={inputStyle}
        />
      </Field>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <Field label="due date (YYYY-MM-DD)" grow>
          <input
            value={dueDate}
            onChange={(e) => setDueDate(e.target.value)}
            placeholder="2026-08-01"
            style={inputStyle}
          />
        </Field>
        <Field label="direction" grow>
          <input
            value={direction}
            onChange={(e) => setDirection(e.target.value)}
            style={inputStyle}
          />
        </Field>
      </div>
      <Field label="evidence">
        <textarea
          value={evidence}
          onChange={(e) => setEvidence(e.target.value)}
          rows={2}
          style={inputStyle}
        />
      </Field>
    </div>
  );
}

function Field({ label, grow, children }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 3, flex: grow ? 1 : "none", minWidth: 140 }}>
      <span
        style={{
          fontFamily: FONT_MONO,
          fontSize: 9,
          letterSpacing: 1,
          textTransform: "uppercase",
          color: C.MOONSTONE,
        }}
      >
        {label}
      </span>
      {children}
    </label>
  );
}

const inputStyle = {
  fontFamily: FONT_BODY,
  fontSize: 14,
  background: C.SHADOW,
  border: `1px solid ${C.MIST}`,
  borderRadius: 3,
  color: C.ARROW,
  padding: "6px 8px",
  width: "100%",
  boxSizing: "border-box",
  resize: "vertical",
};

function ActionBtn({ children, onClick, disabled, tone }) {
  const map = {
    approve: { border: C.AVOCADO, color: C.AVOCADO },
    reject: { border: C.MIST, color: C.SIGNAL },
    neutral: { border: C.MIST, color: C.ARROW },
  };
  const t = map[tone] || map.neutral;
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        fontFamily: FONT_MONO,
        fontSize: 11,
        letterSpacing: 1,
        textTransform: "uppercase",
        padding: "7px 14px",
        borderRadius: 4,
        border: `1px solid ${t.border}`,
        background: "transparent",
        color: t.color,
        cursor: disabled ? "not-allowed" : "pointer",
        minHeight: 36,
      }}
    >
      {children}
    </button>
  );
}
