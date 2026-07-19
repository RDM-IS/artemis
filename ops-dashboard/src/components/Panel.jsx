import { C, FONT_MONO } from "../theme";

// ---------------------------------------------------------------------------
// Panel — titled dark container. `accent` colors the title (e.g. centerpiece).
// ---------------------------------------------------------------------------

export default function Panel({ title, subtitle, accent, right, children, style }) {
  return (
    <div
      style={{
        background: C.SHADOW,
        border: `1px solid ${C.MIST}`,
        borderRadius: 4,
        padding: "16px 18px",
        display: "flex",
        flexDirection: "column",
        gap: 12,
        ...style,
      }}
    >
      {title && (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "baseline",
            borderBottom: `1px solid ${C.MIST}`,
            paddingBottom: 8,
            gap: 12,
            flexWrap: "wrap",
          }}
        >
          <div style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
            <span
              style={{
                fontFamily: FONT_MONO,
                fontSize: 12,
                letterSpacing: 2,
                textTransform: "uppercase",
                color: accent || C.MOONSTONE,
              }}
            >
              {title}
            </span>
            {subtitle && (
              <span
                style={{
                  fontFamily: FONT_MONO,
                  fontSize: 10,
                  color: C.MOONSTONE,
                  opacity: 0.8,
                }}
              >
                {subtitle}
              </span>
            )}
          </div>
          {right}
        </div>
      )}
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        {children}
      </div>
    </div>
  );
}
