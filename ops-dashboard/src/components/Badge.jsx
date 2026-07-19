import { C, FONT_MONO } from "../theme";

// ---------------------------------------------------------------------------
// Badge — small uppercase mono pill. Used for type labels, pending counts, etc.
// ---------------------------------------------------------------------------

export default function Badge({ children, bg, color, style }) {
  return (
    <span
      style={{
        fontFamily: FONT_MONO,
        fontSize: 9,
        letterSpacing: 1,
        textTransform: "uppercase",
        padding: "2px 7px",
        borderRadius: 3,
        background: bg || C.MIST,
        color: color || C.ARROW,
        whiteSpace: "nowrap",
        fontWeight: "bold",
        ...style,
      }}
    >
      {children}
    </span>
  );
}
