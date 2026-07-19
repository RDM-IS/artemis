// ---------------------------------------------------------------------------
// theme.js — shared design tokens for the Engagement Ops UI
//
// Dark-terminal aesthetic with 70s accents. The two 70s tokens (AVOCADO,
// BURNT_ORANGE) are reserved for a single purpose each — see comments.
// ---------------------------------------------------------------------------

export const C = {
  VOID: "#07070A", // page background
  SHADOW: "#12121A", // panel background
  MIST: "#2A2A35", // borders, muted elements
  SIGNAL: "#C8521A", // alerts, primary accent
  ORACLE: "#C8922A", // warnings, secondary accent
  MOONSTONE: "#9FB8C8", // labels, secondary text
  ARROW: "#EDE8E0", // primary text
  GREEN: "#2D7A4F", // success, online status
  EMBER: "#7A2E0A", // deep accent (badges)

  // 70s accents — use STRICTLY for their stated purpose:
  AVOCADO: "#A9B14A", // ONLY approve / confirm actions
  BURNT_ORANGE: "#D86E2C", // attention: overdue, stale, pending, near hard-date
};

export const FONT_BODY = "Georgia, serif";
export const FONT_MONO = "'Courier New', Courier, monospace";
