// ---------------------------------------------------------------------------
// features.js — feature flags
//
// legacyPanels: the old RDMIS survival/pipeline/revenue dashboard is parked,
// not deleted. When true, a /legacy route renders LegacyDashboard. Default OFF
// — legacy panels never render anywhere unless this is flipped on.
// ---------------------------------------------------------------------------

export const features = {
  legacyPanels: false,
};
